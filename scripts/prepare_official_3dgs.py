"""Fetch and build a pinned, project-local Graphdeco research reference.

No packages in .venv are replaced. SIBR is deliberately not downloaded.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/gaussian-upstream-audit'
REVISION = '54c035f7834b564019656c3e3fcc3646292f727d'


def download(entry):
    target = CACHE / entry['path']
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f'https://raw.githubusercontent.com/graphdeco-inria/gaussian-splatting/{REVISION}/{entry["path"]}'
    target.write_bytes(urllib.request.urlopen(url, timeout=60).read())


def run(command, cwd=None):
    print(' '.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd or ROOT, check=True)


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    tree_file = CACHE / 'tree.json'
    if not tree_file.exists():
        url = f'https://api.github.com/repos/graphdeco-inria/gaussian-splatting/git/trees/{REVISION}?recursive=1'
        tree_file.write_bytes(urllib.request.urlopen(url, timeout=60).read())
    tree = json.loads(tree_file.read_text())['tree']
    files = [entry for entry in tree if entry['type'] == 'blob' and
             (entry['path'].startswith(('scene/', 'utils/', 'arguments/', 'gaussian_renderer/', 'lpipsPyTorch/'))
              or entry['path'] in ('train.py', 'LICENSE.md', '.gitmodules'))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(download, files))
    sources = [
        ('diff-gaussian-rasterization', 'https://github.com/graphdeco-inria/diff-gaussian-rasterization.git'),
        ('simple-knn', 'https://gitlab.inria.fr/bkerbl/simple-knn.git'),
        ('fused-ssim', 'https://github.com/rahul-goel/fused-ssim.git'),
    ]
    site = CACHE / 'site'
    site.mkdir(exist_ok=True)
    run([sys.executable, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
         '--no-deps', '--target', site, 'plyfile==1.1.3'])
    for name, url in sources:
        directory = CACHE / 'submodules' / name
        revision = next(entry['sha'] for entry in tree if entry['path'] == 'submodules/' + name)
        if not (directory / '.git').exists():
            run(['git', 'clone', '--no-checkout', url, directory])
        run(['git', 'checkout', '--detach', revision], directory)
        run(['git', 'submodule', 'update', '--init', '--recursive'], directory)
        run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation',
             '--target', site, directory])
    (CACHE / 'build.json').write_text(json.dumps({
        'revision': REVISION, 'python': sys.executable,
        'cuda_home': os.environ.get('CUDA_HOME'), 'site': str(site),
        'submodules': {entry['path']: entry['sha'] for entry in tree if entry['type'] == 'commit'},
    }, indent=2))


if __name__ == '__main__':
    main()
