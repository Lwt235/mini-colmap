"""CPU-only numerical audit against a pinned Graphdeco source snapshot.

No production code/checkpoint is modified. Official methods are loaded from the
local snapshot via AST; only explicit CUDA allocation strings become CPU. This
is a policy/math audit, not an official rasterizer or image-quality benchmark.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.request

import cv2
import numpy as np
import torch
from torch import nn

from minicolmap.io import load_reconstruction, save_reconstruction
from minicolmap.models import Camera, ImageRecord, Pose, Reconstruction
from minicolmap.rendering import algorithms as learner
from minicolmap.rendering import pipeline
from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.gaussian_training import DensityStatistics, adaptive_topology

UPSTREAM_REVISION = '54c035f7834b564019656c3e3fcc3646292f727d'


def fetch_reference(directory):
    directory.mkdir(parents=True, exist_ok=True)
    revision = directory / 'revision.json'
    if revision.exists() and json.loads(revision.read_text())['revision'] != UPSTREAM_REVISION:
        raise ValueError('Snapshot revision differs; use another --upstream directory')
    for name in ('train.py', 'scene/gaussian_model.py', 'arguments/__init__.py',
                 'gaussian_renderer/__init__.py', 'utils/loss_utils.py', 'utils/general_utils.py',
                 'utils/sh_utils.py', 'LICENSE.md'):
        path = directory / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f'https://raw.githubusercontent.com/graphdeco-inria/gaussian-splatting/{UPSTREAM_REVISION}/{name}'
            with urllib.request.urlopen(url, timeout=30) as response:
                path.write_bytes(response.read())
    revision.write_text(json.dumps({'repository': 'https://github.com/graphdeco-inria/gaussian-splatting',
                                    'revision': UPSTREAM_REVISION}, indent=2))


class CpuAllocations(ast.NodeTransformer):
    def visit_Constant(self, node):
        if node.value == 'cuda':
            return ast.copy_location(ast.Constant('cpu'), node)
        return node


def extract_functions(path, names, namespace, class_name=None):
    tree = ast.parse(path.read_text())
    body = tree.body
    if class_name:
        body = next(node.body for node in body if isinstance(node, ast.ClassDef) and node.name == class_name)
    functions = [copy.deepcopy(node) for node in body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != set(names):
        raise ValueError('Upstream functions changed; check the pinned source')
    module = CpuAllocations().visit(ast.Module(body=functions, type_ignores=[]))
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)


def official_adapter(source):
    namespace = {'torch': torch, 'nn': nn, 'np': np, 'math': math}
    extract_functions(source / 'utils/general_utils.py', ['build_rotation', 'build_scaling_rotation', 'get_expon_lr_func'], namespace)
    methods = ['cat_tensors_to_optimizer', '_prune_optimizer', 'prune_points', 'densification_postfix',
               'densify_and_split', 'densify_and_clone', 'densify_and_prune', 'add_densification_stats']
    extract_functions(source / 'scene/gaussian_model.py', methods, namespace, 'GaussianModel')

    class Official:
        get_xyz = property(lambda self: self._xyz)
        get_scaling = property(lambda self: self._scaling.exp())
        get_opacity = property(lambda self: self._opacity.sigmoid())

        def __init__(self, state):
            self._xyz = nn.Parameter(state['means'].detach().clone())
            self._scaling = nn.Parameter(state['log_scales'].detach().clone())
            self._rotation = nn.Parameter(state['quaternions'].detach().clone())
            self._opacity = nn.Parameter(state['opacity_logits'].detach().clone()[:, None])
            self._features_dc = nn.Parameter(state['sh_coeffs'].detach().clone()[:, :1])
            self._features_rest = nn.Parameter(state['sh_coeffs'].detach().clone()[:, 1:])
            self.scaling_inverse_activation = torch.log
            self.percent_dense = .01
            count = len(self._xyz)
            self.xyz_gradient_accum = torch.zeros(count, 1)
            self.denom = torch.ones(count, 1)
            self.max_radii2D = torch.zeros(count)
            self.tmp_radii = torch.ones(count)
            self.optimizer = torch.optim.Adam([
                {'params': [getattr(self, field)], 'name': name} for name, field in
                [('xyz', '_xyz'), ('scaling', '_scaling'), ('rotation', '_rotation'),
                 ('opacity', '_opacity'), ('f_dc', '_features_dc'), ('f_rest', '_features_rest')]], eps=1e-15)

    for name in methods:
        setattr(Official, name, namespace[name])
    return Official, namespace


def tiny_state(scales):
    count = len(scales)
    state = learner.initialize_gaussians(torch.zeros(count, 3), torch.full((count, 3), .5), .01, .3)
    with torch.no_grad():
        state['means'][:, 2] = 2
        state['log_scales'].copy_(torch.tensor(scales).log())
    return state


def topology_cases(Official):
    options = resolve_render_config({'gaussian': {'max_gaussians': 100}})['gaussian']
    state = tiny_state([[.12, .08, .04], [.005, .005, .005]])
    statistics = DensityStatistics(state)
    statistics.gradient[:] = torch.tensor([.001, 0.])
    statistics.count[:] = 1
    current, _, _ = adaptive_topology(state, statistics, options)
    official = Official(state)
    official.xyz_gradient_accum[:, 0] = torch.tensor([.001, 0.])
    with torch.no_grad():
        official.densify_and_prune(.0002, .005, 1., 20, torch.ones(2))
    order = {'parent_scale': [.12, .08, .04], 'prune_world_scale': .1,
             'split_child_scale': [.075, .05, .025], 'current_output_count': len(current['means']),
             'official_output_count': len(official._xyz),
             'explanation': 'Current prunes the large parent before splitting; upstream splits first and keeps two children below .1.'}
    assert order['current_output_count'] == 1 and order['official_output_count'] == 3

    state = tiny_state([[.02, .015, .01], [.005, .005, .005]])
    statistics = DensityStatistics(state)
    statistics.gradient[:] = torch.tensor([.001, 0.])
    statistics.count[:] = 1
    capped_options = {**options, 'max_gaussians': 2}
    capped, _, flags = adaptive_topology(state, statistics, capped_options)
    official = Official(state)
    official.xyz_gradient_accum[:, 0] = torch.tensor([.001, 0.])
    with torch.no_grad():
        official.densify_and_prune(.0002, .005, 1., None, torch.ones(2))
    cap = {'capacity': 2, 'current_count': len(capped['means']), 'current_new_children': int(flags.sum()),
           'official_uncapped_count': len(official._xyz), 'mechanism': 'No free capacity means no split, even with high gradients.'}
    assert cap['current_new_children'] == 0 and cap['official_uncapped_count'] == 3

    count = 512
    state = tiny_state([[.02, .01, .005]] * count)
    statistics = DensityStatistics(state)
    statistics.gradient[:] = .001
    statistics.count[:] = 1
    torch.manual_seed(0)
    current, sources, _ = adaptive_topology(state, statistics, {**options, 'max_gaussians': 2*count})
    offsets = current['means'] - state['means'][sources]
    official = Official(state)
    torch.manual_seed(0)
    with torch.no_grad():
        official.densify_and_split(torch.full((count, 1), .001), .0002, 1.)
    official_offsets = official._xyz - state['means'].repeat(2, 1)
    axis = {'current_minor_axis_nonzero_offsets': int(torch.count_nonzero(offsets[:, 1:])),
            'official_minor_axis_nonzero_offsets': int(torch.count_nonzero(official_offsets[:, 1:])),
            'current_normalized_offset_std': (offsets / state['log_scales'].exp()[sources]).std(0).tolist(),
            'official_normalized_offset_std': (official_offsets / state['log_scales'].exp().repeat(2, 1)).std(0).tolist(),
            'scope': 'Synthetic offset coverage, not a trained-image quality measurement'}
    assert axis['current_minor_axis_nonzero_offsets'] == 0
    return {'prune_before_split': order, 'capacity_blocks_refinement': cap, 'split_axis_coverage': axis}


def trace_real_pipeline(directory):
    directory.mkdir(parents=True, exist_ok=True)
    camera = Camera(1, 8, 6, [[10, 0, 3.5], [0, 10, 2.5], [0, 0, 1]])
    images, poses, records = {}, {}, []
    for image_id in (1, 2, 3):
        name = f'{image_id}.png'
        cv2.imwrite(str(directory / name), np.full((6, 8, 3), 102, np.uint8))
        images[image_id] = ImageRecord(image_id, name, 1, np.empty((0, 2)))
        poses[image_id] = Pose(np.eye(3), [image_id*.2, 0., 0.])
        records.append({'id': image_id, 'name': name, 'path': name, 'camera_id': 1})
    model = directory / 'model.json'
    save_reconstruction(Reconstruction({1: camera}, images, poses, {}), model)
    dataset = directory / 'dataset.json'
    dataset.write_text(json.dumps({'cameras': [{'id': 1, 'width': 8, 'height': 6, 'K': camera.K.tolist()}], 'images': records}))
    # Need two sparse seeds for a valid real data-loading/training path.
    from minicolmap.models import Point3D
    reconstruction = load_reconstruction(model)
    reconstruction.points = {i: Point3D(i, xyz, [102]*3, ()) for i, xyz in
                             [(1, [-.1, 0., 2.]), (2, [.1, 0., 2.])]}  # no supervised matches required here
    save_reconstruction(reconstruction, model)
    captured = []
    original_render, original_topology = pipeline._render, pipeline.adaptive_topology

    def protocol_render(network, state, frame, config, backend, device, *, training_info=None, **kwargs):
        means2d = state['means'][:, :2] * 1.
        if training_info is not None:
            means2d.retain_grad()
            training_info.update(means2d=means2d, radii=torch.ones(len(means2d)),
                                 width=frame['width'], height=frame['height'], indices=torch.arange(len(means2d)))
        return (means2d.sum() * 0 + .4).expand(frame['height'], frame['width'], 3)

    def protocol_topology(state, statistics, options):
        captured.append({'visible_count_per_seed': statistics.count.tolist(),
                         'scope': 'Control-flow fixture; constant differentiable image, no quality claim'})
        sources = torch.arange(len(state['means']))
        return {key: value.detach().clone() for key, value in state.items()}, sources, torch.zeros(len(sources), dtype=torch.bool)

    config = resolve_render_config({'method': '3dgs', 'iterations': 600, 'holdout_every': 3,
        'preview_every': 600, 'gaussian': {'density_strategy': 'adaptive', 'densify_start': 500,
        'densify_stop': 1000, 'densify_every': 100, 'opacity_reset_every': 0}})
    pipeline._render, pipeline.adaptive_topology = protocol_render, protocol_topology
    try:
        pipeline.train(model, dataset, directory / 'run', config)
    finally:
        pipeline._render, pipeline.adaptive_topology = original_render, original_topology
    assert captured[0]['visible_count_per_seed'] == [1., 1.]
    assert captured[1]['visible_count_per_seed'] == [100., 100.]
    return {'actual_pipeline_first_growth_step': 500, 'actual_pipeline_first_statistics_steps': 1,
            'second_growth_statistics_steps': 100, 'official_first_growth_step': 600,
            'official_first_statistics_steps': 600, 'captured': captured,
            'additional_issue': 'Current resets density statistics every 100 steps even when no topology is applied.'}


def math_parity(source, namespace):
    sh = {'torch': torch}
    exec(compile((source / 'utils/sh_utils.py').read_text(), str(source / 'utils/sh_utils.py'), 'exec'), sh)
    torch.manual_seed(0)
    q = torch.randn(128, 4)
    scales = torch.randn(128, 3).exp()*.01
    official_l = namespace['build_scaling_rotation'](scales, q)
    covariance_error = float((learner.gaussian_covariance(scales.log(), q) - official_l @ official_l.transpose(-1, -2)).abs().max())
    coefficients = torch.randn(128, 16, 3)*.1
    directions = torch.nn.functional.normalize(torch.randn(128, 3), dim=-1)
    sh_errors = []
    for degree in range(4):
        expected = (sh['eval_sh'](degree, coefficients.transpose(1, 2), directions) + .5).clamp_min(0)
        sh_errors.append(float((learner.evaluate_sh(coefficients, directions, degree) - expected).abs().max()))
    assert covariance_error < 1e-6 and max(sh_errors) < 1e-6
    schedule = namespace['get_expon_lr_func'](.00016, .0000016, lr_delay_mult=.01, max_steps=30000)
    lr_errors = [abs(float(schedule(step)) - .00016*(.01**(step/30000))) for step in (0, 1, 500, 15000, 30000)]
    return {'covariance_max_abs_error': covariance_error, 'sh_max_abs_error_by_degree': sh_errors,
            'normalized_position_lr_max_abs_error': max(lr_errors),
            'delay_note': 'Upstream lr_delay_steps defaults to 0, so delay_mult alone does not apply warm-up.'}


def real_run_evidence(run):
    config = resolve_render_config(json.loads((run / 'config.json').read_text()))
    density = json.loads((run / 'density.json').read_text())
    summary = json.loads((run / 'summary.json').read_text())
    cap = config['gaussian']['max_gaussians']
    capped = next(row['step'] for row in density if row['after'] == cap)
    png_errors = []
    for row in summary['per_view_metrics']:
        image_id = row['image_id']
        a = cv2.imread(str(run / f'validation/image-{image_id}.png')).astype(np.float64)/255
        b = cv2.imread(str(run / f'previews/reference-{image_id}.png')).astype(np.float64)/255
        png_errors.append(float(np.mean((a-b)**2)))
    model = load_reconstruction(run / 'source_model.json')
    points = np.array([p.xyz for p in model.points.values()])
    scene = json.loads((run / 'training_scene.json').read_text())
    normalized = (points-np.array(scene['normalization']['center']))/scene['normalization']['scale']
    from scipy.spatial import cKDTree
    distances = cKDTree(normalized).query(normalized, k=4)[0][:, 1:]
    raw_scales = np.sqrt(np.mean(distances**2, axis=1))
    return {'checkpoint_sha256': hashlib.sha256((run / 'checkpoint.pt').read_bytes()).hexdigest(),
            'iterations': config['iterations'], 'density_stop': config['gaussian']['densify_stop'],
            'first_capacity_step': capped, 'steps_after_density_stop': config['iterations']-config['gaussian']['densify_stop'],
            'eps': config['gaussian']['adam_eps'], 'rest_sh_lr': config['learning_rate']*2.5*config['gaussian']['sh_rest_lr_ratio'],
            'dc_lr': config['learning_rate']*2.5,
            'knn_fraction_above_cap': float(np.mean(raw_scales>config['gaussian']['initial_scale'])),
            'raw_knn_scale_quantiles': np.quantile(raw_scales,[0,.1,.5,.9,.99,1]).tolist(),
            'float_mse_psnr': summary['metrics']['psnr'], 'clipped_png_mse_psnr': float(-10*np.log10(np.mean(png_errors))),
            'mean_view_psnr': float(np.mean([r['psnr'] for r in summary['per_view_metrics']])),
            'train_views': len(summary['train_ids']), 'validation_views': len(summary['validation_ids'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', type=Path, default=Path('.cache/gaussian-upstream-audit'))
    parser.add_argument('--run', type=Path, default=Path('outputs/ui_3dgs_20261019_230921_797f2a'))
    parser.add_argument('--output', type=Path, default=Path('reports/3dgs-upstream-audit-20261020'))
    parser.add_argument('--fetch', action='store_true', help='Fetch missing files from the pinned official revision')
    args = parser.parse_args()
    torch.set_num_threads(4)
    if args.fetch:
        fetch_reference(args.upstream)
    Official, namespace = official_adapter(args.upstream)
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = Path('.cache') / ('upstream-trace-' + os.urandom(6).hex())
    adam = []
    for gradient in (1e-12, 1e-10, 1e-8, 1e-6):
        values = []
        for eps in (1e-8, 1e-15):
            parameter = nn.Parameter(torch.zeros(1))
            optimizer = torch.optim.Adam([parameter], lr=.001, eps=eps)
            parameter.grad = torch.tensor([gradient]);optimizer.step()
            values.append(abs(float(parameter.detach())))
        adam.append({'gradient': gradient, 'legacy_update': values[0], 'official_update': values[1], 'ratio': values[0]/values[1]})
    result = {'upstream': json.loads((args.upstream/'revision.json').read_text()),
              'scope': 'CPU policy/math fixtures and real recorded run. No official CUDA trainer or quality A/B training executed.',
              'topology': topology_cases(Official), 'training_statistics': trace_real_pipeline(fixture),
              'math': math_parity(args.upstream, namespace), 'real_run': real_run_evidence(args.run), 'adam_first_step': adam,
              'source_fingerprints': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  [Path('src/minicolmap/rendering')/name for name in ('algorithms.py','pipeline.py','data.py','gaussian_training.py','config.py')]},
              'upstream_source_fingerprints': {str(p.relative_to(args.upstream)): hashlib.sha256(p.read_bytes()).hexdigest()
                                               for p in args.upstream.rglob('*.py')},
              'cuda_initialized': torch.cuda.is_initialized()}
    assert not result['cuda_initialized']
    (args.output/'audit.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))
    print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__ == '__main__':
    main()
