"""Finish queued reference checks after both finite training campaigns."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from minicolmap.semantics.project import sha256, write_json

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/langsplat-reference-20261021'


def main():
    write_json(REPORT/'finisher.json', {'pid': os.getpid()})
    while not all((p/'campaign-completed.json').exists() for p in (REPORT, REPORT/'full-view-protocol')):
        if (REPORT/'cancel').exists():
            write_json(REPORT/'acceptance.json', {'status': 'cancelled'})
            return
        write_json(REPORT/'finisher-status.json', {'status': 'waiting_for_campaigns'})
        time.sleep(10)
    stages = [['evaluate', '--kind', 'field', '--split', 'dev', '--checkpoint-step', str(n), '--protocol', 'full-view'] for n in (3000, 7000)]
    stages += [['query', '--image-id', '99', '--text', 'spice jar', '--protocol', p] for p in ('matched', 'full-view')]
    for index, stage in enumerate(stages):
        if (REPORT/'cancel').exists():
            write_json(REPORT/'acceptance.json', {'status': 'cancelled'})
            return
        write_json(REPORT/'finisher-status.json', {'status': 'running', 'stage': stage})
        with (REPORT/f'finish-{index:02d}.log').open('ab') as log:
            child = subprocess.run([sys.executable, '-u', str(ROOT/'scripts/langsplat_reference.py'), *stage],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ, CUDA_VISIBLE_DEVICES='7', OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4'))
        if child.returncode:
            write_json(REPORT/'finisher-status.json', {'status': 'failed', 'stage': stage, 'returncode': child.returncode, 'log': f'finish-{index:02d}.log'})
            return
    subprocess.run([sys.executable, str(ROOT/'scripts/analyze_langsplat_reference.py')], cwd=ROOT, check=True)
    expected = {
        'src/minicolmap/semantics/algorithms.py': 'a2d3b7b1b9731035c872da0d43176ee6cdf6df1e8f707a9151b3e5fc866886c0',
        'outputs/ui_3dgs_20261019_230921_797f2a/checkpoint.pt': 'fa56a42c1c1fc50db13eda64eb43c6d1445b7453abdac4e4c40a908a32059d5d',
        'data/semantic_waldo/autoencoder/checkpoint.pt': '1c21e15be21926d4f8e07dcbec5e9c0cfb3ccb8a2dbca065810c7aa0456146bb',
    }
    actual = {path: sha256(ROOT/path) for path in expected}
    if actual != expected:
        write_json(REPORT/'acceptance.json', {'status': 'failed_preservation_check', 'expected': expected, 'actual': actual})
        raise ValueError('An original learner/model artifact changed')
    manifest = json.loads((REPORT/'source-manifest.json').read_text())
    source = ROOT/'.cache/langsplat-official'
    if any(sha256(source/path) != value for path, value in manifest['files'].items()):
        raise ValueError('Pinned official source changed')
    libraries = manifest['native_libraries']
    if any(sha256(Path(path)) != value for path, value in libraries.items()):
        raise ValueError('Native library changed')
    for workspace in ('langsplat_reference', 'langsplat_reference_full_views'):
        for level in (1, 2, 3):
            marker = json.loads((ROOT/f'data/{workspace}/train/level{level}/completed.json').read_text())
            if not marker['geometry_unchanged'] or marker['iterations'] != 30000:
                raise ValueError('Incomplete or modified RGB field')
    write_json(REPORT/'acceptance.json', {'status': 'completed', 'gpu': 7,
        'original_artifacts_preserved': actual, 'official_sources_unchanged': True,
        'native_libraries_unchanged': True, 'trained_fields': 6,
        'iterations_per_field': 30000, 'budget_snapshots_evaluated': [3000, 7000, 30000],
        'query_smokes': ['matched:99:spice jar', 'full-view:99:spice jar']})
    write_json(REPORT/'finisher-status.json', {'status': 'completed'})


if __name__ == '__main__':
    main()
