"""Semantic extraction, compression, fusion, distillation and query stages (L1–L7)."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

import cv2
import numpy as np

from . import algorithms as learner
from .encoders import ClipEncoder, read_raw
from .project import image_frame, project, sha256, write_json


def choose_frames(prepared, split='train', image_id=None, limit=None):
    ids = ([image_id] if image_id is not None else
           prepared['smoke_image_ids'] if split == 'smoke' else
           [f['image_id'] for f in prepared['frames']] if split == 'all' else prepared['splits'][split])
    by_id = {f['image_id']: f for f in prepared['frames']}
    if any(i not in by_id for i in ids):
        raise ValueError('Unknown semantic image ID')
    return [by_id[i] for i in ids[:limit]]


def regions(config, frames):
    directory = Path(config['workspace']) / 'regions'
    directory.mkdir(parents=True, exist_ok=True)
    counts = []
    for frame in frames:
        masks, quality, features = read_raw(config['workspace'], frame['image_id'])
        labels = learner.masks_to_regions(masks, quality, **config['regions'])
        if labels.shape != masks.shape[1:] or labels.dtype != np.int32 or labels.min() < -1 or labels.max() >= len(features):
            raise ValueError('L1 output must preserve raw proposal indices and int32 pixel grid')
        target = directory / f"{frame['image_id']:06d}.npz"
        temporary = target.with_suffix('.npz.tmp')
        with temporary.open('wb') as stream:
            np.savez_compressed(stream, labels=labels, features=features)
        temporary.replace(target)
        counts.append({'image_id': frame['image_id'], 'covered_pixels': int((labels >= 0).sum())})
    return counts


def read_regions(config, image_id):
    with np.load(Path(config['workspace']) / 'regions' / f'{image_id:06d}.npz', allow_pickle=False) as data:
        return data['labels'].copy(), data['features'].copy()


def _save_torch(path, data):
    import torch
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    torch.save(data, temporary)
    temporary.replace(path)


def fit_autoencoder(config, prepared):
    import torch
    from .compression import build_codec, initialize_conditioning, codec_loss, fit_pca
    directory = Path(config['workspace']) / 'autoencoder'
    if (directory / 'checkpoint.pt').exists():
        raise ValueError('Autoencoder exists; use a new workspace for a different experiment')
    features = []
    for image_id in prepared['splits']['train']:
        labels, values = read_regions(config, image_id)
        used = np.unique(labels[labels >= 0])
        features.append(values[used])
    values = np.concatenate(features)
    if not len(values):
        raise ValueError('No valid training regions')
    options = config['autoencoder']
    torch.manual_seed(config['seed'])
    inputs = torch.as_tensor(values, device='cuda')
    conditioning = options.get('conditioning')
    codec = options.get('codec', 'mlp')
    model = build_codec(512, options['latent_dim'], conditioning=conditioning, codec=codec).cuda()
    if codec == 'pca':
        retained = fit_pca(model, inputs)
        _save_torch(directory / 'checkpoint.pt', {
            'state': {k: v.cpu() for k, v in model.state_dict().items()}, 'latent_dim': options['latent_dim'],
            'codec': codec, 'conditioning': None, 'train_ids': prepared['splits']['train'],
            'training_regions': len(inputs), 'rgb_checkpoint_sha256': prepared['rgb_checkpoint_sha256']})
        result = {'stage': 'autoencoder', 'codec': codec, 'method': 'train-only covariance eigendecomposition',
                  'training_regions': len(inputs), 'retained_train_variance': retained}
        write_json(directory / 'progress.json', result)
        return result
    if conditioning:
        initialize_conditioning(model, inputs)
    optimizer = torch.optim.Adam(model.parameters(), lr=options['learning_rate'])
    history = []
    for step in range(1, options['steps'] + 1):
        indices = torch.randint(len(inputs), (options['batch_size'],), device='cuda')
        target = inputs[indices]
        optimizer.zero_grad(set_to_none=True)
        loss = codec_loss(model, model(target), target, options.get('residual_weight', 0.))
        if not torch.isfinite(loss):
            raise ValueError('Nonfinite autoencoder loss')
        loss.backward()
        optimizer.step()
        if step == 1 or step % 100 == 0:
            history.append({'step': step, 'loss': float(loss.detach())})
            write_json(directory / 'progress.json', {'stage': 'autoencoder', 'step': step, 'total': options['steps'], 'history': history})
    _save_torch(directory / 'checkpoint.pt', {'state': {k: v.cpu() for k, v in model.state_dict().items()},
                 'latent_dim': options['latent_dim'], 'rgb_checkpoint_sha256': prepared['rgb_checkpoint_sha256'],
                 'train_ids': prepared['splits']['train'], 'training_regions': len(inputs),
                 'conditioning': conditioning, 'codec': codec})
    return {'steps': options['steps'], 'training_regions': len(inputs), 'loss': history[-1]['loss']}


def load_autoencoder(config, prepared):
    import torch
    from .compression import build_codec
    path = Path(config['workspace']) / 'autoencoder/checkpoint.pt'
    checkpoint = torch.load(path, map_location='cpu', weights_only=True)
    if (checkpoint['rgb_checkpoint_sha256'] != prepared['rgb_checkpoint_sha256']
            or checkpoint['train_ids'] != prepared['splits']['train']
            or checkpoint['latent_dim'] != config['autoencoder']['latent_dim']
            or checkpoint.get('codec', 'mlp') != config['autoencoder'].get('codec', 'mlp')
            or checkpoint.get('conditioning') != config['autoencoder'].get('conditioning')):
        raise ValueError('Autoencoder used a different source/split')
    model = build_codec(512, checkpoint['latent_dim'], conditioning=checkpoint.get('conditioning'),
                        codec=checkpoint.get('codec', 'mlp')).cuda()
    model.load_state_dict(checkpoint['state'])
    return model.eval().requires_grad_(False)


def encode(config, prepared, frames):
    import torch
    model = load_autoencoder(config, prepared)
    directory = Path(config['workspace']) / 'codes'
    directory.mkdir(parents=True, exist_ok=True)
    counts = []
    for frame in frames:
        labels, features = read_regions(config, frame['image_id'])
        with torch.no_grad():
            codes = model.encode(torch.as_tensor(features, device='cuda')).cpu().numpy()
        if codes.shape != (len(features), config['autoencoder']['latent_dim']) or not np.isfinite(codes).all():
            raise ValueError('Invalid encoded feature shape/value')
        target = directory / f"{frame['image_id']:06d}.npz"
        temporary = target.with_suffix('.npz.tmp')
        with temporary.open('wb') as stream:
            np.savez_compressed(stream, labels=labels, codes=codes, autoencoder_sha256=sha256(Path(config['workspace']) / 'autoencoder/checkpoint.pt'))
        temporary.replace(target)
        counts.append({'image_id': frame['image_id'], 'codes': len(codes)})
    return counts


def validate_autoencoder(config, prepared, split):
    import torch
    if split not in ('dev', 'test'):
        raise ValueError('Autoencoder validation requires held-out dev/test regions')
    model = load_autoencoder(config, prepared)
    rows = []
    with torch.no_grad():
        for frame in choose_frames(prepared, split):
            labels, features = read_regions(config, frame['image_id'])
            used = np.unique(labels[labels >= 0])
            if not len(used):
                continue
            target = torch.as_tensor(features[used], device='cuda')
            reconstructed = model(target)
            rows.append({'image_id': frame['image_id'], 'regions': len(used),
                         'mse': float((reconstructed-target).square().mean()),
                         'mean_cosine': float(torch.nn.functional.cosine_similarity(reconstructed, target).mean())})
    if not rows:
        raise ValueError('No valid held-out regions to evaluate')
    result = {'split': split, 'per_view': rows, 'scope': 'Region feature reconstruction, not segmentation quality'}
    write_json(Path(config['workspace']) / 'autoencoder' / f'{split}-metrics.json', result)
    return result


def target_frame(config, frame, *, latent=True):
    import torch
    rgb, resized = image_frame(frame, config['distill']['max_edge'])
    if latent:
        with np.load(Path(config['workspace']) / 'codes' / f"{frame['image_id']:06d}.npz", allow_pickle=False) as data:
            if str(data['autoencoder_sha256']) != sha256(Path(config['workspace']) / 'autoencoder/checkpoint.pt'):
                raise ValueError('Stale latent cache: autoencoder changed')
            labels, values = data['labels'], data['codes']
    else:
        labels, values = read_regions(config, frame['image_id'])
    labels = cv2.resize(labels, (resized['width'], resized['height']), interpolation=cv2.INTER_NEAREST_EXACT)
    valid = labels >= 0
    target = np.zeros((*labels.shape, values.shape[-1]), np.float32)
    target[valid] = values[labels[valid]]
    return torch.from_numpy(target).cuda(), torch.from_numpy(valid).cuda(), resized, rgb


def _save_features(config, prepared, name, values, **metadata):
    path = Path(config['workspace']) / name / 'checkpoint.pt'
    _save_torch(path, {'features': values.detach().cpu(), 'rgb_checkpoint_sha256': prepared['rgb_checkpoint_sha256'],
                     'autoencoder_sha256': sha256(Path(config['workspace']) / 'autoencoder/checkpoint.pt'),
                     'train_ids': prepared['splits']['train'], 'latent_dim': config['autoencoder']['latent_dim'], **metadata})


def load_features(config, prepared, mode):
    import torch
    checkpoint = torch.load(Path(config['workspace']) / mode / 'checkpoint.pt', map_location='cpu', weights_only=True)
    if (checkpoint['rgb_checkpoint_sha256'] != prepared['rgb_checkpoint_sha256']
            or checkpoint['train_ids'] != prepared['splits']['train']
            or checkpoint['autoencoder_sha256'] != sha256(Path(config['workspace']) / 'autoencoder/checkpoint.pt')
            or checkpoint['features'].shape != (prepared['gaussian_count'], config['autoencoder']['latent_dim'])):
        raise ValueError('Language features are not bound to this RGB Gaussian order/autoencoder/split')
    return checkpoint['features'].cuda()


def fuse(config, prepared):
    import torch
    from .rendering import FrozenGaussians
    if (Path(config['workspace']) / 'fusion/checkpoint.pt').exists():
        raise ValueError('Fusion result exists; use another workspace to change it')
    renderer = FrozenGaussians(config, prepared)
    count, dimensions = prepared['gaussian_count'], config['autoencoder']['latent_dim']
    total, coverage = torch.zeros((count, dimensions), device='cuda'), torch.zeros(count, device='cuda')
    with torch.no_grad():
        for index, frame in enumerate(choose_frames(prepared, 'train')):
            target, valid, resized, _ = target_frame(config, frame)
            indices, values, weights = renderer.observations(target, valid, resized)
            averaged, weight = learner.fuse_observations(indices, values, weights, count)
            total += averaged * weight[:, None]
            coverage += weight
            write_json(Path(config['workspace']) / 'fusion/progress.json', {'step': index + 1, 'total': len(prepared['splits']['train'])})
        result = torch.where(coverage[:, None] > 0, total / coverage[:, None].clamp_min(1e-8), 0.)
    _save_features(config, prepared, 'fusion', result, covered_gaussians=int((coverage > 0).sum()),
                   coverage=(coverage > 0).cpu(),
                   visibility='Center projection with 5% expected-depth gate; approximate baseline')
    return {'covered_gaussians': int((coverage > 0).sum()), 'gaussian_count': count}


def distill(config, prepared):
    import torch
    from .rendering import FrozenGaussians
    if (Path(config['workspace']) / 'distilled/checkpoint.pt').exists():
        raise ValueError('Distillation result exists; choose another workspace')
    renderer = FrozenGaussians(config, prepared)
    options = config['distill']
    torch.manual_seed(config['seed'])
    initial = (load_features(config, prepared, 'fusion') if (Path(config['workspace']) / 'fusion/checkpoint.pt').exists()
               else torch.zeros((prepared['gaussian_count'], config['autoencoder']['latent_dim']), device='cuda'))
    values = torch.nn.Parameter(initial)
    optimizer = torch.optim.Adam([values], lr=options['learning_rate'])
    frames = choose_frames(prepared, 'train')
    rng = np.random.default_rng(config['seed'])
    history = []
    for step in range(1, options['steps'] + 1):
        frame = frames[int(rng.integers(len(frames)))]
        target, valid, resized, _ = target_frame(config, frame)
        optimizer.zero_grad(set_to_none=True)
        prediction, alpha = renderer.splat(values, resized)
        valid = valid & (alpha[..., 0] >= options['min_opacity'])
        loss = learner.distillation_loss(prediction, target, valid)
        if not torch.isfinite(loss):
            raise ValueError('Nonfinite feature loss')
        loss.backward()
        optimizer.step()
        if step == 1 or step % 100 == 0:
            history.append({'step': step, 'loss': float(loss.detach()), 'supervised_pixels': int(valid.sum())})
            write_json(Path(config['workspace']) / 'distilled/progress.json', {'step': step, 'total': options['steps'], 'history': history})
    _save_features(config, prepared, 'distilled', values, steps=options['steps'], geometry_frozen=True)
    return {'steps': options['steps'], 'loss': float(loss.detach()), 'geometry_frozen': True}


def prediction_map(config, prepared, frame, text, mode, *, clip=None, renderer=None, decoder=None, latents=None):
    import torch
    from .rendering import FrozenGaussians
    clip = clip or ClipEncoder(config)
    embeddings = clip.texts([text, *config['query']['negatives']])
    with torch.no_grad():
        if mode in ('2d', 'reconstructed'):
            labels, features = read_regions(config, frame['image_id'])
            features = torch.as_tensor(features, device=embeddings.device)
            if mode == 'reconstructed':
                decoder = decoder or load_autoencoder(config, prepared)
                features = decoder(features)
            scores = learner.text_relevancy(features, embeddings[0], embeddings[1:], temperature=config['query']['temperature'])
            valid = labels >= 0
            heatmap = np.zeros(labels.shape, np.float32)
            heatmap[valid] = scores.cpu().numpy()[labels[valid]]
            rgb, _ = image_frame(frame, config['extract']['max_edge'])
        else:
            renderer = renderer or FrozenGaussians(config, prepared)
            if mode == 'fusion' and renderer.semantic_coverage is None:
                bundle = torch.load(Path(config['workspace']) / 'fusion/checkpoint.pt', map_location='cpu', weights_only=True)
                renderer.semantic_coverage = bundle['coverage'].cuda().float()
            decoder = decoder or load_autoencoder(config, prepared)
            latents = load_features(config, prepared, mode) if latents is None else latents
            rgb, resized = image_frame(frame, config['distill']['max_edge'])
            feature_map, alpha = renderer.splat(latents, resized)
            valid = alpha[..., 0] >= config['distill']['min_opacity']
            # Chunk the decoder: never allocate H*W*512 activations on the GPU.
            flat = feature_map.reshape(-1, feature_map.shape[-1])
            pieces = []
            for start in range(0, len(flat), 8192):
                decoded = decoder.decode(flat[start:start + 8192])
                pieces.append(learner.text_relevancy(decoded, embeddings[0], embeddings[1:], temperature=config['query']['temperature']).cpu())
            heatmap = torch.cat(pieces).reshape(feature_map.shape[:2]).numpy()
            valid = valid.cpu().numpy()
            heatmap[~valid] = 0
    if heatmap.shape != rgb.shape[:2] or not np.isfinite(heatmap).all():
        raise ValueError('Invalid semantic prediction grid/value')
    return heatmap, valid, rgb


def query(config, prepared, frame, text, mode):
    if not text.strip() or len(text) > 300:
        raise ValueError('Use a nonempty text prompt up to 300 characters')
    heatmap, valid, rgb = prediction_map(config, prepared, frame, text, mode)
    mask = (heatmap > config['query']['threshold']) & valid
    name = time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]
    directory = Path(config['workspace']) / 'queries' / name
    directory.mkdir(parents=True, exist_ok=False)
    color = cv2.cvtColor(cv2.applyColorMap(np.uint8(np.clip(heatmap, 0, 1) * 255), cv2.COLORMAP_TURBO), cv2.COLOR_BGR2RGB)
    overlay = (rgb * .55 + color * .45).astype(np.uint8)
    cv2.imwrite(str(directory / 'overlay.png'), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(directory / 'mask.png'), mask.astype(np.uint8) * 255)
    np.savez_compressed(directory / 'scores.npz', scores=heatmap, valid=valid, mask=mask)
    result = {'image_id': frame['image_id'], 'text': text, 'mode': mode, 'threshold': config['query']['threshold'],
              'width': rgb.shape[1], 'height': rgb.shape[0], 'selected_pixels': int(mask.sum()), 'directory': str(directory)}
    write_json(directory / 'query.json', result)
    return result
