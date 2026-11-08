"""Preview publication is independent of Windows readers and concurrent temp names."""
import os
from pathlib import Path

import numpy as np
import pytest

from minicolmap.rendering import pipeline


def test_existing_reference_is_never_replaced_or_touched(tmp_path, monkeypatch):
    image = tmp_path / 'reference-4.png'
    pixels = np.zeros((6, 8, 3))
    pipeline._write_reference_image(image, pixels)
    original = image.read_bytes()
    timestamp = image.stat().st_mtime_ns

    def forbidden(*args):
        raise PermissionError('An HTTP reader denies replacement')

    monkeypatch.setattr(pipeline, '_replace', forbidden)
    pipeline._write_reference_image(image, pixels)
    assert image.read_bytes() == original
    assert image.stat().st_mtime_ns == timestamp


def test_unique_image_temporaries_do_not_clobber_other_writers(tmp_path, monkeypatch):
    target = tmp_path / 'preview.png'
    legacy_temporary = target.with_suffix('.tmp')
    legacy_temporary.write_bytes(b'other writer')
    published = []
    original = pipeline._replace

    def inspect(temporary, path):
        published.append(temporary)
        assert temporary != legacy_temporary and temporary.parent == target.parent
        return original(temporary, path)

    monkeypatch.setattr(pipeline, '_replace', inspect)
    pipeline._write_image(target, np.zeros((6, 8, 3)))
    pipeline._write_image(target, np.ones((6, 8, 3)))
    assert len(set(published)) == 2
    assert all(not path.exists() for path in published)
    assert legacy_temporary.read_bytes() == b'other writer'


def test_persistent_publication_error_is_not_hidden(tmp_path, monkeypatch):
    def forbidden(*args):
        raise PermissionError('persistent permission failure')

    monkeypatch.setattr(pipeline, '_replace', forbidden)
    with pytest.raises(PermissionError, match='persistent permission failure'):
        pipeline._write_image(tmp_path / 'preview.png', np.zeros((6, 8, 3)))
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows native sharing semantics')
def test_reference_survives_real_windows_read_handle_without_delete_sharing(tmp_path):
    import ctypes
    from ctypes import wintypes

    image = tmp_path / 'reference-4.png'
    pixels = np.zeros((6, 8, 3))
    pipeline._write_reference_image(image, pixels)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(image), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        replacement = tmp_path / 'proof.tmp'
        replacement.write_bytes(image.read_bytes())
        with pytest.raises(PermissionError):
            replacement.replace(image)
        pipeline._write_reference_image(image, pixels)
    finally:
        kernel.CloseHandle(handle)
