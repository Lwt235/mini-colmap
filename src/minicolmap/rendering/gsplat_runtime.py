"""Load a verified project-built extension without requiring a compiler in UI workers."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path


def file_sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def validated_library(manifest_path: Path, expected: dict) -> Path | None:
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for key, value in expected.items():
        # Some Linux distributions report 2.6.0 in package metadata while the
        # imported runtime reports 2.6.0+cu124. Keep both identities; actual
        # loading still checks the full runtime version and CUDA version.
        actual = manifest.get(key)
        if key == 'torch_distribution_version' and key not in manifest:
            actual = manifest.get('torch_version')  # Older Windows manifests.
        if actual != value:
            raise RuntimeError(f'gsplat 已编译扩展的 {key} 版本不匹配；请重新运行 scripts/check_gsplat_backend.py')
    directory = (manifest_path.parent / 'torch_extensions').resolve()
    library = (manifest_path.parent / manifest['library']).resolve()
    if not library.is_relative_to(directory) or library.suffix not in ('.pyd', '.so'):
        raise RuntimeError('gsplat 扩展路径必须位于项目的 torch_extensions 缓存内')
    if not library.is_file():
        raise RuntimeError('gsplat 已编译扩展文件缺失；请重新编译并验证')
    digest = file_sha256(library)
    if digest != manifest.get('sha256'):
        raise RuntimeError('gsplat 已编译扩展校验失败；请重新编译并验证')
    return library


@lru_cache(maxsize=1)
def load_project_extension():
    # This function is called only from an actual CUDA rendering process.
    import torch
    import gsplat
    if getattr(gsplat, 'csrc', None) is not None:
        return  # A wheel or the build verifier has already loaded a native module.
    root = Path(__file__).resolve().parents[3]
    library = validated_library(root / '.cache/gsplat-runtime.json', {
        'python_tag': sys.implementation.cache_tag,
        'torch_version': str(torch.__version__),
        'torch_cuda': torch.version.cuda,
        'gsplat_version': gsplat.__version__,
    })
    if library is None:
        return
    with ExitStack() as stack:
        if os.name == 'nt':
            for folder in (Path(torch.__file__).parent / 'lib', root / '.cache/cuda-toolkit/12.8/bin'):
                if folder.is_dir():
                    stack.enter_context(os.add_dll_directory(str(folder)))
        spec = importlib.util.spec_from_file_location('gsplat_cuda', library)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    # gsplat's own loader first looks for gsplat.csrc. Reuse our compiled module
    # through that supported import branch; never patch installed package files.
    sys.modules['gsplat_cuda'] = module
    sys.modules['gsplat.csrc'] = module
    gsplat.csrc = module
    backend = sys.modules.get('gsplat.cuda._backend')
    if backend is not None:
        backend._C = module
