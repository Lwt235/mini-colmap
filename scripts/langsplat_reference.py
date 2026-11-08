#!/usr/bin/env python3
"""Pinned LangSplat reference stages, isolated from the learner/workbench stack.

Uses upstream preprocessing, AE, optimizer, rasterizer and training entry. The
data adapter keeps our frozen RGB and the fixed language holdout protocol.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import random
import sys
import time

os.environ.setdefault('CUDA_VISIBLE_DEVICES', '7')
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / '.cache/langsplat-official'
WORK = ROOT / 'data/langsplat_reference'
REPORT = ROOT / 'reports/langsplat-reference-20261021'
sys.path.insert(0, str(ROOT / '.cache/gaussian-upstream-audit/site'))
sys.path.insert(0, str(ROOT / '.cache/langsplat-runtime/site'))
sys.path.insert(0, str(UP / 'submodules/segment-anything-langsplat'))
sys.path.insert(0, str(UP))

import cv2
import numpy as np
import torch
from minicolmap.semantics.project import load_config, project, write_json, sha256
from minicolmap.semantics.encoders import gpu_session, model_assets


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def seed(value=0):
    random.seed(value)
    np.random.seed(value)
    torch.manual_seed(value)
    torch.cuda.manual_seed_all(value)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.set_num_threads(4)


def progress(stage, **values):
    data = dict(stage=stage, **values)
    write_json(REPORT / 'progress.json', data)
    print(json.dumps(data), flush=True)


def fast_mask_nms(masks, scores, iou_thr=.7, score_thr=.1, inner_thr=.2, **kwargs):
    """Same CPU sort/thresholds; exact integer intersections via GPU GEMM.

    No TF32: sums of binary products below 2**24 are exactly representable.
    Keep division on CPU to retain upstream threshold arithmetic and tie order.
    """
    scores, idx = scores.sort(0, descending=True)
    masks = masks[idx]
    areas = masks.sum((1, 2), dtype=torch.float)
    flat = masks.flatten(1).to(kwargs.get('intersection_device','cuda')).float()
    intersections = (flat @ flat.T).cpu().round().long()
    del flat
    area_i = areas.long()[:, None]
    area_j = areas.long()[None, :]
    union = area_i + area_j - intersections
    iou = (intersections / union).triu(1)
    ri, rj = intersections / area_i, intersections / area_j
    first = (ri < .5) & (rj >= .85)
    second = (ri >= .85) & (rj < .5)
    triangular = torch.ones_like(first).triu().bool()
    values = 1 - rj * ri
    inner = torch.where(first & triangular, values, 0.)
    inner += torch.where(second & triangular, values, 0.).T
    keep = iou.max(0).values <= iou_thr
    confidence = scores > score_thr
    upper = inner.triu(1).max(0).values <= 1-inner_thr
    lower = inner.tril(-1).max(0).values <= 1-inner_thr
    for decision in (confidence, upper, lower):
        if not decision.any():
            # Upstream uses [indices,0] on a 1D tensor in this rare fallback.
            # Preserve intended top-three semantics, record if actually reached.
            decision[scores.topk(min(3, len(scores))).indices] = True
            progress('preprocess-nms-fallback', reason='upstream 1D index compatibility')
    return idx[keep & confidence & upper & lower]


def share_sam_postprocessing(generator):
    """Avoid doing identical mask filtering/RLE twice for default and s/m/l.

    The first real decoder batch is compared field-for-field against unmodified
    upstream for all four outputs before enabling this optimization.
    """
    import types
    import importlib
    api = importlib.import_module(type(generator).__module__)
    original = generator._process_batch
    checked = False
    def shared(self, points, im_size, crop_box, orig_size):
        transformed = self.predictor.transform.apply_coords(points, im_size)
        in_points = torch.as_tensor(transformed, device=self.predictor.device)
        in_labels = torch.ones(in_points.shape[0],dtype=torch.int,device=in_points.device)
        masks, scores, _ = self.predictor.predict_torch(in_points[:,None],in_labels[:,None],
                                                      multimask_output=True,return_logits=True)
        data = api.MaskData(masks=masks.flatten(0,1),iou_preds=scores.flatten(0,1),
                            points=torch.as_tensor(points.repeat(masks.shape[1],axis=0)),
                            reference_level=torch.arange(3).repeat(len(points)))
        data = self._process_batch_data(data,crop_box,orig_size[1],orig_size[0])
        levels = data['reference_level']
        del data['reference_level']
        outputs = [data]
        for level in range(3):
            selected = api.MaskData(**dict(data.items()))
            selected.filter(levels==level)
            outputs.append(selected)
        return tuple(outputs)
    def guarded(self, points, im_size, crop_box, orig_size):
        nonlocal checked
        if not checked:
            sample = points[:8]
            expected = original(sample,im_size,crop_box,orig_size)
            actual = shared(self,sample,im_size,crop_box,orig_size)
            for e,a in zip(expected,actual):
                assert set(dict(e.items())) == set(dict(a.items()))
                for key,value in e.items():
                    if isinstance(value,torch.Tensor):
                        torch.testing.assert_close(value,a[key],rtol=0,atol=0)
                    else:
                        assert value == a[key], key
            write_json(REPORT/'sam-batch-parity.json',dict(real_points=len(sample),four_levels=True,
                       exact=True,compared='all scores, points, boxes, stability values and RLE mask dictionaries'))
            checked = True
        return shared(self,points,im_size,crop_box,orig_size)
    generator._process_batch = types.MethodType(guarded,generator)


def preprocess(config, prepared):
    import open_clip
    module = load_file('reference_preprocess', UP / 'preprocess.py')
    assets, _ = model_assets(config, verify=True)
    create = open_clip.create_model_and_transforms
    def local_model(*args, **kwargs):
        kwargs['pretrained'] = str(assets['clip'])
        return create(*args, **kwargs)
    open_clip.create_model_and_transforms = local_model
    module.model = module.OpenCLIPNetwork(module.OpenCLIPNetworkConfig)
    from segment_anything import SamAutomaticMaskGenerator, sam_model_registry
    module.mask_generator = SamAutomaticMaskGenerator(
        sam_model_registry['vit_h'](checkpoint=str(assets['sam'])).cuda(),
        points_per_side=32, pred_iou_thresh=.7, box_nms_thresh=.7,
        stability_score_thresh=.85, crop_n_layers=1,
        crop_n_points_downscale_factor=1, min_mask_region_area=100)
    share_sam_postprocessing(module.mask_generator)
    original_nms = module.mask_nms
    # Independent nested/overlap control, including score ties.
    rng = np.random.default_rng(3)
    masks = torch.from_numpy(rng.random((12, 31, 29)) > .45)
    masks[2] = masks[1]
    scores = torch.tensor([.9, .95, .95, .8, .7, .9, .75, .85, .88, .96, .92, .83])
    expected = original_nms(masks, scores, .8, .7, .5)
    torch.testing.assert_close(fast_mask_nms(masks, scores, .8, .7, .5), expected, rtol=0, atol=0)
    module.mask_nms = fast_mask_nms
    directory = WORK / 'language_features'
    directory.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    rows = []
    for index, frame in enumerate(prepared['frames']):
        stem = Path(frame['name']).stem
        target = directory / f'{stem}_f.npy'
        meta = directory / f'{stem}.json'
        key = dict(image_sha256=frame['image_sha256'], preprocess_sha256=sha256(UP/'preprocess.py'),
                   sam_source_sha256=sha256(UP/'submodules/segment-anything-langsplat/segment_anything/automatic_mask_generator.py'))
        if target.exists() and meta.exists():
            saved = json.loads(meta.read_text())
            if saved['key'] != key:
                raise ValueError('Reference extraction identity changed')
            rows.append(saved)
            continue
        image = cv2.imread(frame['path'])
        if image.shape[:2] != (frame['height'], frame['width']) or sha256(frame['path']) != frame['image_sha256']:
            raise ValueError('Input photo changed')
        # Exact upstream BGR input, masked crops, resize, padding and CLIP transform.
        with torch.inference_mode():
            features, maps = module._embed_clip_sam_tiles(torch.from_numpy(image).permute(2,0,1)[None], module.sam_encoder)
        offsets, labels, values = 0, [], []
        for name in ('default', 's', 'm', 'l'):
            if name not in features or name not in maps:
                raise ValueError('Official SAM produced an empty semantic level; no single-level fallback')
            values.append(features[name].float().numpy())
            label = maps[name].copy()
            label[label >= 0] += offsets
            labels.append(label.astype(np.int32))
            offsets += len(features[name])
        np.save(target, np.concatenate(values))
        np.save(directory/f'{stem}_s.npy', np.stack(labels))
        saved = dict(image_id=frame['image_id'], image_name=frame['name'], key=key,
                     counts={name:len(features[name]) for name in features},
                     coverage={name:float((maps[name]>=0).mean()) for name in maps})
        write_json(meta, saved)
        rows.append(saved)
        progress('preprocess', completed=index+1, total=len(prepared['frames']), image=frame['name'],
                 counts=saved['counts'], seconds=time.monotonic()-start)
    write_json(REPORT/'preprocessing.json', dict(images=rows, gpu=7, exact_upstream_crops=True,
               nms_integer_parity=True, label_storage='int32; same integer indices as upstream float32'))


def upstream_ae(device='cuda'):
    return load_file('reference_ae_model', UP/'autoencoder/model.py').Autoencoder(
        [256,128,64,32,3], [16,32,64,128,256,256,512]).to(device)


def autoencoder(config, prepared, *, device='cuda'):
    """Upstream AE model/objective/100 epochs, with a GPU-resident data adapter.

    Exact dim=0 cosine and 0.001 weight; upstream chooses among epochs96..99 on
    reconstruction of its training corpus. No manual dev/test labels enter fit.
    Matched mode excludes those images; full-view mode explicitly includes them.
    """
    directory = WORK/'autoencoder'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory/'best_ckpt.pth'
    if (directory/'completed.json').exists():
        return
    if target.exists():
        raise ValueError('Partial AE selection exists without completion; do not silently reuse it')
    by_id = {f['image_id']:f for f in prepared['frames']}
    inputs = []
    for image_id in sorted(prepared['splits']['train'],key=lambda i:by_id[i]['name']):
        stem = Path(by_id[image_id]['name']).stem
        inputs.append(np.load(WORK/'language_features'/f'{stem}_f.npy'))
    values = torch.from_numpy(np.concatenate(inputs)).to(device)
    del inputs
    model = upstream_ae(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=.0007)
    history, best = [], float('inf')
    for epoch in range(100):
        model.train()
        indices = torch.randperm(len(values), device=device)
        losses = []
        for start in range(0, len(values), 64):
            batch = values[indices[start:start+64]]
            if len(batch) == 1:
                raise ValueError('Upstream BatchNorm cannot train a singleton final batch')
            prediction = model(batch)
            l2 = (prediction-batch).square().mean()
            cosine = 1-torch.nn.functional.cosine_similarity(prediction, batch, dim=0).mean()
            loss = l2 + .001*cosine
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite upstream AE loss')
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            losses.append(loss.detach())
        row = dict(epoch=epoch, loss=float(torch.stack(losses).mean()))
        if epoch > 95:
            model.eval()
            total = torch.zeros((),device=device)
            with torch.no_grad():
                for start in range(0, len(values), 256):
                    batch = values[start:start+256]
                    predicted = model(batch)
                    score = (predicted-batch).square().mean()+1-torch.nn.functional.cosine_similarity(predicted,batch,dim=0).mean()
                    total += score*len(batch)
            row['upstream_train_selection_loss'] = float(total/len(values))
            if row['upstream_train_selection_loss'] < best:
                best = row['upstream_train_selection_loss']
                torch.save(model.state_dict(), target)
                write_json(directory/'selection.json', dict(epoch=epoch, loss=best))
        history.append(row)
        write_json(directory/'history.json', dict(training_regions=len(values), batch_size=64, train_ids=prepared['splits']['train'],history=history))
        progress('autoencoder', **row, regions=len(values), epochs=100)
    write_json(directory/'completed.json',dict(epochs=100,regions=len(values),best_selection_loss=best,
               model_sha256=sha256(UP/'autoencoder/model.py'),
               objective='MSE + 0.001*(1-cosine_similarity(prediction,target,dim=0).mean())',
               data_adapter='GPU-resident epoch permutation; batch64; fitting IDs explicitly recorded',
               fit_includes_evaluation_views=bool(set(prepared['splits']['train']) &
                   (set(prepared['splits']['dev'])|set(prepared['splits']['test']))),
               train_ids=prepared['splits']['train']))


def encode(config, prepared):
    model = upstream_ae()
    model.load_state_dict(torch.load(WORK/'autoencoder/best_ckpt.pth',weights_only=True))
    model.eval()
    directory = WORK/'language_features_dim3'
    directory.mkdir(exist_ok=True)
    with torch.inference_mode():
        for frame in prepared['frames']:
            stem = Path(frame['name']).stem
            features = np.load(WORK/'language_features'/f'{stem}_f.npy')
            values = torch.from_numpy(features).cuda()
            codes = torch.cat([model.encode(v) for v in values.split(256)]).cpu().numpy()
            if not np.isfinite(codes).all() or not np.allclose(np.linalg.norm(codes,axis=-1),1,atol=1e-4):
                raise ValueError('Invalid upstream unit latent codes')
            np.save(directory/f'{stem}_f.npy', codes)
            np.save(directory/f'{stem}_s.npy', np.load(WORK/'language_features'/f'{stem}_s.npy'))
    progress('encode', completed=len(prepared['frames']))
    write_json(REPORT/'encoding.json',dict(completed=len(prepared['frames']),latent_dim=3,
               normalized=True,autoencoder_sha256=sha256(WORK/'autoencoder/best_ckpt.pth')))


def native_camera(frame):
    from types import SimpleNamespace
    from utils.graphics_utils import getProjectionMatrix
    import math
    width, height = frame['width'], frame['height']
    K = np.asarray(frame['K'])
    if not np.allclose(K[:2,2], [(width-1)/2,(height-1)/2], atol=1e-5):
        raise ValueError('Official FoV camera cannot discard an off-center principal point')
    view = torch.eye(4,device='cuda')
    view[:3,:3] = torch.tensor(frame['R'],device='cuda')
    view[:3,3] = torch.tensor(frame['t'],device='cuda')
    view = view.T.contiguous()
    fx, fy = 2*math.atan(width/(2*K[0,0])), 2*math.atan(height/(2*K[1,1]))
    projection = getProjectionMatrix(.01,100.,fx,fy).T.cuda()
    return SimpleNamespace(FoVx=fx,FoVy=fy,image_width=width,image_height=height,
                           world_view_transform=view, full_proj_transform=view@projection,
                           camera_center=torch.linalg.inv(view)[3,:3], image_name=Path(frame['name']).stem,
                           uid=frame['image_id'])


def native_imports():
    import types
    scene = types.ModuleType('scene')
    scene.__path__ = [str(UP/'scene')]
    sys.modules['scene'] = scene
    from scene.gaussian_model import GaussianModel
    scene.GaussianModel = GaussianModel
    return scene, GaussianModel


def rgb_checkpoint(config, prepared):
    """Convert normalized project tensors back to the original world camera frame."""
    path = WORK/'frozen_rgb_world.pt'
    if path.exists():
        # Upstream training uses torch.load without map_location. Its RGB
        # checkpoints carry CUDA tensors; a CPU serialization passes host
        # pointers to the native rasterizer instead of device pointers.
        params, iteration = torch.load(path,weights_only=True)
        if params[1].device.type != 'cuda':
            params = tuple(v.cuda() if isinstance(v,torch.Tensor) else v for v in params)
            torch.save((params,iteration),path)
        return path
    source = torch.load(Path(config['rgb_run'])/'checkpoint.pt',map_location='cpu',weights_only=True)
    state = source['state']
    norm = source['normalization']
    scale = float(norm['scale'])
    center = torch.tensor(norm['center'])
    count = len(state['means'])
    params = (3, state['means']*scale+center, state['sh_coeffs'][:,:1].contiguous(),
              state['sh_coeffs'][:,1:].contiguous(), state['log_scales']+float(np.log(scale)),
              state['quaternions'], state['opacity_logits'].reshape(-1,1),
              torch.zeros(count), torch.zeros(count,1), torch.zeros(count,1), {}, scale)
    params = tuple(v.cuda() if isinstance(v,torch.Tensor) else v for v in params)
    torch.save((params,30000),path)
    write_json(REPORT/'rgb-conversion.json',dict(source_sha256=prepared['rgb_checkpoint_sha256'],
               conversion='xyz=normalized_xyz*scale+center; log_scale+=log(scale); all other values unchanged',
               normalization=norm, gaussians=count, camera_grid='unchanged native resolution',
               limitation='Official semantic method on our frozen RGB, not a full stock RGB/semantic paper replication'))
    return path


def geometry_probe(config,prepared,GaussianModel,source):
    """Sanity-check world cameras and fixed-RGB opacity before long training."""
    from types import SimpleNamespace
    from gaussian_renderer import render
    from langsplat_reference_metrics import annotations
    model = GaussianModel(3)
    params,_ = torch.load(source,map_location='cuda',weights_only=True)
    model.restore(params,SimpleNamespace(include_feature=True),mode='eval')
    model._language_feature = torch.ones((len(model.get_xyz),3),device='cuda')
    pipe = SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False)
    frames = {f['image_id']:f for f in prepared['frames']}
    rows = []
    with torch.inference_mode():
        for item in prepared['annotations']:
            i = item['image_id']
            if i not in prepared['splits']['dev']:
                continue
            frame = frames[i]
            output = render(native_camera(frame),model,pipe,torch.ones(3,device='cuda'),SimpleNamespace(include_feature=True))
            # Unit (1,1,1) features render alpha/sqrt(3), with no feature background.
            alpha = (output['language_feature_image'][0]*np.sqrt(3)).cpu().numpy().clip(0,1)
            prediction = output['render'].permute(1,2,0).cpu().numpy()
            gt = cv2.cvtColor(cv2.imread(frame['path']),cv2.COLOR_BGR2RGB).astype(np.float32)/255
            mse = float(np.mean((prediction-gt)**2))
            objects = []
            for text,ann in annotations(item['annotation']).items():
                values = alpha[ann['mask']>0]
                objects.append(dict(query=text,mean_alpha=float(values.mean()),below_half=float((values<.5).mean()),below_point9=float((values<.9).mean())))
            rows.append(dict(image_id=i,rgb_raw_psnr=float(-10*np.log10(mse)),objects=objects))
            cv2.imwrite(str(REPORT/f'geometry-alpha-{i:06d}.png'),(alpha*255).astype(np.uint8))
    write_json(REPORT/'geometry-probe.json',dict(per_view=rows,
               scope='Dev only; native world RGB projection sanity and opacity coverage, not ground-truth geometry accuracy'))


def train(config, prepared, level, steps):
    from types import SimpleNamespace
    scene_module, GaussianModel = native_imports()
    output = WORK / ('train' if steps==30000 else f'smoke-{steps}') / f'level{level}'
    output.mkdir(parents=True, exist_ok=True)
    if (output/'completed.json').exists() and (output/f'chkpnt{steps}.pth').exists():
        return
    if (output/f'chkpnt{steps}.pth').exists():
        raise ValueError('Training checkpoint exists without verification; do not silently treat as complete')
    source = rgb_checkpoint(config, prepared)
    if steps==100 and not (REPORT/'geometry-probe.json').exists():
        geometry_probe(config,prepared,GaussianModel,source)
    frames = {f['image_id']:f for f in prepared['frames']}
    cameras = []
    for image_id in sorted(prepared['splits']['train'],key=lambda i:frames[i]['name']):
        frame = frames[image_id]
        camera = native_camera(frame)
        labels = np.load(WORK/'language_features_dim3'/f"{camera.image_name}_s.npy")[level]
        codes = np.load(WORK/'language_features_dim3'/f"{camera.image_name}_f.npy")
        target = torch.from_numpy(codes[labels]).permute(2,0,1).contiguous().cuda()
        mask = torch.from_numpy(labels>=0)[None].cuda()
        camera.get_language_feature = lambda language_feature_dir, feature_level, target=target, mask=mask: (target,mask)
        if not cameras:
            from scene.cameras import Camera
            original_target, original_mask = Camera.get_language_feature(camera,str(WORK/'language_features_dim3'),level)
            torch.testing.assert_close(target,original_target,atol=0,rtol=0)
            torch.testing.assert_close(mask,original_mask,atol=0,rtol=0)
        cameras.append(camera)
    random.shuffle(cameras)  # Same Scene shuffle followed by stock epoch sampling.
    holder = []
    class AdaptedScene:
        def __init__(self, dataset, model):
            self.gaussians = model
            self.model_path = str(output)
            self.cameras_extent = prepared['normalization']['scale']
            holder.append(self)
        def getTrainCameras(self):
            return cameras
        def getTestCameras(self):
            return []
    scene_module.Scene = AdaptedScene
    entry = load_file('reference_training',UP/'train.py')
    # Only I/O/reporting adapters. training(), model/Adam and native CUDA unchanged.
    entry.prepare_output_and_logger = lambda dataset: None
    start = time.monotonic()
    trace = []
    def observe(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, *args):
        if iteration == 1 or iteration % 100 == 0 or iteration == steps:
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite official semantic loss')
            row = dict(iteration=iteration,loss=float(loss.detach()),seconds=time.monotonic()-start)
            trace.append(row)
            write_json(output/'progress.json',dict(level=level,total=steps,trace=trace))
            progress('train',level=level,total=steps,**row)
    entry.training_report = observe
    from arguments import OptimizationParams
    parser = argparse.ArgumentParser()
    OptimizationParams(parser)
    opt = parser.parse_args([])
    if not opt.include_feature or opt.language_feature_lr != .0025:
        raise ValueError('Pinned official semantic defaults changed')
    opt.iterations = steps
    dataset = SimpleNamespace(sh_degree=3,white_background=True,model_path=str(output),source_path=str(WORK),
                              lf_path=str(WORK/'language_features_dim3'),feature_level=level)
    pipe = SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False)
    checkpoints = sorted({steps,*[n for n in (3000,7000) if n<steps]})
    entry.training(dataset,opt,pipe,[],[],checkpoints,str(source),-1)
    frozen = torch.load(source,map_location='cpu',weights_only=True)[0]
    actual = holder[0].gaussians
    for expected, value in zip(frozen[1:7],(actual._xyz,actual._features_dc,actual._features_rest,
                                         actual._scaling,actual._rotation,actual._opacity)):
        torch.testing.assert_close(expected,value.detach().cpu(),rtol=0,atol=0)
    write_json(output/'completed.json',dict(level=level,iterations=steps,seconds=time.monotonic()-start,
               train_ids=prepared['splits']['train'],geometry_unchanged=True,
               train_py_sha256=sha256(UP/'train.py'),native_kernel=True,unit_gaussian_features=True,
               alpha_normalization=False,opacity_mask_gate=False,optimizer_epsilon=1e-15,
               semantic_lr=opt.language_feature_lr,
               autoencoder_sha256=sha256(WORK/'autoencoder/best_ckpt.pth'),
               rgb_source_sha256=prepared['rgb_checkpoint_sha256'],adapter_sha256=sha256(Path(__file__)),
               data_adapter='Native cameras, frozen world RGB, preloaded targets with exact getter parity'))


def reference_clip(config):
    import open_clip
    assets,_ = model_assets(config)
    create = open_clip.create_model_and_transforms
    def local_model(*args,**kwargs):
        kwargs['pretrained'] = str(assets['clip'])
        return create(*args,**kwargs)
    open_clip.create_model_and_transforms = local_model
    return load_file('reference_clip_eval',UP/'eval/openclip_encoder.py').OpenCLIPNetwork('cuda')


def native_models(prepared, checkpoint_step):
    _,GaussianModel = native_imports()
    models = []
    ae_hash = sha256(WORK/'autoencoder/best_ckpt.pth')
    for level in (1,2,3):
        directory = WORK/'train'/f'level{level}'
        marker = directory/'completed.json'
        if not marker.exists():
            raise ValueError(f'Reference level {level} has not finished training/verification')
        record = json.loads(marker.read_text())
        if record.get('autoencoder_sha256')!=ae_hash or record.get('rgb_source_sha256')!=prepared['rgb_checkpoint_sha256']:
            raise ValueError('Reference RGB/codec changed; refusing mismatched language field')
        params,_ = torch.load(directory/f'chkpnt{checkpoint_step}.pth',map_location='cuda',weights_only=True)
        model = GaussianModel(3)
        model.restore(params,None,mode='eval')
        if model.get_language_feature.shape!=(prepared['gaussian_count'],3):
            raise ValueError('Reference Gaussian indexing/dimension changed')
        models.append(model)
    return models


def evaluate(config, prepared, kind, split, checkpoint_step=30000):
    """Matched raw protocol and actual upstream eval bodies, on the same maps."""
    from langsplat_reference_metrics import evaluate_maps,load_metrics
    clip = reference_clip(config)
    ae = upstream_ae()
    ae.load_state_dict(torch.load(WORK/'autoencoder/best_ckpt.pth',map_location='cuda',weights_only=True))
    ae.eval()
    models = []
    if kind=='field':
        models = native_models(prepared,checkpoint_step)
        from gaussian_renderer import render
    tag = kind if kind!='field' or checkpoint_step==30000 else f'field-i{checkpoint_step}'
    directory = REPORT/'evaluation'/f'{split}-{tag}'
    directory.mkdir(parents=True,exist_ok=True)
    frames = {f['image_id']:f for f in prepared['frames']}
    annotations = {a['image_id']:a for a in prepared['annotations']}
    source = load_metrics(UP)
    rows = []
    with torch.inference_mode():
        for image_id in prepared['splits'][split]:
            annotation = annotations[image_id]
            frame = frames[image_id]
            assert annotation['image_name']==frame['name']
            record = json.loads(Path(annotation['annotation']).read_text())
            names = list(dict.fromkeys(o['category'] for o in record['objects']))
            clip.set_positives(names)
            heatmaps = []
            field_diagnostics = []
            if kind!='field':
                stem = Path(frame['name']).stem
                labels = np.load(WORK/'language_features'/f'{stem}_s.npy')
                features = torch.from_numpy(np.load(WORK/'language_features'/f'{stem}_f.npy')).cuda()
                if kind=='reconstructed':
                    features = ae(features)
                scores = np.stack([clip.get_relevancy(features,i)[:,0].float().cpu().numpy() for i in range(len(names))])
                for level in (1,2,3):
                    heat = np.zeros((len(names),*labels.shape[1:]),np.float32)
                    valid = labels[level]>=0
                    heat[:,valid] = scores[:,labels[level][valid]]
                    heatmaps.append(heat)
            else:
                from types import SimpleNamespace
                camera = native_camera(frame)
                pipe = SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False)
                opt = SimpleNamespace(include_feature=True)
                stem = Path(frame['name']).stem
                target_labels = np.load(WORK/'language_features_dim3'/f'{stem}_s.npy')
                target_codes = np.load(WORK/'language_features_dim3'/f'{stem}_f.npy')
                for level,model in enumerate(models,1):
                    latent = render(camera,model,pipe,torch.ones(3,device='cuda'),opt)['language_feature_image']
                    target = torch.from_numpy(target_codes[target_labels[level]]).permute(2,0,1).cuda()
                    valid = torch.from_numpy(target_labels[level]>=0).cuda()
                    if valid.any():
                        norms = latent.norm(dim=0)
                        field_diagnostics.append(dict(level=level,
                            whole_grid_masked_l1=float(((latent-target).abs()*valid[None]).mean()),
                            valid_pixel_l1=float((latent-target).abs()[:,valid].mean()),
                            valid_pixel_latent_cosine=float(torch.nn.functional.cosine_similarity(latent[:,valid],target[:,valid],dim=0).mean()),
                            mean_rendered_latent_norm=float(norms[valid].mean())))
                    latent = latent.permute(1,2,0).reshape(-1,3)
                    scores = []
                    for chunk in latent.split(8192):
                        restored = ae.decode(chunk)
                        scores.append(torch.stack([clip.get_relevancy(restored,i)[:,0] for i in range(len(names))]).cpu())
                    heatmaps.append(torch.cat(scores,dim=1).reshape(len(names),frame['height'],frame['width']).numpy())
            maps = np.stack(heatmaps)
            gt_shape = (record['info']['height'],record['info']['width'])
            if maps.shape[2:] != gt_shape:
                raise ValueError('Reference/annotation resolution mismatch')
            metrics,masks = evaluate_maps(UP,maps,names,annotation['annotation'],source=source)
            row = dict(image_id=image_id,image_name=frame['name'],kind=kind,field_diagnostics=field_diagnostics,**metrics)
            rows.append(row)
            np.savez_compressed(directory/f'{image_id:06d}-maps.npz',maps=maps,names=np.array(names))
            write_json(directory/f'{image_id:06d}-metrics.json',row)
            rgb = cv2.imread(frame['path'])
            for item in metrics['per_query']:
                text = item['query']
                level = item['official_level']-1
                score = maps[level,names.index(text)]
                overlay = cv2.addWeighted(rgb,.55,cv2.applyColorMap((np.clip(score,0,1)*255).astype(np.uint8),cv2.COLORMAP_TURBO),.45,0)
                safe = ''.join(c if c.isalnum() or c in '_-' else '_' for c in text)
                cv2.imwrite(str(directory/f'{image_id:06d}-{safe}-heatmap.png'),overlay)
                cv2.imwrite(str(directory/f'{image_id:06d}-{safe}-mask.png'),masks[text].astype(np.uint8)*255)
            progress('evaluate',kind=kind,split=split,image_id=image_id,official=metrics['official'],common=metrics['common'])
    count = sum(len(row['per_query']) for row in rows)
    summary = {protocol:{metric:sum(row[protocol][metric]*len(row['per_query']) for row in rows)/count
                         for metric in ('mean_iou',('bbox_localization_rate' if protocol=='official' else 'polygon_peak_rate'))}
               for protocol in ('common','official')}
    write_json(directory/'summary.json',dict(split=split,kind=kind,checkpoint_iteration=checkpoint_step if kind=='field' else None,
               queries=count,results=summary,per_view=rows,
               scope=prepared.get('reference_protocol','Matched fixed RGB and 173 language train views; official semantic components and metric AST; not end-to-end paper benchmark')))


def query(config, prepared, image_id, text, checkpoint_step=30000):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    import uuid
    from langsplat_reference_metrics import query_mask
    frames = {f['image_id']:f for f in prepared['frames']}
    if image_id not in frames or not text or not text.strip():
        raise ValueError('Query needs a registered image ID and nonempty text')
    frame = frames[image_id]
    clip = reference_clip(config)
    clip.set_positives([text])
    ae = upstream_ae()
    ae.load_state_dict(torch.load(WORK/'autoencoder/best_ckpt.pth',map_location='cuda',weights_only=True))
    ae.eval()
    models = native_models(prepared,checkpoint_step)
    from gaussian_renderer import render
    camera = native_camera(frame)
    pipe = SimpleNamespace(debug=False,convert_SHs_python=False,compute_cov3D_python=False)
    opt = SimpleNamespace(include_feature=True)
    maps = []
    with torch.inference_mode():
        for model in models:
            latent = render(camera,model,pipe,torch.ones(3,device='cuda'),opt)['language_feature_image']
            values = latent.permute(1,2,0).reshape(-1,3)
            scores = [clip.get_relevancy(ae.decode(chunk),0)[:,0].cpu() for chunk in values.split(8192)]
            maps.append(torch.cat(scores).reshape(frame['height'],frame['width']).numpy())
    maps = np.stack(maps)
    level,mask = query_mask(maps)
    name = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    directory = REPORT/'queries'/name
    directory.mkdir(parents=True)
    rgb = cv2.imread(frame['path'])
    overlay = cv2.addWeighted(rgb,.55,cv2.applyColorMap((np.clip(maps[level],0,1)*255).astype(np.uint8),cv2.COLORMAP_TURBO),.45,0)
    cv2.imwrite(str(directory/'heatmap.png'),overlay)
    cv2.imwrite(str(directory/'mask.png'),mask*255)
    np.savez_compressed(directory/'scores.npz',maps=maps)
    result = dict(image_id=image_id,image_name=frame['name'],text=text,level=level+1,selected_pixels=int(mask.sum()),
                  checkpoint_step=checkpoint_step,directory=str(directory),protocol=prepared.get('reference_protocol','matched language holdout'),
                  mask_protocol='upstream filter30, per-map rescale, threshold0.4, majority7; no GT needed')
    write_json(directory/'query.json',result)
    print(json.dumps(result,indent=2))


def legacy_evaluate(config, prepared):
    """Same trained legacy fields, change evaluation only, retain all score maps."""
    from langsplat_reference_metrics import evaluate_maps,load_metrics
    from minicolmap.semantics.encoders import ClipEncoder
    from minicolmap.semantics.pipeline import prediction_map,load_autoencoder,load_features
    from minicolmap.semantics.rendering import FrozenGaussians
    source = load_metrics(UP)
    clip = ClipEncoder(config)
    frames = {f['image_id']:f for f in prepared['frames']}
    for name,path in (('original-mlp3','semantic_waldo.json'),('repaired-pca224','semantic_waldo_repaired.json')):
        cfg = load_config(ROOT/'configs'/path)
        proj = project(cfg)
        renderer = FrozenGaussians(cfg,proj)
        decoder = load_autoencoder(cfg,proj)
        latents = load_features(cfg,proj,'distilled')
        directory = REPORT/'paired-protocol-3d'/name
        directory.mkdir(parents=True,exist_ok=True)
        rows = []
        with torch.inference_mode():
            for annotation in prepared['annotations']:
                i = annotation['image_id']
                if i not in prepared['splits']['dev']:
                    continue
                record = json.loads(Path(annotation['annotation']).read_text())
                names = list(dict.fromkeys(o['category'] for o in record['objects']))
                shape = (record['info']['height'],record['info']['width'])
                maps = []
                for text in names:
                    heat,valid,_ = prediction_map(cfg,proj,frames[i],text,'distilled',clip=clip,
                                                 renderer=renderer,decoder=decoder,latents=latents)
                    heat = cv2.resize(heat,(shape[1],shape[0]),interpolation=cv2.INTER_LINEAR)
                    valid = cv2.resize(valid.astype(np.uint8),(shape[1],shape[0]),interpolation=cv2.INTER_NEAREST_EXACT)>0
                    heat[~valid] = 0
                    maps.append(heat)
                maps = np.stack(maps)[None]
                metrics,_ = evaluate_maps(UP,maps,names,annotation['annotation'],source=source)
                rows.append(dict(image_id=i,**metrics))
                np.savez_compressed(directory/f'{i:06d}-maps.npz',maps=maps,names=np.array(names))
                write_json(directory/f'{i:06d}-metrics.json',rows[-1])
        count = sum(len(row['per_query']) for row in rows)
        summary = {protocol:{metric:sum(row[protocol][metric]*len(row['per_query']) for row in rows)/count
                             for metric in ('mean_iou',('bbox_localization_rate' if protocol=='official' else 'polygon_peak_rate'))}
                   for protocol in ('common','official')}
        write_json(directory/'summary.json',dict(variant=name,results=summary,per_view=rows,
                   scope='Identical existing distilled field and score maps; dev only; evaluation/postprocessing changed'))
        progress('legacy-evaluate',variant=name,results=summary)
        del renderer,decoder,latents
        torch.cuda.empty_cache()


def main():
    global WORK,REPORT
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('stage', choices=('preprocess','autoencoder','encode','train','evaluate','legacy-evaluate','query'))
    parser.add_argument('--level',type=int,choices=(1,2,3),default=1)
    parser.add_argument('--steps',type=int,default=30000)
    parser.add_argument('--kind',choices=('teacher','reconstructed','field'),default='field')
    parser.add_argument('--split',choices=('dev','test'),default='dev')
    parser.add_argument('--checkpoint-step',type=int,default=30000)
    parser.add_argument('--protocol',choices=('matched','full-view'),default='matched')
    parser.add_argument('--image-id',type=int)
    parser.add_argument('--text')
    args = parser.parse_args()
    config = load_config(ROOT/'configs/semantic_waldo.json')
    prepared = project(config)
    if args.protocol=='full-view':
        if args.stage=='preprocess':
            parser.error('Full-view mode reuses the completed common reference extraction')
        original = WORK
        WORK = ROOT/'data/langsplat_reference_full_views'
        REPORT = REPORT/'full-view-protocol'
        WORK.mkdir(parents=True,exist_ok=True)
        features = WORK/'language_features'
        if not features.exists():
            features.symlink_to(original/'language_features',target_is_directory=True)
        prepared = {**prepared,'splits':{**prepared['splits'],'train':[f['image_id'] for f in prepared['frames']]},
                    'reference_protocol':'All 187 language views, including evaluation images via SAM/CLIP; no manual labels in fit; seen-view official-style protocol on our frozen RGB, not held-out-view generalization'}
        write_json(REPORT/'split.json',dict(train_ids=prepared['splits']['train'],
                   dev_ids=prepared['splits']['dev'],test_ids=prepared['splits']['test'],
                   scope=prepared['reference_protocol']))
    REPORT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with gpu_session({**config,'workspace':str(REPORT)}):
        seed()
        if args.stage == 'train':
            train(config,prepared,args.level,args.steps)
        elif args.stage == 'evaluate':
            evaluate(config,prepared,args.kind,args.split,args.checkpoint_step)
        elif args.stage == 'legacy-evaluate':
            legacy_evaluate(config,prepared)
        elif args.stage == 'query':
            query(config,prepared,args.image_id,args.text,args.checkpoint_step)
        else:
            globals()[args.stage](config, prepared)


if __name__ == '__main__':
    main()
