"""Matched teatime seed-0 tests, including the unedited Graphdeco train loop.

Default prepares a frozen manifest. --run executes four independent workers,
one idle physical GPU at a time. Existing models and production code are read-only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'outputs/ui_3dgs_20261024_112609_a1d94a'
DATASET = ROOT / 'data/lerf_ovs/teatime/dataset_all.json'
UPSTREAM = ROOT / '.cache/gaussian-upstream-audit'
REPORT = ROOT / 'reports/teatime-official-tests-20261026'
VARIANTS = ['official_entry', 'baseline', 'sh_rest_lr', 'statistics']


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    from minicolmap.ui.worker import atomic_json
    atomic_json(Path(path), value)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def prepare(report):
    config = read(SOURCE / 'config.json')
    scene = read(SOURCE / 'training_scene.json')
    dataset = read(DATASET)
    paths = list((ROOT / 'src/minicolmap/rendering').glob('*.py'))
    paths += [Path(__file__), ROOT / 'scripts/experiment_official_3dgs.py',
              ROOT / 'scripts/experiment_official_3dgs_fast.py',
              ROOT / 'scripts/report_teatime_official_tests.py',
              ROOT / 'scripts/check_lerf_rgb_renderers.py',
              ROOT / 'src/minicolmap/ui/gpu_lease.py']
    paths += [UPSTREAM / name for name in ('train.py', 'scene/gaussian_model.py',
              'arguments/__init__.py', 'utils/general_utils.py', 'utils/loss_utils.py',
              'utils/graphics_utils.py', 'utils/sh_utils.py', 'gaussian_renderer/network_gui.py',
              'gaussian_renderer/__init__.py', 'submodules/fused-ssim/fused_ssim/__init__.py',
              'build.json')]
    paths += list((UPSTREAM / 'site').rglob('*.so'))
    paths += [SOURCE / name for name in ('source_model.json', 'checkpoint.pt', 'config.json', 'training_scene.json')]
    paths += [DATASET] + [DATASET.parent / item['path'] for item in dataset['images']]
    plan = {
        'schema_version': 1, 'variants': VARIANTS, 'config': config,
        'train_ids': scene['train_ids'], 'validation_ids': scene['validation_ids'],
        'normalization': scene['normalization'],
        'sha256': {str(p.relative_to(ROOT)): sha(p) for p in sorted(set(paths))},
        'upstream_revision': read(UPSTREAM / 'build.json')['revision'],
        'protocol': 'Native 988x730, original 169/8 split, white background, 30000 iterations, seed0; development views, not an independent test.',
        'official_adapter': 'Unedited train.py.training; Scene/camera IO and training_report monitoring adapted. Stock GaussianModel, losses, optimization and topology. AA/exposure/depth disabled as in classic RGB-only comparison.',
        'project_adapter': 'Existing controlled loop and fused SH/covariance transport, with a fresh baseline and native-size forward/gradient parity. Only the historical GPU7 guard is generalized in memory; the source file is not edited.',
        'primary_evaluation': 'All checkpoints normalized and rendered by the SAME fused gsplat adapter, original eight GT grids; clipped float PSNR (both aggregations), valid SSIM, saved 8-bit RGB LPIPS VGG [-1,1]. Native training-render metrics are retained separately.',
    }
    plan['signature'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    plan['outputs'] = {name: str(report / 'runs' / name) for name in VARIANTS}
    report.mkdir(parents=True, exist_ok=True)
    if (report / 'plan.json').exists() and read(report / 'plan.json') != plan:
        raise ValueError('Frozen plan differs; use a new --report directory')
    write(report / 'plan.json', plan)
    return plan


def verify(plan):
    for name, digest in plan['sha256'].items():
        if sha(ROOT / name) != digest:
            raise RuntimeError('Frozen input/code changed: ' + name)


def load_base(report):
    """Generalize only an obsolete GPU guard; preserve historical source files."""
    path = ROOT / 'scripts/experiment_official_3dgs.py'
    source = path.read_text()
    old = "if os.environ.get('CUDA_VISIBLE_DEVICES') != '7' or torch.cuda.device_count() != 1:"
    new = "if os.environ.get('CUDA_VISIBLE_DEVICES') != os.environ.get('MINICOLMAP_EXPERIMENT_GPU') or torch.cuda.device_count() != 1:"
    if source.count(old) != 1:
        raise RuntimeError('Expected GPU guard no longer matches')
    module = types.ModuleType('experiment_official_3dgs')
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(source.replace(old, new, 1), str(path), 'exec'), module.__dict__)
    module.ORIGINAL, module.DATASET, module.REPORT = SOURCE, DATASET, report
    module.POLICIES['official_entry'] = {}
    return module


def fixed_scene(plan):
    import numpy as np
    from minicolmap.rendering.data import load_training_scene
    scene = load_training_scene(SOURCE / 'source_model.json', DATASET, plan['config'])
    norm = plan['normalization']
    if (scene.train_ids != plan['train_ids'] or scene.validation_ids != plan['validation_ids']
            or not np.allclose(scene.center, norm['center'], atol=1e-12, rtol=0)
            or not np.isclose(scene.scale, norm['scale'], atol=1e-12, rtol=0)):
        raise RuntimeError('Split/normalization differs from completed teatime run')
    return scene


def official_train(base, plan, output):
    import numpy as np
    import torch
    from minicolmap.rendering import pipeline
    from minicolmap.rendering.gaussian_training import ssim_image
    scene = fixed_scene(plan)
    GaussianModel, renderer, PointCloud, projection, opt = base.upstream_import()
    cameras = {}
    for image_id, frame in scene.frames.items():
        world_frame = copy.copy(frame)
        world_frame.t = frame.t * scene.scale - frame.R @ scene.center
        camera = base.camera_for(world_frame, projection)
        camera.uid = image_id
        camera.original_image = torch.as_tensor(frame.rgb, device='cuda').permute(2, 0, 1).contiguous()
        camera.alpha_mask, camera.depth_reliable = None, False
        cameras[image_id] = camera
    models, history, curve = [], [], []
    class MatchedScene:
        def __init__(self, dataset, gaussians):
            self.gaussians, self.cameras_extent, self.model_path = gaussians, scene.scale, str(output)
            gaussians.create_from_pcd(PointCloud(scene.points * scene.scale + scene.center,
                                                scene.colors, np.zeros_like(scene.points)),
                                      list(cameras.values()), scene.scale)
            original = gaussians.densify_and_prune
            def observed(*args, **kwargs):
                before = len(gaussians.get_xyz)
                counts = gaussians.denom
                statistics = {'observations_max': float(counts.max()),
                              'observations_mean': float(counts.mean())}
                original(*args, **kwargs)
                history.append({'step': current[0], 'before': before,
                                'after': len(gaussians.get_xyz), **statistics})
            gaussians.densify_and_prune = observed
            models.append(gaussians)
        def getTrainCameras(self):
            return [cameras[i] for i in scene.train_ids]
        def getTestCameras(self):
            return [cameras[i] for i in scene.validation_ids]
    sys.modules['scene'].Scene = MatchedScene
    sys.modules['scene'].GaussianModel = GaussianModel
    spec = importlib.util.spec_from_file_location('teatime_unedited_upstream_training', UPSTREAM / 'train.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    # Instrument reporting only; no edits to training/optimizer/density code.
    entry.prepare_output_and_logger = lambda dataset: None
    pipe = types.SimpleNamespace(debug=False, antialiasing=False,
                                 convert_SHs_python=False, compute_cov3D_python=False)
    current = [0]
    began = time.perf_counter()
    eval_seconds = 0.
    def report(tb, iteration, Ll1, loss, *unused):
        nonlocal eval_seconds
        current[0] = iteration
        model = models[0]
        if iteration == 1 or iteration % 100 == 0:
            value = float(loss)
            if not math.isfinite(value):
                raise RuntimeError('Nonfinite official loss')
            write(output / 'progress.json', {'variant': 'official_entry', 'step': iteration,
                  'budget': 30000, 'loss': value, 'gaussians': len(model.get_xyz),
                  'wall_seconds': time.perf_counter() - began,
                  'scope': 'Before this iteration topology/optimizer, as in official train.py'})
        if iteration not in (1000, 3000, 7000, 15000, 30000):
            return
        started = time.perf_counter()
        rows = []
        with torch.no_grad():
            for image_id in scene.validation_ids:
                rgb = renderer(cameras[image_id], model, pipe, torch.ones(3, device='cuda'),
                               use_trained_exp=False, separate_sh=entry.SPARSE_ADAM_AVAILABLE)['render'].permute(1, 2, 0)
                target = cameras[image_id].original_image.permute(1, 2, 0)
                mse = float((rgb - target).square().mean())
                rows.append({'image_id': image_id, 'mse': mse,
                             'psnr': -10 * math.log10(max(mse, 1e-12)),
                             'ssim_valid': float(ssim_image(rgb, target))})
                pipeline._write_image(output / f'training-render-validation/{image_id}-step-{iteration}.png', rgb.cpu().numpy())
        record = {'step': iteration, 'gaussians': len(model.get_xyz), 'per_view': rows,
                  'psnr': -10 * math.log10(np.mean([r['mse'] for r in rows])),
                  'mean_view_psnr': float(np.mean([r['psnr'] for r in rows])),
                  'ssim_valid': float(np.mean([r['ssim_valid'] for r in rows]))}
        curve.append(record)
        write(output / 'curve.json', curve)
        write(output / 'density.json', history)
        print(json.dumps({'official_evaluation': record}), flush=True)
        eval_seconds += time.perf_counter() - started
    entry.training_report = report
    dataset = types.SimpleNamespace(sh_degree=3, white_background=True, train_test_exp=False,
                                    model_path=str(output), source_path=str(DATASET.parent))
    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    torch.cuda.reset_peak_memory_stats()
    # Untouched official training loop, full 30k budget, no prefix abort or resume.
    entry.training(dataset, opt, pipe, [], [], [], None, -1)
    torch.cuda.synchronize()
    model = models[0]
    state = dict(base.export_state(model))
    state['means'] = (state['means'] - torch.as_tensor(scene.center, device='cuda')) / scene.scale
    state['log_scales'] = state['log_scales'] - math.log(scene.scale)
    state['means'] = state['means'].float()
    if any(not bool(torch.isfinite(value).all()) for value in state.values()):
        raise RuntimeError('Official model has nonfinite parameters')
    config = copy.deepcopy(plan['config'])
    config['gaussian'].update(adam_eps=1e-15, sh_rest_lr_ratio=.05, split_mode='stochastic',
                              max_gaussians=max(1000000, len(model.get_xyz)))
    pipeline._save_checkpoint(output, config, scene, None, state, 30000, torch)
    write(output / 'training-policy.json', {'loop': 'unedited pinned train.py.training',
          'train_py_sha256': sha(UPSTREAM / 'train.py'), 'optimization': vars(opt),
          'config_role': 'Project checkpoint config is a rendering descriptor; exact official training policy is this sidecar, not a claim that all policies map to project JSON.'})
    result = {'status': 'complete', 'variant': 'official_entry', 'steps': 30000, 'seed': 0,
              'downscale': 1, 'metrics': curve[-1], 'train_seconds': time.perf_counter() - began - eval_seconds,
              'eval_seconds': eval_seconds, 'peak_allocated_mb': torch.cuda.max_memory_allocated() / 2**20,
              'peak_reserved_mb': torch.cuda.max_memory_reserved() / 2**20}
    write(output / 'training-result.json', result)
    return result


def common_evaluation(plan, output, result, fast):
    import numpy as np
    import torch
    from minicolmap.rendering import pipeline
    from minicolmap.rendering.gaussian_training import ssim_image
    scene = fixed_scene(plan)
    checkpoint = torch.load(output / 'checkpoint.pt', map_location='cuda', weights_only=True)
    state, rows = checkpoint['state'], []
    config = checkpoint['config']
    if (output / 'validation').exists():
        (output / 'validation').rename(output / 'training-render-validation-project')
    with torch.no_grad():
        for image_id in scene.validation_ids:
            frame = scene.frames[image_id]
            rgb = fast.fast_render(None, state, pipeline._frame_record(frame), config, torch, 'cuda', sh_degree=3)
            target = torch.as_tensor(frame.rgb, device='cuda')
            raw_mse = float((rgb - target).square().mean())
            rgb = rgb.clamp(0, 1)
            mse = float((rgb - target).square().mean())
            rows.append({'image_id': image_id, 'mse': mse, 'psnr': -10 * math.log10(max(mse, 1e-12)),
                         'ssim_valid': float(ssim_image(rgb, target)), 'raw_mse': raw_mse})
            pipeline._write_image(output / f'validation/{image_id}.png', rgb.cpu().numpy())
            pipeline._write_image(output / f'validation/{image_id}-reference.png', frame.rgb)
    metrics = {'step': 30000, 'gaussians': len(state['means']), 'per_view': rows,
               'psnr': -10 * math.log10(np.mean([r['mse'] for r in rows])),
               'mean_view_psnr': float(np.mean([r['psnr'] for r in rows])),
               'ssim_valid': float(np.mean([r['ssim_valid'] for r in rows]))}
    result['training_renderer_metrics'] = result['metrics']
    result['metrics'], result['evaluation_protocol'] = metrics, plan['primary_evaluation']
    result['physical_gpu'] = int(os.environ['CUDA_VISIBLE_DEVICES'])
    result['signature'] = plan['signature']
    result['checkpoint_sha256'] = sha(output / 'checkpoint.pt')
    write(output / 'result.json', result)


def worker(args):
    from check_lerf_rgb_renderers import idle_devices
    from minicolmap.ui import gpu_lease
    plan = read(args.report / 'plan.json')
    verify(plan)
    if str(args.gpu) != os.environ.get('CUDA_VISIBLE_DEVICES') or args.gpu not in idle_devices():
        raise RuntimeError('GPU changed/became busy; CUDA not initialized')
    token = gpu_lease.adopt(ROOT, os.environ.get('MINICOLMAP_QUERY_RESERVATION'))
    try:
        import torch
        torch.set_num_threads(4)
        assert torch.cuda.device_count() == 1
        output = Path(plan['outputs'][args.variant])
        if any((output / name).exists() for name in ('checkpoint.pt', 'result.json', 'progress.json')):
            raise RuntimeError('Refusing to overwrite existing worker output')
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / 'source_model.json', output / 'source_model.json')
        shutil.copy2(SOURCE / 'training_scene.json', output / 'training_scene.json')
        base = load_base(args.report)
        import experiment_official_3dgs_fast as fast
        fast.POLICY = {}
        fixed_scene(plan)
        write(output / 'protocol.json', {'signature': plan['signature'],
              'train_ids': plan['train_ids'], 'validation_ids': plan['validation_ids'],
              'normalization': plan['normalization'], 'physical_gpu': args.gpu})
        if args.variant == 'official_entry':
            result = official_train(base, plan, output)
        else:
            if args.variant == 'baseline':
                fast.parity()
            else:
                parity = read(args.report / 'fused-parity.json')
                if abs(parity['delta_db']) > .002:
                    raise RuntimeError('Missing/invalid native teatime transport parity')
            from minicolmap.rendering import pipeline
            pipeline._render = fast.fast_render
            result = base.train(types.SimpleNamespace(variant=args.variant, seed=0,
                                downscale=1, steps=30000, output=output))
            write(output / 'training-result.json', result)
        common_evaluation(plan, output, result, fast)
    finally:
        gpu_lease.release(ROOT, token)


def run(args, plan):
    from check_lerf_rgb_renderers import idle_devices, guard_workbench
    from minicolmap.ui import gpu_lease
    records = []
    for variant in VARIANTS:
        output = Path(plan['outputs'][variant])
        if (output / 'result.json').exists():
            result = read(output / 'result.json')
            if result.get('signature') != plan['signature']:
                raise RuntimeError('Existing result has a different frozen signature')
            records.append(result)
            continue
        verify(plan)
        guard_workbench()
        free = idle_devices()
        if not free:
            write(args.report / 'execution.json', {'status': 'stopped_all_gpus_busy',
                  'completed': [r['variant'] for r in records], 'pending': VARIANTS[len(records):]})
            subprocess.run([sys.executable, str(ROOT / 'scripts/report_teatime_official_tests.py'),
                            '--report', str(args.report)], check=True, cwd=ROOT)
            return
        gpu = free[0]
        token = gpu_lease.acquire(ROOT, args.report)
        try:
            if gpu not in idle_devices():
                raise RuntimeError('Chosen GPU became busy before launch')
            environment = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu),
                MINICOLMAP_EXPERIMENT_GPU=str(gpu), MINICOLMAP_QUERY_RESERVATION=token,
                OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4',
                TORCH_EXTENSIONS_DIR=str(ROOT / '.cache/torch_extensions'))
            output.mkdir(parents=True, exist_ok=True)
            write(args.report / 'execution.json', {'status': 'running', 'active': variant,
                  'physical_gpu': gpu, 'completed': [r['variant'] for r in records],
                  'signature': plan['signature'], 'started': time.time()})
            print(json.dumps({'starting': variant, 'physical_gpu': gpu}), flush=True)
            with (output / 'worker.log').open('w') as log:
                completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    '--worker', '--report', str(args.report), '--variant', variant,
                    '--gpu', str(gpu)], env=environment, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            if completed.returncode or not (output / 'result.json').exists():
                write(args.report / 'execution.json', {'status': 'failed', 'active': variant,
                      'returncode': completed.returncode, 'completed': [r['variant'] for r in records],
                      'log': str(output / 'worker.log')})
                raise RuntimeError('Worker failed; see ' + str(output / 'worker.log'))
            records.append(read(output / 'result.json'))
            print(json.dumps({'completed': variant, 'metrics': records[-1]['metrics']}), flush=True)
        finally:
            gpu_lease.release(ROOT, token)
    verify(plan)
    write(args.report / 'execution.json', {'status': 'training_completed',
          'completed': [r['variant'] for r in records], 'signature': plan['signature']})
    subprocess.run([sys.executable, str(ROOT / 'scripts/report_teatime_official_tests.py'),
                    '--report', str(args.report)], check=True, cwd=ROOT)
    print('All four 30k training runs and shared-render evaluations completed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=REPORT)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--variant', choices=VARIANTS)
    parser.add_argument('--gpu', type=int)
    args = parser.parse_args()
    args.report = args.report.resolve()
    if args.worker:
        worker(args)
    else:
        plan = prepare(args.report)
        print(json.dumps({'signature': plan['signature'], 'variants': VARIANTS}), flush=True)
        if args.run:
            run(args, plan)


if __name__ == '__main__':
    main()
