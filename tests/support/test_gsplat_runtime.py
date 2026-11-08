import hashlib
import json

import pytest

from minicolmap.rendering.gsplat_runtime import validated_library


@pytest.fixture
def runtime(tmp_path):
    library = tmp_path / 'torch_extensions/gsplat_cuda/gsplat_cuda.pyd'
    library.parent.mkdir(parents=True)
    library.write_bytes(b'protocol fixture, not an executable library')
    expected = dict(python_tag='cpython-312', torch_version='2.7.1+cu128', torch_cuda='12.8', gsplat_version='1.5.3')
    manifest = dict(expected, library='torch_extensions/gsplat_cuda/gsplat_cuda.pyd',
                    sha256=hashlib.sha256(library.read_bytes()).hexdigest())
    path = tmp_path / 'gsplat-runtime.json'
    path.write_text(json.dumps(manifest))
    return path, library, expected, manifest


def test_validated_extension_is_resolved_only_inside_the_build_cache(runtime):
    path, library, expected, _ = runtime
    assert validated_library(path, expected) == library
    assert validated_library(path.with_name('absent.json'), expected) is None


@pytest.mark.parametrize('key', ['python_tag', 'torch_version', 'torch_cuda', 'gsplat_version'])
def test_old_extension_abi_is_not_silently_reused(runtime, key):
    path, _, expected, _ = runtime
    with pytest.raises(RuntimeError, match=key):
        validated_library(path, {**expected, key: 'different'})


def test_extension_integrity_is_checked_before_loading(runtime):
    path, library, expected, _ = runtime
    library.write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='校验失败'):
        validated_library(path, expected)


def test_manifest_cannot_load_a_binary_outside_the_cache(runtime):
    path, _, expected, manifest = runtime
    manifest['library'] = '../elsewhere.pyd'
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match='扩展路径'):
        validated_library(path, expected)


def test_linux_metadata_and_cuda_runtime_versions_are_checked_separately(runtime):
    path, library, expected, manifest = runtime
    manifest.update(torch_version='2.6.0+cu124', torch_distribution_version='2.6.0', torch_cuda='12.4')
    path.write_text(json.dumps(manifest))
    assert validated_library(path, {'torch_distribution_version': '2.6.0'}) == library
    # The package metadata check must not loosen the actual CUDA ABI check.
    assert validated_library(path, {'torch_version': '2.6.0+cu124', 'torch_cuda': '12.4'}) == library
    with pytest.raises(RuntimeError, match='torch_version'):
        validated_library(path, {'torch_version': '2.6.0+cu128'})
    with pytest.raises(RuntimeError, match='torch_distribution_version'):
        validated_library(path, {'torch_distribution_version': '2.7.0'})


def test_legacy_manifest_still_supports_matching_distribution_identity(runtime):
    path, library, expected, _ = runtime
    assert validated_library(path, {'torch_distribution_version': expected['torch_version']}) == library
