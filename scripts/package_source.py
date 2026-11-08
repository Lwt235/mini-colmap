"""Build a clean, deterministic source ZIP without Git history or local assets.

In a Git checkout, select tracked and non-ignored untracked files. An exported
source tree can be repackaged without Git using the same narrow allowlist.
Neither staging nor committing is required. Run from any directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {'src', 'scripts', 'tests', 'exercises', 'web', 'configs', 'docs', 'examples', 'typings'}
ROOT_FILES = {'.gitattributes', '.gitignore', 'README.md', 'THIRD_PARTY_NOTICES.md',
              'pyproject.toml', 'LICENSE', 'LICENSE.md', 'LICENSE.txt'}
PUBLIC_CONFIGS = {'.vscode/settings.json', 'web/.prettierignore', 'web/.prettierrc.json'}
EXCLUDED = {'.git', '.venv', '.cache', '.local-archive', '__pycache__', 'node_modules',
            'dist', 'build', '.vite', '.pytest_cache', 'test-results', 'playwright-report'}
SUFFIXES = {'.py', '.pyi', '.sh', '.ps1', '.json', '.md', '.txt', '.toml', '.ts', '.tsx', '.mjs',
            '.js', '.css', '.html', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.webm'}


def allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = path.parts
    if not parts or path.is_absolute() or any(part in ('', '.', '..') for part in parts):
        return False
    if any(part in EXCLUDED or part.startswith('.env') for part in parts):
        return False
    if relative in PUBLIC_CONFIGS:
        return True
    if any(part.startswith('.') for part in parts) and relative not in ROOT_FILES:
        return False
    if len(parts) == 1:
        return relative in ROOT_FILES or (relative.startswith('requirements') and path.suffix == '.txt')
    if relative in {'models/README.md', 'reports/README.md'}:
        return True
    if parts[0] == 'reports':
        return len(parts) == 3 and parts[1] == 'benchmarks' and path.suffix in {'.json', '.csv'}
    return parts[0] in DIRECTORIES and (path.suffix.lower() in SUFFIXES or (
        parts[0] in {'src', 'typings'} and path.name == 'py.typed'))


def candidates(root: Path):
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                 '--exclude-standard'], check=True, stdout=subprocess.PIPE)
        # core.quotepath has no effect with -z; Git paths are UTF-8.
        return sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    return sorted(path.relative_to(root).as_posix() for directory in DIRECTORIES | {'reports', 'models'}
                  if (root / directory).is_dir()
                  for path in (root / directory).rglob('*') if path.is_file()) + sorted(
                      path.name for path in root.iterdir() if path.is_file()) + sorted(PUBLIC_CONFIGS)


def selected_files(root: Path):
    for relative in sorted(set(candidates(root))):
        if not allowed(relative):
            continue
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file():  # A tracked deletion is not part of the current snapshot.
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Source snapshot cannot include symlinks: {relative}')
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f'Review oversized source asset before publishing: {relative}')
        yield relative, path


def entry(relative: str):
    info = zipfile.ZipInfo('mini-colmap/' + relative, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o100755 if relative.endswith('.sh') else 0o100644
    info.external_attr = mode << 16
    return info


def build(root: Path, output: Path):
    root, output = root.resolve(), output.resolve()
    if output.suffix != '.zip':
        raise ValueError('Output must have a .zip suffix')
    # Source files cannot be replaced by an output archive.
    if output.is_relative_to(root) and output.relative_to(root).parts[0] != 'dist':
        raise ValueError('Archives inside the project must be written under dist/')
    if output.exists():
        raise FileExistsError(f'Refusing to overwrite an existing archive: {output}')
    files = list(selected_files(root))
    required = {'README.md', 'pyproject.toml', 'src/minicolmap/ui/static/index.html',
                'reports/README.md', 'THIRD_PARTY_NOTICES.md'}
    if not required.issubset({relative for relative, _ in files}):
        raise ValueError('Source tree is incomplete; required public files are missing')
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, path in files:
            data = path.read_bytes()
            archive.writestr(entry(relative), data)
            manifest.append({'path': relative, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        note = ('Current working-tree source snapshot, including uncommitted changes. '
                'No Git history, local archives, credentials, datasets, checkpoints or pretrained weights. '
                'Historical benchmark evidence is retained, not rerun. '
                'Third-party usage and license conditions still apply.\n')
        archive.writestr(entry('SNAPSHOT-NOTE.txt'), note.encode('utf-8'))
        metadata = json.dumps({'schema_version': 1, 'files': manifest}, ensure_ascii=False, indent=2) + '\n'
        archive.writestr(entry('SOURCE-MANIFEST.json'), metadata.encode('utf-8'))
    with zipfile.ZipFile(output) as archive:
        damaged = archive.testzip()
        if damaged:
            raise ValueError(f'Corrupt archive member: {damaged}')
    result = {'archive': str(output), 'files': len(manifest), 'bytes': output.stat().st_size,
              'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/mini-colmap-source.zip')
    args = parser.parse_args()
    build(ROOT, args.output)


if __name__ == '__main__':
    main()
