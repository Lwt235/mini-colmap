"""Real CUDA forward/backward/Adam check, then register the built project runtime."""
import importlib.metadata
import json
import sys
from pathlib import Path

import gsplat
import torch

from minicolmap.rendering.algorithms import initialize_gaussians
from minicolmap.rendering.config import resolve_render_config
from minicolmap.rendering.pipeline import _render
from minicolmap.rendering.gsplat_runtime import file_sha256

root = Path(__file__).resolve().parents[1]
config = resolve_render_config({'method': '3dgs', 'device': 'cuda', 'background': 'black',
                                'gaussian': {'backend': 'gsplat'}})
state = initialize_gaussians(torch.tensor([[0., 0., 2.], [.15, -.1, 2.5]], device='cuda'),
                             torch.tensor([[.7, .3, .2], [.1, .5, .8]], device='cuda'),
                             initial_scale=.15, initial_opacity=.3)
optimizer = torch.optim.Adam(list(state.values()), lr=1e-3)
frame = dict(K=[[8., 0., 4.], [0., 8., 3.], [0., 0., 1.]],
             R=[[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]], t=[0., 0., 0.], width=9, height=7)
losses = []
for _ in range(2):
    optimizer.zero_grad()
    rgb = _render(None, state, frame, config, torch, 'cuda')
    assert rgb.is_cuda and torch.isfinite(rgb).all()
    loss = (rgb-.1).square().mean()
    loss.backward()
    assert all(value.grad is not None and torch.isfinite(value.grad).all() for value in state.values())
    optimizer.step()
    losses.append(loss.detach().item())

from gsplat.cuda._backend import _C
library = Path(_C.__file__).resolve()
cache = root / '.cache'
assert library.is_relative_to(cache / 'torch_extensions')
digest = file_sha256(library)
manifest = dict(python_tag=sys.implementation.cache_tag, torch_version=str(torch.__version__),
                torch_distribution_version=importlib.metadata.version('torch'),
                torch_cuda=torch.version.cuda, gsplat_version=gsplat.__version__,
                library=library.relative_to(cache).as_posix(), sha256=digest,
                device=torch.cuda.get_device_name(), capability=list(torch.cuda.get_device_capability()),
                verification='real CUDA SH/covariance/rasterization/backward/Adam', losses=losses)
temporary = cache / 'gsplat-runtime.json.tmp'
temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
temporary.replace(cache / 'gsplat-runtime.json')
print(json.dumps(manifest, indent=2), flush=True)
