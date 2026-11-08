"""Semantic counterexamples for the independent ablation machinery (CPU)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch


spec = importlib.util.spec_from_file_location('official_experiment', Path(__file__).resolve().parents[2] /
                                              'scripts/experiment_official_3dgs.py')
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


@pytest.fixture(autouse=True)
def synthetic_original(tmp_path, monkeypatch):
    """Use the public baseline config, not an untracked historical output run."""
    repository = Path(__file__).resolve().parents[2]
    config = json.loads((repository / 'configs/3dgs_lerf.json').read_text(encoding='utf-8'))
    config['iterations'] = 30000
    config['gaussian']['max_gaussians'] = 1000000
    original = tmp_path / 'original'
    original.mkdir()
    (original / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(experiment, 'ORIGINAL', original)


def fixture():
    from minicolmap.rendering.gaussian_training import DensityStatistics
    state = {'means': torch.tensor([[0., 0., 1.], [1., 0., 1.]]),
             'log_scales': torch.tensor([[.12, .08, .04], [.001, .001, .001]]).log(),
             'quaternions': torch.tensor([[1., 0., 0., 0.]]).repeat(2, 1),
             'sh_coeffs': torch.zeros(2, 16, 3), 'opacity_logits': torch.zeros(2)}
    stats = DensityStatistics(state)
    stats.gradient[:] = torch.tensor([.001, 0.])
    stats.count[:] = 1
    options = experiment.config_for({}, 0, 1)['gaussian']
    return state, stats, options


def test_split_can_rescue_a_coarse_parent_before_size_pruning():
    state, stats, options = fixture()
    baseline, _, _ = experiment.topology(state, stats, options, {}, 1000)
    aligned, sources, new = experiment.topology(state, stats, options, {'grow_then_prune': True}, 1000)
    assert len(baseline['means']) == 1
    assert len(aligned['means']) == 3
    assert sources.tolist() == [1, 0, 0]
    assert new.tolist() == [False, True, True]
    assert torch.all(aligned['log_scales'].exp().amax(-1) < .1)


def test_pruning_schedule_follows_effective_pinned_upstream_behavior():
    state, stats, options = fixture()
    stats.gradient.zero_()
    stats.radius[:] = 1.
    early, _, _ = experiment.topology(state, stats, options, {'prune_schedule': True}, 1000)
    late, _, _ = experiment.topology(state, stats, options, {'prune_schedule': True}, 3100)
    assert len(early['means']) == 2
    assert len(late['means']) == 1


def test_world_coordinate_rasterizer_proxy_keeps_the_geometry():
    state, _, _ = fixture()
    center = torch.tensor([1., 2., 3.])
    proxy = experiment.official_proxy(state, 2, center, 8.)
    assert torch.equal(proxy.get_xyz, state['means'] * 8 + center)
    assert torch.allclose(proxy.get_scaling, state['log_scales'].exp() * 8)
    assert proxy.active_sh_degree == 2


def test_initializer_converts_official_knn_floor_to_world_units(monkeypatch):
    as_tensor = torch.as_tensor
    def cpu_tensor(value, *args, **kwargs):
        kwargs['device'] = 'cpu'
        return as_tensor(value, *args, **kwargs)
    monkeypatch.setattr(torch, 'as_tensor', cpu_tensor)
    points = np.array([[0, 0, 0], [1e-8, 0, 0], [0, 1e-8, 0], [0, 0, 1e-8]])
    scene = SimpleNamespace(points=points, colors=np.ones((4, 3))*.5, scale=8.)
    config = experiment.config_for({}, 0, 1)
    legacy = experiment.initial_state(scene, config, {})
    official_floor = experiment.initial_state(scene, config, {'knn_floor': True})
    assert torch.allclose(official_floor['log_scales'].exp() * scene.scale,
                          torch.full((4, 3), float(np.sqrt(1e-7))))
    assert torch.all(legacy['log_scales'].exp() < official_floor['log_scales'].exp())
    for key in ('means', 'quaternions', 'sh_coeffs', 'opacity_logits'):
        assert torch.equal(legacy[key], official_floor[key])


def test_baseline_keeps_original_budget_and_rest_lr():
    config = experiment.config_for({}, 0, 2)
    assert config['iterations'] == 30000
    assert config['image_downscale'] == 2
    assert config['gaussian']['densify_stop'] == 6666
    assert config['gaussian']['max_gaussians'] == 1000000
    assert config['gaussian']['sh_rest_lr_ratio'] == 1
    assert config['gaussian']['adam_eps'] == 1e-8


def test_official_opacity_reset_rewrap_drops_stale_gradient_and_keeps_step():
    import ast
    source = (Path(__file__).resolve().parents[2] / 'scripts/experiment_official_3dgs_fast.py').read_text()
    definition = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                      and node.name == 'rewrap_opacity_parameter')
    namespace = {}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), '<actual-experiment-rewrap>', 'exec'), namespace)
    from minicolmap.rendering.gaussian_training import reset_opacity
    state = {'opacity_logits': torch.nn.Parameter(torch.zeros(2))}
    optimizer = torch.optim.Adam([{'params': [state['opacity_logits']], 'name': 'opacity_logits'}])
    state['opacity_logits'].grad = torch.ones(2)
    optimizer.step()
    old = state['opacity_logits']
    reset_opacity(state, optimizer)
    namespace['rewrap_opacity_parameter'](state, optimizer)
    assert state['opacity_logits'] is not old
    assert state['opacity_logits'].grad is None
    assert old not in optimizer.state
    assert optimizer.param_groups[0]['params'][0] is state['opacity_logits']
    assert optimizer.state[state['opacity_logits']]['step'] == 1
    assert optimizer.state[state['opacity_logits']]['exp_avg'].count_nonzero() == 0
