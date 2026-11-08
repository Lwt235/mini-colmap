"""Read-only VJP parity on rotated, anisotropic, trained Gaussians."""
from __future__ import annotations

import json
import os
import time

import numpy as np
import torch

import experiment_official_3dgs as base
import experiment_official_3dgs_fast as fused
from minicolmap.rendering import pipeline as pipe
from minicolmap.rendering.data import load_training_scene
from minicolmap.rendering.gaussian_training import photometric_loss


def main():
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '7':
        raise RuntimeError('GPU 7 only')
    config = base.config_for({}, 0, 1)
    scene = load_training_scene(base.ORIGINAL / 'source_model.json', base.DATASET, config)
    checkpoint = torch.load(base.ORIGINAL / 'checkpoint.pt', map_location='cuda', weights_only=True)
    indices = torch.as_tensor(np.random.default_rng(41).choice(len(checkpoint['state']['means']),
                              4096, replace=False), device='cuda')
    state = {key: value[indices].detach().clone().requires_grad_()
             for key, value in checkpoint['state'].items()}
    records = {}
    started = time.time()
    for image_id in (20, 60, 162):
        frame = scene.frames[image_id]
        target = torch.as_tensor(frame.rgb, device='cuda')
        record = {'width': frame.width, 'height': frame.height,
                  **{key: torch.as_tensor(getattr(frame,key), device='cuda', dtype=torch.float32)
                     for key in ('K','R','t')}}
        images, gradients = [], []
        for renderer in (pipe._render, fused.fast_render):
            rgb = renderer(None, state, record, config, torch, 'cuda', sh_degree=3)
            images.append(rgb.detach())
            photometric_loss(rgb, target, config).backward()
            gradients.append({key: value.grad.detach().clone() for key, value in state.items()})
            for value in state.values():
                value.grad = None
        records[image_id] = {'image_max_abs': float((images[0]-images[1]).abs().max()),
                            'gradients': {key: {
                                'max_abs': float((gradients[0][key]-gradients[1][key]).abs().max()),
                                'relative_l2': float((gradients[0][key]-gradients[1][key]).norm()/
                                                     gradients[0][key].norm().clamp_min(1e-30)),
                                'reference_norm': float(gradients[0][key].norm())}
                                for key in state}}
    scales = state['log_scales'].detach().exp()
    result = {'samples': 4096, 'seed':41, 'anisotropic_fraction': float((scales.amax(-1)/scales.amin(-1) > 1.1).float().mean()),
              'started': started, 'ended': time.time(), 'views': records,
              'note': 'Short read-only VJP probe; may overlap the ongoing initialization-scale job. No optimizer or weight update.'}
    base.write(base.REPORT / 'trained-gradient-parity.json', result)
    print(json.dumps(result,indent=2), flush=True)
    if any(values['relative_l2'] > .005 for view in records.values() for values in view['gradients'].values()):
        raise RuntimeError('Trained anisotropic VJP parity failed; investigate fast transport')


if __name__ == '__main__':
    main()
