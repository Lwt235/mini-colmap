"""Replicate the full matrix, measure repeatability, and test native images.

Selection uses development data deliberately. Never labels this selection a
fresh test or proof of scene-independent improvement. GPU jobs are sequential.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/3dgs-official-ablation-20261020'
SINGLE_FACTORS = {
    'statistics', 'grow_then_prune', 'window15000', 'no_capacity',
    'stochastic_split', 'adam_epsilon', 'sh_rest_lr', 'knn_unclipped',
    'epoch_sampling', 'white_reset', 'no_reset_pause',
    'ssim_same', 'ssim_fused', 'rgb_clamp', 'topology_before_adam',
    'prune_schedule', 'near_plane', 'alpha99', 'circular_footprint',
}
DATASET_SHA256 = '3f8d84c4d73fcffd62215ed2058f81fc17e9862eb7e5a5e75210649a47d1de4f'


def verify_frozen_controls():
    from experiment_official_3dgs import sha, ORIGINAL, DATASET, config_for
    manifest = json.loads((REPORT / 'matrix/manifest-d4-t30000-s0.json').read_text())
    first = REPORT / 'matrix/runs/baseline-s0-d4-t30000'
    normalization = json.loads((ORIGINAL / 'training_scene.json').read_text())['normalization']
    checks = {'source': sha(ORIGINAL / 'source_model.json') == manifest['source'],
              'checkpoint': sha(ORIGINAL / 'checkpoint.pt') == manifest['original_checkpoint'],
              'dataset': sha(DATASET) == DATASET_SHA256,
              'config': config_for({}, 0, 4) == json.loads((first / 'config.json').read_text())['config'],
              'normalization': {key: normalization[key] for key in ('center', 'scale')} ==
                               json.loads((first / 'protocol.json').read_text())['normalization'],
              'code': all(sha(ROOT / name) == digest for name, digest in manifest['code'].items()),
              'images': all(sha(Path(path)) == digest for path, digest in manifest['images'].items())}
    if not all(checks.values()):
        raise RuntimeError(f'Frozen controls changed between suites: {checks}')
    return checks


def run(script, *args, cpu=False):
    if not cpu:
        verify_frozen_controls()
    subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *map(str, args)], cwd=ROOT,
                   env=dict(os.environ, CUDA_VISIBLE_DEVICES='-1' if cpu else '7',
                            OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4'), check=True)


def complete_result(path):
    try:
        return json.loads(path.read_text()).get('status') == 'complete'
    except (OSError, ValueError):
        return False


def finish_seed0_only():
    """No additional CUDA jobs: finish CPU reporting and retire the scheduler."""
    from minicolmap.ui.gpu_lease import active_lease
    manifest = json.loads((REPORT / 'matrix/manifest-d4-t30000-s0.json').read_text())
    expected = [REPORT / f'matrix/runs/{name}-s0-d4-t30000' for name in manifest['policies']]
    while True:
        attempted = all((path / 'result.json').exists() or (path / 'failure.json').exists() for path in expected)
        lease = active_lease(ROOT)
        matrix_busy = lease and Path(lease['report']).resolve() == (REPORT / 'matrix').resolve()
        if attempted and not matrix_busy:
            break
        print('Finishing seed0 only; all further GPU experiments canceled.', flush=True)
        time.sleep(10)
    disabled_path = REPORT / 'disabled-followup-controller.json'
    if disabled_path.exists():
        disabled = json.loads(disabled_path.read_text())
        pid = disabled['pid']
        proc = Path('/proc') / str(pid)
        if proc.exists():
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            if b'scripts/run_official_3dgs_campaign.py' not in args:
                raise RuntimeError('Stopped controller identity changed; refusing to signal')
            # Its matrix child can briefly remain alive during CUDA teardown.
            while True:
                children = (proc / 'task' / str(pid) / 'children').read_text().split()
                living = [child for child in children if (path := Path('/proc') / child / 'stat').exists()
                          and path.read_text().split()[2] != 'Z']
                if not living:
                    break
                time.sleep(1)
            try:
                os.kill(pid, signal.SIGTERM)
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass  # Already exited, which is the requested state.
        disabled['state'] = 'terminated_after_matrix'
        disabled_path.write_text(json.dumps(disabled, indent=2))
    run('evaluate_3dgs_perceptual.py', REPORT / 'matrix', cpu=True)
    unchanged = verify_frozen_controls()
    completed = sum(complete_result(path / 'result.json') for path in expected)
    completion = {'mode': 'seed0_only', 'completed': completed, 'expected': len(expected),
                  'seeds': [0], 'gpu': 7, 'status': 'complete' if completed == len(expected) else 'completed_with_failures',
                  'canceled': ['fast-native', 'control-repeat1', 'control-repeat2', 'confirm-seed1',
                               'confirm-seed2', 'native-factors', 'cross-render', 'official-entry-prefix'],
                  'reason': 'User requested finishing seed0 and moving to Waldo semantic training.'}
    (REPORT / 'campaign-completion.json').write_text(json.dumps(completion, indent=2))
    (REPORT / 'final-input-verification.json').write_text(json.dumps(unchanged, indent=2))
    run('analyze_official_3dgs_campaign.py', cpu=True)
    print(json.dumps(completion), flush=True)


def main():
    policy_file = REPORT / 'verification-policy.json'
    if policy_file.exists() and json.loads(policy_file.read_text()).get('mode') == 'seed0_only':
        finish_seed0_only()
        return
    from minicolmap.ui.gpu_lease import active_lease
    markers = [REPORT / f'fast-native/runs/{name}-s0-d1-t30000/result.json'
               for name in ('baseline', 'official_policy_gsplat')]
    while not all(complete_result(path) for path in markers) or active_lease(ROOT):
        print('Waiting for full matrix and native controls.', flush=True)
        time.sleep(30)
    manifest = json.loads((REPORT / 'matrix/manifest-d4-t30000-s0.json').read_text())
    expected = [REPORT / f'matrix/runs/{name}-s0-d4-t30000' for name in manifest['policies']]
    if not all((path / 'result.json').exists() or (path / 'failure.json').exists() for path in expected):
        raise RuntimeError('Full matrix is incomplete; confirmations not started')
    run('cross_render_3dgs_checkpoints.py')
    run('check_official_training_entry.py')
    policy_path = REPORT / 'verification-policy.json'
    policy = json.loads(policy_path.read_text()) if policy_path.exists() else {'mode': 'full'}
    for repeat in range(1, policy.get('repeat_identical_seed0_baselines', 2)+1):
        destination = REPORT / f'control-repeat{repeat}'
        run('experiment_official_3dgs_fast.py', '--variants', 'baseline', '--seed', 0,
            '--steps', 30000, '--downscale', 4, '--report', destination)
        run('evaluate_3dgs_perceptual.py', destination, cpu=True)
    results = {path.parent.name.split('-s0-')[0]: json.loads(path.read_text())
               for path in (REPORT / 'matrix').glob('runs/*/result.json')}
    baseline = results['baseline']['metrics']['psnr']
    singles = {name: result['metrics']['psnr'] - baseline for name, result in results.items()
               if name in SINGLE_FACTORS}
    if not singles:
        raise RuntimeError('No completed single factors')
    best, worst = max(singles, key=singles.get), min(singles, key=singles.get)
    selected = list(dict.fromkeys([best, worst]))
    replicated = list(manifest['policies']) if policy.get('mode') == 'full' else ['baseline', *selected]
    selection = {'mode': policy.get('mode', 'full'), 'replicated_variants': replicated,
                 'best_single_seed0': best, 'worst_single_seed0': worst,
                 'seed0_delta_db': {name: singles[name] for name in selected},
                 'native_controls': [str(path) for path in markers],
                 'note': 'All 30 policies use fixed seeds 0, 1, 2. Native candidates are selected on development images; knn_floor is an inactive negative control, excluded from candidate selection.'}
    (REPORT / 'confirmation-plan.json').write_text(json.dumps(selection, indent=2))
    for seed in policy.get('additional_seeds', [1, 2]):
        destination = REPORT / f'confirm-seed{seed}'
        run('experiment_official_3dgs_fast.py', '--variants', ','.join(replicated),
            '--seed', seed, '--steps', 30000, '--downscale', 4, '--report', destination)
        run('evaluate_3dgs_perceptual.py', destination, cpu=True)
    if policy.get('mode') == 'full':
        groups = []
        for directory in ('matrix', 'confirm-seed1', 'confirm-seed2'):
            groups.append({json.loads(path.read_text())['variant']: json.loads(path.read_text())
                           for path in (REPORT / directory).glob('runs/*/result.json')})
        averages = {}
        for name in SINGLE_FACTORS:
            if all(name in group and 'baseline' in group for group in groups):
                averages[name] = sum(group[name]['metrics']['psnr']-group['baseline']['metrics']['psnr']
                                     for group in groups)/len(groups)
        if averages:
            for name in (max(averages,key=averages.get), min(averages,key=averages.get)):
                if name not in selected and abs(averages[name]) >= .2:
                    selected.append(name)
        selection['native_selected_after_replication'] = selected
        selection['replicated_mean_delta_db'] = averages
        (REPORT / 'confirmation-plan.json').write_text(json.dumps(selection,indent=2))
    run('experiment_official_3dgs_fast.py', '--variants', ','.join(selected),
        '--steps', 30000, '--downscale', 1, '--report', REPORT / 'native-factors')
    run('evaluate_3dgs_perceptual.py', REPORT / 'native-factors', cpu=True)
    # Compare all stages to seed0, not merely to each new stage's own manifest.
    unchanged = verify_frozen_controls()
    (REPORT / 'final-input-verification.json').write_text(json.dumps(unchanged, indent=2))
    if not all(unchanged.values()):
        raise RuntimeError('Inputs changed; results require investigation')
    run('analyze_official_3dgs_campaign.py', cpu=True)


if __name__ == '__main__':
    main()
