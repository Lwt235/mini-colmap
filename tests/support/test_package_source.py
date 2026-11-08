"""The source snapshot includes public evidence, not local state or old history."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location('package_source', Path(__file__).resolve().parents[2] / 'scripts/package_source.py')
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


@pytest.mark.parametrize('relative', [
    'README.md', '.gitignore', '.gitattributes', 'requirements-semantic.txt',
    'src/minicolmap/ui/static/assets/index.js', 'web/package-lock.json',
    'docs/assets/preview.png', 'tests/support/test_package_source.py',
    'models/README.md', 'reports/README.md', 'reports/benchmarks/semantics.json',
    'reports/benchmarks/3dgs-screening.csv',
    '.vscode/settings.json', 'typings/einx-stubs/__init__.pyi',
    'typings/einx-stubs/py.typed', 'web/.prettierignore', 'web/.prettierrc.json',
])
def test_public_files_are_allowed(relative):
    assert package.allowed(relative)


@pytest.mark.parametrize('relative', [
    'AGENTS.md', '.git/config', '.local-archive/docs/old.md', '.env',
    'web/.env.production', 'web/node_modules/dependency/index.js',
    'tests/__pycache__/test.pyc', 'web/test-results/result.json',
    'reports/raw-run/config.json', 'reports/benchmarks/model.pt',
    'data/scene.json', 'outputs/run/config.json', 'models/private.key',
    'docs/../../private.md', '/absolute.md', 'dist/source.zip',
])
def test_private_or_generated_files_are_excluded(relative):
    assert not package.allowed(relative)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / 'source'
    for relative in ('README.md', 'pyproject.toml', 'THIRD_PARTY_NOTICES.md',
                     'src/minicolmap/ui/static/index.html', 'reports/README.md',
                     'reports/benchmarks/metrics.json', 'docs/guide.md',
                     '.vscode/settings.json', 'web/.prettierignore', 'web/.prettierrc.json',
                     'typings/einx-stubs/__init__.pyi', 'typings/einx-stubs/py.typed'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('public source\n', encoding='utf-8')
    for relative in ('.env', 'reports/private/config.json', 'web/node_modules/vendor/index.js'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private content\n', encoding='utf-8')
    return root


def test_archive_is_deterministic_and_has_verified_manifest(source_tree):
    first = source_tree / 'dist/first.zip'
    second = source_tree / 'dist/second.zip'
    package.build(source_tree, first)
    package.build(source_tree, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert 'mini-colmap/docs/guide.md' in names
        for relative in package.PUBLIC_CONFIGS | {'typings/einx-stubs/__init__.pyi', 'typings/einx-stubs/py.typed'}:
            assert 'mini-colmap/' + relative in names
        assert all('private' not in name and 'node_modules' not in name for name in names)
        manifest = json.loads(archive.read('mini-colmap/SOURCE-MANIFEST.json'))
        for item in manifest['files']:
            data = archive.read('mini-colmap/' + item['path'])
            assert len(data) == item['bytes']
            assert hashlib.sha256(data).hexdigest() == item['sha256']


def test_archive_refuses_overwrite_or_source_output(source_tree):
    output = source_tree / 'dist/snapshot.zip'
    package.build(source_tree, output)
    with pytest.raises(FileExistsError):
        package.build(source_tree, output)
    with pytest.raises(ValueError, match='dist/'):
        package.build(source_tree, source_tree / 'README.zip')
