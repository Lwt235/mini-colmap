"""Read-only CPU progress; refresh evidence when completed artifacts change."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/3dgs-official-ablation-20261020'
STAGES = ('matrix', 'fast-native', 'control-repeat1', 'control-repeat2',
          'confirm-seed1', 'confirm-seed2', 'native-factors')


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def snapshot():
    state = {'stages': {}, 'active': None, 'final': read(REPORT / 'final-input-verification.json'),
             'controller_errors': []}
    for name in ('campaign.log', 'confirmation.log'):
        path = REPORT / name
        if path.exists():
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size-65536))
                tail = stream.read().decode(errors='replace')
            if 'Traceback (most recent call last)' in tail:
                state['controller_errors'].append(name)
    for stage in STAGES:
        directory = REPORT / stage
        records = [read(path, {}) for path in directory.glob('runs/*/result.json')]
        complete = [row for row in records if row.get('status') == 'complete']
        manifests = list(directory.glob('manifest-d*-t*-s*.json'))
        manifest = read(manifests[0], {}) if manifests else {}
        state['stages'][stage] = {
            'complete': len(complete), 'expected': len(manifest.get('policies', [])),
            'lpips': len(list(directory.glob('runs/*/lpips.json'))),
            'failed': len(list(directory.glob('runs/*/failure.json')))}
    lease = read(ROOT / '.cache/3dgs-ablation-gpu.json')
    if lease and lease.get('report'):
        directory = Path(lease['report'])
        active = read(directory / 'active.json', {})
        output = Path(active['output']) if active.get('output') else None
        progress = read(output / 'progress.json', {}) if output else {}
        state['active'] = {'stage': directory.name, 'variant': active.get('variant'),
                           'step': progress.get('step'), 'gaussians': progress.get('gaussians'),
                           'last_progress_seconds_ago': round(time.time()-(output / 'progress.json').stat().st_mtime)
                           if output and (output / 'progress.json').exists() else None}
    return state


def refresh(state):
    marker = REPORT / 'report-progress-counts.json'
    counts = state['stages']
    if read(marker) != counts or state['final'] is not None:
        subprocess.run([sys.executable, str(ROOT / 'scripts/analyze_official_3dgs_campaign.py')],
                       cwd=ROOT, env=dict(os.environ, CUDA_VISIBLE_DEVICES='-1'),
                       stdout=subprocess.DEVNULL, check=True)
        marker.write_text(json.dumps(counts, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--allow-failures', action='store_true')
    args = parser.parse_args()
    while True:
        status = snapshot()
        refresh(status)
        print(json.dumps(status), flush=True)
        if not args.allow_failures and (status['controller_errors'] or
                                       any(row['failed'] for row in status['stages'].values())):
            raise SystemExit(2)
        if not args.watch or status['final'] is not None:
            break
        time.sleep(55)
