"""Cross-render frozen original/Graphdeco models with both CUDA backends.

Same 9 native cameras and clipping/near convention. Separates a forward-render
difference from differences learned during training. No weight changes.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import experiment_official_3dgs as base
import experiment_official_3dgs_fast as fused


def main():
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '7':
        raise RuntimeError('Only physical GPU 7 is authorized')
    from minicolmap.ui import gpu_lease
    base.guard_idle()
    destination = base.REPORT / 'cross-render'
    destination.mkdir(exist_ok=True)
    token = gpu_lease.acquire(base.ROOT, destination)
    try:
        import numpy as np
        import torch
        from minicolmap.rendering import pipeline as pipe
        from minicolmap.rendering.data import load_training_scene
        from minicolmap.rendering.gaussian_training import ssim_image
        config = base.config_for({}, 0, 1)
        scene = load_training_scene(base.ORIGINAL / 'source_model.json', base.DATASET, config)
        _, official_render, _, projection, _ = base.upstream_import()
        options = base.types.SimpleNamespace(debug=False, antialiasing=False,
                                            compute_cov3D_python=False, convert_SHs_python=False)
        cameras = {}
        records = {}
        for image_id in scene.validation_ids:
            frame = base.copy.deepcopy(scene.frames[image_id])
            frame.t = frame.t * scene.scale - frame.R @ scene.center
            cameras[image_id] = base.camera_for(frame, projection)
            records[image_id] = {'width': frame.width, 'height': frame.height,
                                **{key: torch.as_tensor(getattr(frame,key),device='cuda',dtype=torch.float32)
                                   for key in ('K','R','t')}}
        center = torch.as_tensor(scene.center, device='cuda', dtype=torch.float32)
        background = torch.ones(3, device='cuda')
        fused.POLICY = {'near_plane': .2, 'rgb_clamp': True, 'official_model': True}
        sources = {'original': base.ORIGINAL / 'checkpoint.pt',
                   'official': base.REPORT / 'native/runs/official_reference-s0-d1-t30000/checkpoint.pt'}
        results = {}
        for source, path in sources.items():
            checkpoint = torch.load(path, weights_only=True, map_location='cuda')
            if (checkpoint['validation_ids'] != scene.validation_ids or
                    not np.isclose(checkpoint['normalization']['scale'], scene.scale)):
                raise ValueError('Camera protocol differs')
            state = checkpoint['state']
            # Restore world coordinates ONCE, and use those same tensors for
            # both renderers. Activation and SH/projection work stay inside the
            # measured render call; camera tensors are resident for both APIs.
            state['means'] = state['means'] * scene.scale + center
            state['log_scales'] = state['log_scales'] + math.log(scene.scale)
            for backend in ('gsplat', 'graphdeco'):
                rows, timings = [], []
                with torch.no_grad():
                    for repeat in range(4):
                        for image_id in scene.validation_ids:
                            frame = scene.frames[image_id]
                            a, b = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                            a.record()
                            if backend == 'gsplat':
                                rgb = fused.fast_render(None, state, records[image_id], config,
                                                        torch, 'cuda', sh_degree=3).clamp(0, 1)
                            else:
                                pc = base.official_proxy(state, 3)
                                rgb = official_render(cameras[image_id], pc, options, background)['render'].permute(1, 2, 0)
                            b.record()
                            b.synchronize()
                            if repeat:
                                timings.append(a.elapsed_time(b))
                            else:
                                target = torch.as_tensor(frame.rgb, device='cuda')
                                mse = float((rgb-target).square().mean())
                                rows.append({'image_id': image_id, 'mse': mse,
                                             'psnr': -10*math.log10(max(mse, 1e-12)),
                                             'ssim_valid': float(ssim_image(rgb, target))})
                                pipe._write_image(destination / f'{source}-{backend}-{image_id}.png', rgb.cpu().numpy())
                result = {'source_checkpoint': str(path), 'checkpoint_sha256': base.sha(path),
                          'gaussians': len(state['means']), 'backend': backend,
                          'psnr': -10*math.log10(max(float(np.mean([row['mse'] for row in rows])), 1e-12)),
                          'ssim_valid': float(np.mean([row['ssim_valid'] for row in rows])),
                          'render_gpu_ms': float(np.mean(timings)), 'per_view': rows}
                results[f'{source}_{backend}'] = result
                base.write(destination / 'results.json', {'scope': 'native9; clip[0,1]; physical-world near .2; immutable weights',
                                                          'results': results})
                print(source, backend, result['psnr'], result['render_gpu_ms'], flush=True)
    finally:
        gpu_lease.release(base.ROOT, token)


if __name__ == '__main__':
    main()
