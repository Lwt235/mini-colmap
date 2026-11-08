"""Run screening, fixed-topology controls and original-resolution confirmation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from minicolmap.ui.gpu_lease import process_identity
from minicolmap.ui.worker import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def invoke(script, *arguments):
    subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *map(str, arguments)],
                   cwd=ROOT, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'reports/3dgs-ablation-20261019')
    parser.add_argument('--wait-pid', type=int, help='Continue a screening batch that is already running')
    parser.add_argument('--confirmation-steps', type=int, default=10000)
    args = parser.parse_args()
    report = args.report.resolve()
    state = report / 'stages.json'
    try:
        if args.wait_pid:
            identity = process_identity(args.wait_pid)
            atomic_json(state, {'status': 'running', 'stage': 'screening', 'pid': args.wait_pid})
            while identity is not None and process_identity(args.wait_pid) == identity:
                time.sleep(5)
            results = json.loads((report / 'results.json').read_text(encoding='utf-8'))
            if len(results) != 36:
                raise ValueError('Screening stopped before all 36 paired experiments completed')
        else:
            atomic_json(state, {'status': 'running', 'stage': 'screening'})
            invoke('run_3dgs_ablation.py', '--report', report)
        invoke('summarize_3dgs_ablation.py', '--report', report)
        screening = json.loads((report / 'aggregate.json').read_text(encoding='utf-8'))
        if screening['provisional']:
            raise ValueError('Cannot select from incomplete screening')
        selected = screening['dev_ranking'][0]['variant']
        selection = {'variant': selected, 'criterion': screening['selection_metric'],
                     'dev_ranking': screening['dev_ranking'], 'test_used_for_selection': False,
                     'confirmation_steps': args.confirmation_steps, 'confirmation_resolution': [640, 480]}
        atomic_json(report / 'selection.json', selection)
        atomic_json(state, {'status': 'running', 'stage': 'fixed_topology', 'selection': selected})
        fixed = report / 'fixed-topology'
        invoke('run_3dgs_ablation.py', '--report', fixed, '--variants', 'baseline,loss', '--fixed-topology')
        invoke('summarize_3dgs_ablation.py', '--report', fixed)
        atomic_json(state, {'status': 'running', 'stage': 'original_resolution', 'selection': selected})
        full = report / 'full-resolution'
        invoke('run_3dgs_ablation.py', '--report', full, '--scenes', 'temple312',
               '--variants', f'baseline,{selected}', '--seeds', '0',
               '--iterations', args.confirmation_steps, '--downscale', '1')
        invoke('summarize_3dgs_ablation.py', '--report', full)
        # Publish copies for the normal UI discovery/live-rendering workflow.
        # Never move or rewrite the experiment source or any existing output.
        exports = []
        for result in json.loads((full / 'results.json').read_text(encoding='utf-8')):
            source = Path(result['output']).resolve()
            target = ROOT / 'outputs' / ('ablation_' + source.name)
            if not source.is_relative_to((full / 'runs').resolve()) or target.resolve().parent != (ROOT / 'outputs').resolve():
                raise ValueError('Uncontained experiment publication path')
            if target.exists():
                from run_3dgs_ablation import digest
                if digest(target / 'checkpoint.pt') != digest(source / 'checkpoint.pt'):
                    raise ValueError('Refusing to replace existing published weights: ' + str(target))
            else:
                shutil.copytree(source, target)
            exports.append({'variant': result['variant'], 'output': str(target)})
        atomic_json(report / 'published.json', exports)
        atomic_json(state, {'status': 'completed', 'stage': 'done', 'selection': selected,
                            'screening_jobs': 36, 'fixed_topology_jobs': 12, 'full_resolution_jobs': 2,
                            'published': exports})
        print(json.dumps({'status': 'completed', 'selection': selected, 'published': exports}), flush=True)
    except Exception as exc:
        atomic_json(state, {'status': 'failed', 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
