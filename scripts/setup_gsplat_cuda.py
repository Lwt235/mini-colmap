"""Install the minimal official CUDA 12.8 compiler in this project, without admin.

Uses NVIDIA's redistributable manifest, verifies SHA256, preserves licenses, and
never changes the system CUDA/PyTorch installation. Run with the project Python.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

RELEASE = '12.8.1'
BASE = 'https://developer.download.nvidia.com/compute/cuda/redist/'
COMPONENTS = ('cuda_cccl', 'cuda_cudart', 'cuda_nvcc')
ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / '.cache/cuda-toolkit/12.8'
CACHE = ROOT / '.cache/cuda-redistrib'


def contained(root: Path, target: Path) -> Path:
    target = target.resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f'Path outside the intended project directory: {target}')
    return target


def download(url: str, target: Path, digest: str, size: int):
    if target.is_file():
        checksum = hashlib.sha256()
        with target.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(chunk)
        if checksum.hexdigest() == digest:
            print(f'Cached verified archive: {target.name}', flush=True)
            return
    temporary = target.with_suffix('.part')
    print(f'Download {target.name}: {size / 1024**2:.1f} MiB', flush=True)
    checksum, count, progress = hashlib.sha256(), 0, 0
    with urllib.request.urlopen(url, timeout=60) as source, temporary.open('wb') as output:
        while chunk := source.read(1024 * 1024):
            output.write(chunk)
            checksum.update(chunk)
            count += len(chunk)
            if count - progress >= 32 * 1024**2:
                print(f'  {count / 1024**2:.0f} MiB downloaded', flush=True)
                progress = count
    if count != size or checksum.hexdigest() != digest:
        raise ValueError(f'Official archive checksum/size mismatch: {target.name}')
    temporary.replace(target)


def extract(archive: Path, component: str):
    with zipfile.ZipFile(archive) as stream:
        for member in stream.infolist():
            parts = PurePosixPath(member.filename).parts
            if not parts or any(part in ('..', '') or ':' in part or '\\' in part for part in parts):
                raise ValueError(f'Unexpected archive path: {member.filename}')
            if len(parts) == 1 or member.is_dir():
                continue
            relative = Path(*parts[1:])
            # Keep component licenses/metadata separate when merging include/bin/lib.
            if len(relative.parts) == 1:
                relative = Path('licenses') / component / relative
            destination = contained(HOME, HOME / relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with stream.open(member) as source, destination.open('wb') as output:
                shutil.copyfileobj(source, output)


def main():
    contained(ROOT, HOME)
    contained(ROOT, CACHE)
    HOME.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(BASE + f'redistrib_{RELEASE}.json', timeout=60) as source:
        manifest = json.load(source)
    (CACHE / f'redistrib_{RELEASE}.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    selected = {key: manifest[key]['windows-x86_64'] for key in COMPONENTS}
    print(f'Official components total: {sum(int(x["size"]) for x in selected.values()) / 1024**2:.1f} MiB', flush=True)
    for component, metadata in selected.items():
        relative = metadata['relative_path']
        archive = CACHE / PurePosixPath(relative).name
        download(BASE + relative, archive, metadata['sha256'], int(metadata['size']))
        extract(archive, component)
    (HOME / 'version.json').write_text(json.dumps({'cuda': {'version': RELEASE}}), encoding='utf-8')
    (HOME / 'redistrib-manifest.json').write_text(json.dumps(selected, indent=2), encoding='utf-8')
    for name in ('bin/nvcc.exe', 'include/cuda_runtime.h', 'include/cub/cub.cuh', 'lib/x64/cudart.lib'):
        if not (HOME / name).is_file():
            raise FileNotFoundError(f'Missing compiler component: {name}')
    print(f'Project CUDA Toolkit ready: {HOME}', flush=True)


if __name__ == '__main__':
    main()
