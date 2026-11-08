"""Exercise unedited upstream train.py.training through a matched data adapter.

This 1000-step prefix keeps the 30000-step LR schedule. Only Scene I/O and
reporting are adapted; an observer stops at iteration1001 before its update.
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import experiment_official_3dgs as base


def main():
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '7':
        raise RuntimeError('Only physical GPU 7 is authorized')
    from minicolmap.ui import gpu_lease
    base.guard_idle()
    output = base.REPORT / 'official-entry-prefix'
    output.mkdir(exist_ok=True)
    token = gpu_lease.acquire(base.ROOT, output)
    try:
        import numpy as np
        import torch
        from minicolmap.rendering.data import load_training_scene
        config = base.config_for({}, 0, 4)
        source = load_training_scene(base.ORIGINAL / 'source_model.json', base.DATASET, config)
        source.points = source.points * source.scale + source.center
        GaussianModel, renderer, BasicPointCloud, projection, opt = base.upstream_import()
        cameras = {}
        for image_id, frame in source.frames.items():
            frame.t = frame.t * source.scale - frame.R @ source.center
            camera = base.camera_for(frame, projection)
            camera.uid = image_id
            camera.original_image = torch.as_tensor(frame.rgb, device='cuda').permute(2,0,1).contiguous()
            camera.alpha_mask = None
            camera.depth_reliable = False
            cameras[image_id] = camera
        holder = []
        class MatchedScene:
            def __init__(self, dataset, gaussians):
                self.gaussians = gaussians
                self.cameras_extent = source.scale
                self.model_path = str(output)
                gaussians.create_from_pcd(BasicPointCloud(source.points, source.colors, np.zeros_like(source.points)),
                                          list(cameras.values()), source.scale)
                holder.append(self)
            def getTrainCameras(self):
                return [cameras[i] for i in source.train_ids]
            def getTestCameras(self):
                return [cameras[i] for i in source.validation_ids]
        sys.modules['scene'].Scene = MatchedScene
        sys.modules['scene'].GaussianModel = GaussianModel
        spec = importlib.util.spec_from_file_location('pinned_graphdeco_training', base.UPSTREAM / 'train.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        entry.prepare_output_and_logger = lambda dataset: None
        seen = []
        class PrefixComplete(Exception):
            pass
        def observe(tb_writer, iteration, *args):
            if iteration % 100 == 0:
                seen.append({'iteration': iteration, 'gaussians': len(holder[0].gaussians.get_xyz)})
            if iteration == 1001:
                raise PrefixComplete()
        entry.training_report = observe
        dataset = SimpleNamespace(sh_degree=3, white_background=True, train_test_exp=False,
                                  model_path=str(output), source_path=str(base.DATASET.parent))
        pipe = SimpleNamespace(debug=False, antialiasing=False, convert_SHs_python=False, compute_cov3D_python=False)
        random.seed(0)
        np.random.seed(0)
        torch.manual_seed(0)
        torch.cuda.manual_seed_all(0)
        try:
            entry.training(dataset, opt, pipe, [], [], [], None, -1)
        except PrefixComplete:
            pass
        else:
            raise RuntimeError('Prefix observer did not stop upstream entry')
        model = holder[0].gaussians
        rows = []
        with torch.no_grad():
            for image_id in source.validation_ids:
                camera = cameras[image_id]
                rgb = renderer(camera, model, pipe, torch.ones(3, device='cuda'))['render']
                mse = float((rgb-camera.original_image).square().mean())
                rows.append({'image_id': image_id, 'mse': mse})
        result = {'entry': 'pinned train.py.training, unedited optimization/densification loop',
                  'steps_updated': 1000, 'lr_schedule_steps': 30000, 'downscale': 4,
                  'data_adapter': 'same world-coordinate seeds, sRGB, fixed178/9 cameras/order; no Scene random shuffle',
                  'psnr': -10*np.log10(np.mean([row['mse'] for row in rows])),
                  'gaussians': len(model.get_xyz), 'per_view': rows, 'trace': seen,
                  'train_py_sha256': base.sha(base.UPSTREAM / 'train.py')}
        reference = base.REPORT / 'reference-preflight/runs/official_reference-s0-d4-t1000/result.json'
        if reference.exists():
            result['matched_loop_reference_psnr'] = json.loads(reference.read_text())['metrics']['psnr']
            result['delta_from_matched_loop_db'] = float(result['psnr']-result['matched_loop_reference_psnr'])
        base.write(output / 'result.json', result)
        print(json.dumps(result, indent=2), flush=True)
    finally:
        gpu_lease.release(base.ROOT, token)


if __name__ == '__main__':
    main()
