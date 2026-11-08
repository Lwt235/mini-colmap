"""Build gsplat 1.5.3 with native platform flags, then verify/register the runtime.

Windows: with_gsplat_env.ps1. Linux: setup_linux.sh --gpu.
Preserves objects and installed package sources.
"""
import importlib.util
import os
import runpy
import subprocess
import sys
from pathlib import Path

from torch.utils.cpp_extension import load

root = Path(__file__).resolve().parents[1]
directory = Path(os.environ['TORCH_EXTENSIONS_DIR']).resolve() / 'gsplat_cuda'
if not directory.is_relative_to(root / '.cache/torch_extensions'):
    raise ValueError('Build directory must remain in the project cache')
directory.mkdir(parents=True, exist_ok=True)
spec = importlib.util.find_spec('gsplat')
package = Path(next(iter(spec.submodule_search_locations)))
cuda = package / 'cuda'
sources = [*sorted((cuda / 'csrc').glob('*.cu')), *sorted((cuda / 'csrc').glob('*.cpp')), cuda / 'ext.cpp']
try:
    native = load(name='gsplat_cuda', sources=[str(path) for path in sources],
        extra_cflags=['/O2', '/utf-8'] if os.name == 'nt' else ['-O3', '-Wno-attributes'],
        extra_cuda_cflags=['-O3', '-use_fast_math'],
        extra_include_paths=[str(cuda / 'include'), str(cuda / 'csrc/third_party/glm')],
        build_directory=str(directory), with_cuda=True, verbose=False)
except Exception:
    # On this host torch's OEM decoder can hide the actual compiler diagnostic.
    log = root / '.cache/gsplat-build.log'
    with log.open('wb') as output:
        result = subprocess.run(['ninja', '-v', '-j', os.environ.get('MAX_JOBS', '2')], cwd=directory,
                                 stdout=output, stderr=subprocess.STDOUT)
    print('\n'.join(log.read_bytes().decode('utf-8', errors='replace').splitlines()[-65:]), flush=True)
    print(f'Raw build log: {log}', flush=True)
    raise

import gsplat
gsplat.csrc = native
sys.modules['gsplat.csrc'] = native
runpy.run_path(str(root / 'scripts/check_gsplat_backend.py'), run_name='__main__')
