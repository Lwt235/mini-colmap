"""Check the actual CUDA environment, without implementing learner renderers.

This runs a generic tensor/linear-algebra/autograd/Adam smoke check. Passing it
does not certify completed NeRF/3DGS TODOs or reconstructed image quality.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time


def check_gpu() -> dict:
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(f'CUDA unavailable: torch={torch.__version__}, runtime={torch.version.cuda}')
    started = time.monotonic()
    device = torch.device('cuda:0')
    torch.manual_seed(73)
    torch.cuda.manual_seed_all(73)
    torch.backends.cuda.matmul.allow_tf32 = False
    properties = torch.cuda.get_device_properties(device)
    torch.cuda.reset_peak_memory_stats(device)

    # Verify genuine GPU execution against a CPU reference, rather than relying
    # only on a build suffix or torch.cuda.is_available().
    left = torch.randn(64, 64)
    right = torch.randn(64, 64)
    actual = left.to(device) @ right.to(device)
    expected = left @ right
    torch.testing.assert_close(actual.cpu(), expected, rtol=1e-4, atol=1e-4)

    # The reference splatter needs differentiable small matrix operations.
    # Exercise CUDA solve/backprop without supplying S2/S3/S4 implementations.
    raw = torch.randn(4, 3, 3, device=device, requires_grad=True)
    matrices = raw @ raw.transpose(-1, -2) + torch.eye(3, device=device) * .5
    rhs = torch.ones(4, 3, 1, device=device)
    solved = torch.linalg.solve(matrices, rhs)
    solved.square().sum().backward()
    if raw.grad is None or not torch.isfinite(raw.grad).all():
        raise RuntimeError('CUDA linear-algebra gradients are invalid')

    # Generic optimizer check, not the student's position/density/color network.
    network = torch.nn.Sequential(torch.nn.Linear(3, 32), torch.nn.SiLU(),
                                  torch.nn.Linear(32, 3), torch.nn.Sigmoid()).to(device)
    optimizer = torch.optim.Adam(network.parameters(), lr=.01)
    x = torch.randn(64, 3, device=device)
    target = torch.tensor([.2, .5, .8], device=device).expand(64, 3)
    with torch.no_grad():
        before = float((network(x) - target).square().mean())
    for _ in range(40):
        optimizer.zero_grad(set_to_none=True)
        loss = (network(x) - target).square().mean()
        loss.backward()
        if any(p.grad is None or not torch.isfinite(p.grad).all() for p in network.parameters()):
            raise RuntimeError('CUDA network gradients are invalid')
        optimizer.step()
    with torch.no_grad():
        after = float((network(x) - target).square().mean())
    if not after < before:
        raise RuntimeError(f'CUDA Adam did not lower the test loss: {before} -> {after}')
    torch.cuda.synchronize(device)
    free, total = torch.cuda.mem_get_info(device)
    try:
        driver = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader,nounits'],
            text=True, encoding='utf-8', errors='replace', timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        driver = None
    return {'status': 'passed', 'python': sys.version, 'python_executable': sys.executable,
            'platform': platform.platform(), 'torch': torch.__version__,
            'cuda_runtime': torch.version.cuda, 'cudnn_version': torch.backends.cudnn.version(),
            'cuda_available': True, 'device': str(device), 'gpu_name': properties.name,
            'compute_capability': [properties.major, properties.minor],
            'total_memory_bytes': total, 'free_memory_bytes': free,
            'peak_tensor_memory_bytes': torch.cuda.max_memory_allocated(device),
            'nvidia_smi': driver, 'matrix_cpu_gpu_close': True,
            'linear_algebra_backward_finite': True,
            'optimizer': {'steps': 40, 'before_mse': before, 'after_mse': after},
            'elapsed_seconds': time.monotonic() - started,
            'scope': 'GPU environment smoke; student NeRF/3DGS algorithms are not implemented or certified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON verification record')
    args = parser.parse_args()
    try:
        report = check_gpu()
    except Exception as exc:
        report = {'status': 'failed', 'error': str(exc), 'python_executable': sys.executable}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
