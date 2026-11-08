"""Admission checks: never start CUDA or acquire a lease on an occupied GPU."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def runner():
    import sys
    path = ROOT / 'scripts/run_teatime_official_tests.py'
    spec = importlib.util.spec_from_file_location('teatime_tests_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # CLI helper imports use the same script search path as direct execution.
    if str(ROOT / 'scripts') not in sys.path:
        sys.path.insert(0, str(ROOT / 'scripts'))
    return module


def unexpected(*args, **kwargs):
    pytest.fail('Occupied GPU must not acquire a lease or spawn a CUDA worker')


def test_busy_inventory_stops_suite_before_reservation_or_child(monkeypatch, tmp_path):
    module = runner()
    import check_lerf_rgb_renderers as inventory
    from minicolmap.ui import gpu_lease
    monkeypatch.setattr(module, 'verify', lambda plan: None)
    monkeypatch.setattr(inventory, 'guard_workbench', lambda: None)
    monkeypatch.setattr(inventory, 'idle_devices', lambda: [])
    monkeypatch.setattr(gpu_lease, 'acquire', unexpected)
    spawned = []
    def cpu_report_only(command, **kwargs):
        assert Path(command[1]).name == 'report_teatime_official_tests.py'
        assert '--worker' not in command
        spawned.append(command)
    monkeypatch.setattr(module.subprocess, 'run', cpu_report_only)
    plan = {'outputs': {name: str(tmp_path / 'runs' / name) for name in module.VARIANTS}}
    module.run(SimpleNamespace(report=tmp_path), plan)
    result = module.read(tmp_path / 'execution.json')
    assert result['status'] == 'stopped_all_gpus_busy'
    assert result['completed'] == []
    assert result['pending'] == module.VARIANTS
    assert len(spawned) == 1


def test_gpu_becoming_busy_stops_worker_before_adopting_lease(monkeypatch, tmp_path):
    module = runner()
    import check_lerf_rgb_renderers as inventory
    from minicolmap.ui import gpu_lease
    module.write(tmp_path / 'plan.json', {})
    monkeypatch.setattr(module, 'verify', lambda plan: None)
    monkeypatch.setattr(inventory, 'idle_devices', lambda: [])
    monkeypatch.setattr(gpu_lease, 'adopt', unexpected)
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '7')
    with pytest.raises(RuntimeError, match='became busy'):
        module.worker(SimpleNamespace(report=tmp_path, gpu=7))


@pytest.mark.parametrize('variant,first_step,observations', [('baseline', 500, 1), ('statistics', 600, 600)])
def test_actual_controlled_loop_collects_the_intended_statistics(monkeypatch, tmp_path, variant, first_step, observations):
    """Run its real 600-step control flow; CPU allocation/renderer fixtures only."""
    import numpy as np
    import torch
    module = runner()
    import audit_gaussian_upstream as audit
    from minicolmap.rendering import algorithms, data, pipeline
    assert not torch.cuda.is_initialized()
    frame = data.Frame(1, 'tiny.png', np.array([[10., 0, 3.5], [0, 10., 2.5], [0, 0, 1.]]),
                       np.eye(3), np.zeros(3), np.full((6, 8, 3), .4, np.float32))
    scene = data.TrainingScene({i: frame for i in (1, 2, 3)},
             np.array([[-.1, 0, 2.], [.1, 0, 2.]], np.float32),
             np.full((2, 3), .4, np.float32), np.zeros(3), 1., [1, 2], [3])
    monkeypatch.setattr(data, 'load_training_scene', lambda *args: scene)
    def render(network, state, frame, config, backend, device, *, training_info=None, **kwargs):
        projected = state['means'][:, :2] * 1.
        if training_info is not None:
            projected.retain_grad()
            training_info.update(means2d=projected, radii=torch.ones(2), width=8,
                                 height=6, indices=torch.arange(2))
        return (projected.sum() * 0 + .4).expand(6, 8, 3)
    monkeypatch.setattr(pipeline, '_render', render)
    base = module.load_base(tmp_path)
    # The CPU control-flow fixture does not depend on a historical local RGB run.
    config = json.loads((ROOT / 'configs/3dgs_lerf.json').read_text(encoding='utf-8'))
    config['iterations'] = 30000
    config['gaussian']['max_gaussians'] = 1000000
    original = tmp_path / 'original'
    original.mkdir()
    (original / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(base, 'ORIGINAL', original)
    namespace = dict(vars(base))
    namespace['initial_state'] = lambda scene, config, policy: {
        key: torch.nn.Parameter(value) for key, value in algorithms.initialize_gaussians(
            torch.from_numpy(scene.points), torch.from_numpy(scene.colors), .001, .1, 3).items()}
    captured = []
    def topology(state, stats, options, policy, step):
        captured.append((step, stats.count.detach().tolist()))
        return base.topology(state, stats, options, policy, step)
    namespace['topology'] = topology
    # Explicit device literals become CPU; original loop/conditions stay intact.
    audit.extract_functions(ROOT / 'scripts/experiment_official_3dgs.py', ['train'], namespace)
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '7')
    monkeypatch.setenv('MINICOLMAP_EXPERIMENT_GPU', '7')
    for name in ('manual_seed_all', 'reset_peak_memory_stats', 'synchronize'):
        monkeypatch.setattr(torch.cuda, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(torch.cuda, 'device_count', lambda: 1)
    monkeypatch.setattr(torch.cuda, 'max_memory_allocated', lambda: 0)
    monkeypatch.setattr(torch.cuda, 'max_memory_reserved', lambda: 0)
    monkeypatch.setattr(torch.cuda, 'get_device_name', lambda: 'CPU control-flow fixture')
    class CpuTimer:
        def __init__(self, **kwargs):
            pass
        def record(self):
            pass
        def synchronize(self):
            pass
        def elapsed_time(self, other):
            return 0.
    monkeypatch.setattr(torch.cuda, 'Event', CpuTimer)
    namespace['train'](SimpleNamespace(variant=variant, seed=0, downscale=1, steps=600, output=tmp_path / variant))
    assert captured[0] == (first_step, [float(observations)] * 2)
    assert not torch.cuda.is_initialized()
