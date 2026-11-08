"""Resume a sequential campaign after the existing native reference suite.

The waiting process does not claim/use CUDA. Each actual suite obtains the
workbench GPU lease itself and fails if another task has begun using GPU 7.
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


def run(script, *args, cpu=False):
    env = dict(os.environ, CUDA_VISIBLE_DEVICES='-1' if cpu else '7',
               OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4')
    subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *map(str, args)],
                   cwd=ROOT, env=env, check=True)


def main():
    from minicolmap.ui.gpu_lease import active_lease, release
    # The user's existing 30k baseline is already frozen and audited. A 5k
    # prefix checks our loop (including the first 1m-cap event); don't spend an
    # additional hour duplicating that old Python-transport baseline. Full
    # matrix and later native confirmation each get a fused-transport baseline.
    while lease := active_lease(ROOT):
        if Path(lease['report']).resolve() != (REPORT / 'native').resolve():
            raise RuntimeError('Unexpected GPU reservation; campaign will not start')
        baseline = REPORT / 'native/runs/baseline-s0-d1-t30000/result.json'
        progress_file = baseline.parent / 'progress.json'
        progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}
        if baseline.is_file() or progress.get('step', 0) >= 5000:
            parent = lease['pid']
            proc = Path('/proc') / str(parent)
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            if not any(value.endswith(b'scripts/experiment_official_3dgs.py') for value in args):
                raise RuntimeError('Unexpected native controller identity')
            children_file = proc / 'task' / str(parent) / 'children'
            children = children_file.read_text().split()
            stopped = []
            for child in children:
                command = (Path('/proc') / child / 'cmdline').read_bytes().split(b'\0')
                if (b'--worker' not in command or not any(
                        value.endswith(b'scripts/experiment_official_3dgs.py') for value in command)):
                    raise RuntimeError('Unexpected child; refusing to stop it')
                os.kill(int(child), signal.SIGTERM)
                stopped.append(int(child))
            os.kill(parent, signal.SIGTERM)
            release(ROOT, lease['token'])
            (REPORT / 'native/fused-takeover.json').write_text(json.dumps({
                'frozen_original_checkpoint': str(ROOT / 'outputs/ui_3dgs_20261019_230921_797f2a/checkpoint.pt'),
                'duplicate_baseline_completed': baseline.is_file(), 'prefix_progress': progress,
                'native_controller': parent,
                'stopped_own_children': stopped,
                'reason': 'Reuse audited original 30k baseline; verified prefix; use a matching fused baseline for all new factor experiments.'
            }, indent=2))
            time.sleep(5)
            break
        print(json.dumps({'waiting_for': lease['report'], 'pid': lease['pid']}), flush=True)
        time.sleep(30)
    for variant in ('official_reference',):
        result = REPORT / f'native/runs/{variant}-s0-d1-t30000/result.json'
        if not result.exists() or json.loads(result.read_text())['status'] != 'complete':
            raise RuntimeError('Native baseline/reference incomplete')
    original = ROOT / 'outputs/ui_3dgs_20261019_230921_797f2a'
    from experiment_official_3dgs import sha
    if sha(original / 'checkpoint.pt') != 'fa56a42c1c1fc50db13eda64eb43c6d1445b7453abdac4e4c40a908a32059d5d':
        raise RuntimeError('Frozen original baseline changed')
    (REPORT / 'reference-original.json').write_text(json.dumps({
        'provenance': str(original), 'checkpoint_sha256': sha(original / 'checkpoint.pt'),
        'summary': json.loads((original / 'summary.json').read_text()),
        'timing_comparable': False,
        'note': 'Completed original 30k baseline, reused for native image-quality comparison only. Training timing uses different engineering and must not be compared.'
    }, indent=2))
    run('summarize_official_3dgs.py', REPORT / 'native', cpu=True)
    run('evaluate_3dgs_perceptual.py', REPORT / 'native', cpu=True)
    controls = 'alpha99,alpha99_ste,circular_footprint'
    run('experiment_official_3dgs.py', '--variants', controls, '--steps', 1000,
        '--downscale', 4, '--report', REPORT / 'kernel-preflight')
    smoke_results = json.loads((REPORT / 'kernel-preflight/results-d4-t1000-s0.json').read_text())
    if any(row['status'] != 'complete' for row in smoke_results):
        raise RuntimeError('Kernel preflight failed; matrix not started')
    parity = json.loads((REPORT / 'fused-parity.json').read_text())
    if abs(parity['delta_db']) > .002:
        raise RuntimeError('Fast adapter parity failed')
    run('experiment_official_3dgs_fast.py', '--steps', 30000, '--downscale', 4,
        '--report', REPORT / 'matrix')
    run('summarize_official_3dgs.py', REPORT / 'matrix', cpu=True)
    run('evaluate_3dgs_perceptual.py', REPORT / 'matrix', cpu=True)
    policy = REPORT / 'verification-policy.json'
    if policy.exists() and json.loads(policy.read_text()).get('mode') == 'seed0_only':
        print('Seed0 matrix complete; additional GPU experiments canceled by user.', flush=True)
        return
    run('experiment_official_3dgs_fast.py', '--variants', 'baseline,official_policy_gsplat',
        '--steps', 30000, '--downscale', 1, '--report', REPORT / 'fast-native')
    run('evaluate_3dgs_perceptual.py', REPORT / 'fast-native', cpu=True)
    print('Campaign complete. Inspect all failure records and run seed confirmations before conclusions.', flush=True)


if __name__ == '__main__':
    main()
