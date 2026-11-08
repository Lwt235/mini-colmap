"""Real local SAM/OpenCLIP adapters and the existing workbench GPU reservation."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

import cv2
import numpy as np

from .project import contained, sha256, write_json


def model_assets(config, *, verify=False):
    manifest = json.loads(Path(config['model_manifest']).read_text())
    result = {}
    for key in ('sam', 'clip'):
        item = manifest[key]
        path = contained(config['_root'], item['path'])
        if not path.is_file() or path.stat().st_size != item['size']:
            raise ValueError(f'Missing/incomplete {key} weights; run scripts/setup_semantic_models.py')
        if verify and sha256(path) != item['sha256']:
            raise ValueError(f'{key} checkpoint checksum mismatch')
        result[key] = path
    return result, manifest


@contextmanager
def gpu_session(config, *, reservation_token=None):
    """Expose one explicitly selected GPU and share the workbench reservation."""
    from ..ui.gpu import selected_gpu
    current = os.environ.get('CUDA_VISIBLE_DEVICES',str(selected_gpu(config['_root'])))
    if not current.isascii() or not current.isdecimal():
        raise ValueError('Select exactly one physical GPU with CUDA_VISIBLE_DEVICES')
    os.environ['CUDA_VISIBLE_DEVICES'] = current
    from ..ui.gpu_lease import acquire, adopt, release
    root = Path(config['_root'])

    def check_workbench():
        for path in (root / '.cache/mini-colmap-ui/runs').glob('*.json'):
            record = json.loads(path.read_text())
            if record.get('status') == 'running':
                raise RuntimeError('A workbench task is active; wait or cancel it first')
        try:
            with urllib.request.urlopen('http://127.0.0.1:8765/api/semantic/status', timeout=3) as response:
                if json.load(response).get('gpu_busy'):
                    raise RuntimeError('Close the workbench live view or wait for its GPU task')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        except urllib.error.URLError:
            pass  # No local service; the file/process lease still protects CLI jobs.
    check_workbench()
    token = adopt(root,reservation_token) if reservation_token else acquire(root, Path(config['workspace']))
    try:
        check_workbench()
        import torch
        torch.set_num_threads(4)
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError('The selected physical GPU must be exposed as the single CUDA device')
        yield
    finally:
        release(root, token)


class ClipEncoder:
    def __init__(self, config):
        import open_clip
        import torch
        assets, _ = model_assets(config)
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            'ViT-B-16', pretrained=str(assets['clip']), device='cuda')
        self.model.eval().requires_grad_(False)
        self.tokenizer = open_clip.get_tokenizer('ViT-B-16')

    def images(self, crops, batch_size=32):
        from PIL import Image
        torch = self.torch
        output = []
        with torch.inference_mode(), torch.autocast('cuda', dtype=torch.float16):
            for start in range(0, len(crops), batch_size):
                batch = torch.stack([self.preprocess(Image.fromarray(crop)) for crop in crops[start:start + batch_size]]).cuda()
                feature = self.model.encode_image(batch).float()
                output.append(torch.nn.functional.normalize(feature, dim=-1).cpu().numpy())
        return np.concatenate(output) if output else np.empty((0, 512), np.float32)

    def texts(self, texts):
        torch = self.torch
        with torch.inference_mode():
            feature = self.model.encode_text(self.tokenizer(texts).cuda()).float()
            return torch.nn.functional.normalize(feature, dim=-1)


def extract(config, frames):
    """Cache real raw proposals and regional embeddings; L1 is a separate stage."""
    import torch
    from segment_anything import SamAutomaticMaskGenerator, sam_model_registry
    from .project import image_frame
    assets, manifest = model_assets(config, verify=True)
    options = config['extract']
    clip = ClipEncoder(config)
    sam = sam_model_registry['vit_h'](checkpoint=str(assets['sam'])).cuda().eval()
    sam.requires_grad_(False)
    generator = SamAutomaticMaskGenerator(sam, **{key: options[key] for key in (
        'points_per_side', 'points_per_batch', 'pred_iou_thresh', 'stability_score_thresh',
        'crop_n_layers', 'min_mask_region_area')})
    directory = Path(config['workspace']) / 'raw'
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for frame in frames:
        rgb, resized = image_frame(frame, options['max_edge'])
        path = directory / f"{frame['image_id']:06d}.npz"
        metadata = directory / f"{frame['image_id']:06d}.json"
        photo_hash = sha256(frame['path'])
        cache_key = {'image_sha256': photo_hash, 'extract': options,
                     'sam_sha256': manifest['sam']['sha256'], 'clip_sha256': manifest['clip']['sha256']}
        if path.exists() and metadata.exists():
            cached = json.loads(metadata.read_text())
            if cached['cache_key'] != cache_key:
                raise ValueError('Extraction cache identity changed; use a new workspace')
            results.append(cached)
            continue
        with torch.inference_mode():
            proposals = generator.generate(rgb)
        masks = np.array([item['segmentation'] for item in proposals], dtype=bool).reshape(-1, *rgb.shape[:2])
        crops = []
        for item in proposals:
            x, y, width, height = map(int, item['bbox'])
            width, height = max(1, width), max(1, height)
            masked = np.where(item['segmentation'][..., None], rgb, 0)[y:y + height, x:x + width]
            edge = max(masked.shape[:2])
            square = np.zeros((edge, edge, 3), np.uint8)
            top, left = (edge - masked.shape[0]) // 2, (edge - masked.shape[1]) // 2
            square[top:top + masked.shape[0], left:left + masked.shape[1]] = masked
            crops.append(square)
        features = clip.images(crops, options['clip_batch_size'])
        qualities = np.array([p['predicted_iou'] * p['stability_score'] for p in proposals], np.float32)
        temp = path.with_suffix('.npz.tmp')
        with temp.open('wb') as stream:
            np.savez_compressed(stream, masks=np.packbits(masks.reshape(len(masks), -1), axis=1) if len(masks) else np.empty((0, (rgb.shape[0]*rgb.shape[1]+7)//8), np.uint8),
                                shape=np.array(rgb.shape[:2]), features=features.astype(np.float32), quality=qualities)
        temp.replace(path)
        # Raw proposal overlay is a QA visualization, not the L1 region assignment.
        overlay = rgb.copy().astype(np.float32)
        rng = np.random.default_rng(0)
        for mask in masks:
            overlay[mask] = .65 * overlay[mask] + .35 * rng.integers(20, 255, 3)
        preview = directory / f"{frame['image_id']:06d}.png"
        cv2.imwrite(str(preview), cv2.cvtColor(overlay.astype(np.uint8), cv2.COLOR_RGB2BGR))
        result = {'image_id': frame['image_id'], 'image_name': frame['name'], 'width': resized['width'],
                  'height': resized['height'], 'regions': len(proposals), 'feature_dim': 512, 'cache_key': cache_key}
        write_json(metadata, result)
        results.append(result)
        print(json.dumps({'stage': 'extract', **{k: v for k, v in result.items() if k != 'cache_key'}}, ensure_ascii=False), flush=True)
    return results


def read_raw(workspace, image_id):
    with np.load(Path(workspace) / 'raw' / f'{image_id:06d}.npz', allow_pickle=False) as data:
        height, width = map(int, data['shape'])
        masks = np.unpackbits(data['masks'], axis=1, count=height*width).reshape(-1, height, width).astype(bool)
        return masks, data['quality'].copy(), data['features'].copy()
