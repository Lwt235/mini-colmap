"""Reproducible sequential GPU ablation; each job owns a fresh Python process.

No SfM is rerun. Read-only source maps, original photos and actual training code
are fingerprinted. Existing completed jobs are reused only for identical inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCENES = {
    'temple47': ('outputs/ui_20261018_153919_803ccb/model.json', 'data/middlebury_temple_ring/dataset_all.json', 4),
    'temple312': ('outputs/ui_20261019_084229_6c1c74/model.json', 'data/temple/dataset_all.json', 20),
    'dino321': ('outputs/dino363_repaired_cached_52058b/model.json', 'data/dino/dataset_all.json', 20),
}
VARIANTS = {
    'baseline': {'optimizer_state': 'reset'},
    'adam': {'optimizer_state': 'preserve'},
    'lr': {'optimizer_state': 'preserve', 'lr_mode': 'grouped'},
    'density': {'optimizer_state': 'preserve', 'density_strategy': 'adaptive'},
    'loss': {'optimizer_state': 'preserve', 'loss': 'l1_ssim'},
    'combined': {'optimizer_state': 'preserve', 'lr_mode': 'grouped',
                 'density_strategy': 'adaptive', 'loss': 'l1_ssim'},
}
CODE = [ROOT / 'src/minicolmap/rendering' / name for name in (
    'algorithms.py', 'pipeline.py', 'gaussian_training.py', 'config.py', 'data.py',
    'gaussian_backend.py', 'live.py')]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    from minicolmap.ui.worker import atomic_json
    atomic_json(path, value)


def split_score(rows, ids):
    selected = [row for row in rows if row['image_id'] in ids]
    mse = float(np.mean([row['mse'] for row in selected]))
    return {'views': len(selected), 'psnr_from_mean_mse': -10 * math.log10(max(mse, 1e-12)),
            'mean_view_psnr': float(np.mean([row['psnr'] for row in selected])),
            'ssim': float(np.mean([row['ssim'] for row in selected]))}


def benchmark(output):
    """Local renderer -> readback -> JPEG, excludes network/browser/display."""
    import torch
    from minicolmap.io import load_reconstruction
    from minicolmap.rendering.data import scaled_intrinsics
    from minicolmap.rendering.live import CudaRenderer
    renderer = CudaRenderer(output)
    source = load_reconstruction(output / 'source_model.json')
    checkpoint = torch.load(output / 'checkpoint.pt', weights_only=True, map_location='cpu')
    cameras = []
    for image_id in checkpoint['validation_ids']:
        image, pose = source.images[image_id], source.poses[image_id]
        camera = source.cameras[image.camera_id]
        cameras.append({'width': 1280, 'height': 720, 'R': pose.R.tolist(), 't': pose.t.tolist(),
                        'K': scaled_intrinsics(camera.K, (camera.width, camera.height), (1280, 720)).tolist()})
    measurements = []
    for i in range(50):
        began = time.perf_counter()
        meta, _ = renderer.render(cameras[i % len(cameras)])
        if i >= 10:
            measurements.append({**meta, 'local_frame_ms': 1000 * (time.perf_counter() - began)})
    report = {'scope': '1280x720 local CUDA render + readback + JPEG85; no WebSocket/browser/physical display',
              'warmup_frames': 10, 'measured_frames': 40,
              'local_fps': 1000 / statistics.mean(row['local_frame_ms'] for row in measurements),
              'local_latency_p95_ms': float(np.percentile([row['local_frame_ms'] for row in measurements], 95)),
              'gpu_render_mean_ms': statistics.mean(row['render_ms'] for row in measurements)}
    write(output / 'render_benchmark.json', report)
    return report


def one_job(job_path):
    import torch
    import importlib.metadata
    from minicolmap.rendering.pipeline import train
    from minicolmap.ui.runtime import configure_worker_runtime
    job = json.loads(job_path.read_text(encoding='utf-8'))
    runtime = configure_worker_runtime()
    for path, expected in job['code_sha256'].items():
        if digest(ROOT / path) != expected:
            raise ValueError('Training code changed after the plan was frozen: ' + path)
    output = Path(job['output'])
    def observe(event):
        if event.get('kind') == 'progress' and event.get('completed', 0) % 500 == 0:
            print(json.dumps({'job': job['name'], 'step': event['completed'], 'total': event['total']}), flush=True)
    result = train(job['model'], job['dataset'], output, job['config'], on_event=observe)
    # Validation views are partitioned before running any experiment. Dev is
    # for configuration selection; test remains untouched by gradient training.
    ids = sorted(result['validation_ids'])
    evaluation = {'dev_ids': ids[::2], 'test_ids': ids[1::2],
                  'dev': split_score(result['per_view_metrics'], set(ids[::2])),
                  'test': split_score(result['per_view_metrics'], set(ids[1::2]))}
    write(output / 'ablation_evaluation.json', evaluation)
    performance = benchmark(output)
    record = {'name': job['name'], 'scene': job['scene'], 'variant': job['variant'], 'seed': job['config']['seed'],
              'iterations': job['config']['iterations'], 'downscale': job['config']['image_downscale'],
              'source_fingerprint': job['source_fingerprint'], 'code_sha256': job['code_sha256'],
              'gpu': torch.cuda.get_device_name(), 'torch': torch.__version__,
              'gsplat': importlib.metadata.version('gsplat'), 'runtime': runtime,
              'summary': {key: result[key] for key in ('metrics', 'point_count', 'elapsed_seconds',
                         'peak_cuda_allocated_mb', 'peak_cuda_reserved_mb')},
              'evaluation': evaluation, 'render_benchmark': performance, 'output': str(output)}
    write(output / 'ablation_result.json', record)
    print(json.dumps({'finished': job['name'], 'metrics': result['metrics'], 'points': result['point_count']}), flush=True)


def plan(args):
    from minicolmap.io import load_dataset
    from minicolmap.rendering.config import resolve_render_config
    model_override = getattr(args, 'model', None)
    dataset_override = getattr(args, 'dataset', None)
    if (model_override is not None or dataset_override is not None) and len(args.scenes.split(',')) != 1:
        raise ValueError('--model/--dataset overrides require exactly one --scenes entry')
    code = {path.relative_to(ROOT).as_posix(): digest(path) for path in CODE}
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    for path in CODE:
        saved = report / 'source' / code[path.relative_to(ROOT).as_posix()][:12] / path.name
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            saved.write_bytes(path.read_bytes())
    jobs = []
    for scene in args.scenes.split(','):
        model, dataset, holdout = SCENES[scene]
        model = model_override if model_override is not None else model
        dataset = dataset_override if dataset_override is not None else dataset
        data = load_dataset(ROOT / dataset)
        images = {str(key): digest(path) for key, path in sorted(data['image_paths'].items())}
        fingerprint = {'model_sha256': digest(ROOT / model), 'dataset_sha256': digest(ROOT / dataset),
                       'image_sha256': images}
        for seed in map(int, args.seeds.split(',')):
            for variant in args.variants.split(','):
                gaussian = {'backend': 'gsplat', 'max_gaussians': args.max_gaussians, 'densify_every': 100,
                            'sh_degree': 3, 'sh_degree_interval': 1000, 'densify_start': 500,
                            'densify_stop': min(15000, max(501, args.iterations * 2 // 3)), **VARIANTS[variant]}
                if args.fixed_topology:
                    gaussian.update(densify_every=0, optimizer_state='preserve', density_strategy='legacy')
                config = resolve_render_config({'method': '3dgs', 'iterations': args.iterations,
                    'image_downscale': args.downscale, 'holdout_every': holdout, 'seed': seed,
                    'device': 'cuda', 'background': 'black', 'preview_every': max(500, args.iterations // 6),
                    'gaussian': gaussian})
                key = hashlib.sha256(json.dumps([fingerprint, config, code], sort_keys=True).encode()).hexdigest()[:10]
                name = f'{scene}_{variant}_s{seed}_{key}'
                output = report / 'runs' / name
                job = {'name': name, 'scene': scene, 'variant': variant, 'model': str(ROOT / model),
                       'dataset': str(ROOT / dataset), 'config': config, 'output': str(output),
                       'source_fingerprint': fingerprint, 'code_sha256': code}
                job_file = report / 'jobs' / f'{name}.json'
                job_file.parent.mkdir(exist_ok=True)
                if job_file.exists() and json.loads(job_file.read_text(encoding='utf-8')) != job:
                    raise ValueError('Existing job manifest differs: ' + str(job_file))
                write(job_file, job)
                jobs.append(job_file)
    write(report / 'plan.json', {'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
          'jobs': [str(path) for path in jobs], 'fixed_topology': args.fixed_topology,
          'note': 'Three input sets, TWO object identities. All comparisons paired within each set.'})
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'reports/3dgs-ablation-20261019')
    parser.add_argument('--scenes', default=','.join(SCENES))
    parser.add_argument('--variants', default=','.join(VARIANTS))
    parser.add_argument('--seeds', default='0,1')
    parser.add_argument('--iterations', type=int, default=3000)
    parser.add_argument('--downscale', type=int, default=4)
    parser.add_argument('--max-gaussians', type=int, default=100000)
    parser.add_argument('--model', type=Path, help='Explicit source model for a single selected scene')
    parser.add_argument('--dataset', type=Path, help='Explicit photo manifest for a single selected scene')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--fixed-topology', action='store_true', help='Disable densification in all arms; preserve Adam for a controlled loss-only comparison')
    parser.add_argument('--one-job', type=Path)
    args = parser.parse_args()
    if args.one_job:
        one_job(args.one_job)
        return
    jobs = plan(args)
    print(json.dumps({'jobs': len(jobs), 'report': str(args.report), 'dry_run': args.dry_run}), flush=True)
    if args.dry_run:
        return
    from minicolmap.ui.gpu_lease import acquire, release
    token = acquire(ROOT, args.report)
    try:
        completed = []
        for index, path in enumerate(jobs, 1):
            job = json.loads(path.read_text(encoding='utf-8'))
            result = Path(job['output']) / 'ablation_result.json'
            print(json.dumps({'index': index, 'total': len(jobs), 'name': job['name'], 'reuse': result.exists()}), flush=True)
            if not result.exists():
                env = dict(os.environ, PYTHONIOENCODING='utf-8')
                with path.with_suffix('.log').open('w', encoding='utf-8') as log:
                    subprocess.run([sys.executable, str(Path(__file__).resolve()), '--one-job', str(path)],
                                   cwd=ROOT, env=env, check=True, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            completed.append(json.loads(result.read_text(encoding='utf-8')))
            write(args.report / 'results.json', completed)
            write(args.report / 'progress.json', {'completed': index, 'total': len(jobs), 'last': job['name']})
        print(json.dumps({'completed_jobs': len(completed)}), flush=True)
    finally:
        release(ROOT, token)


if __name__ == '__main__':
    main()
