#!/usr/bin/env bash
# Install into the project venv, leaving the system and Conda base unchanged.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
gpu=0
reuse=0
for arg in "$@"; do
    case "$arg" in
        --gpu) gpu=1 ;;
        --reuse-torch) reuse=1 ;;
        *) echo "Usage: bash scripts/setup_linux.sh [--gpu] [--reuse-torch]" >&2; exit 2 ;;
    esac
done
cd "$root"
venv_args=()
if (( reuse )); then venv_args+=(--system-site-packages); fi
if [[ ! -x .venv/bin/python ]]; then
    "${MINICOLMAP_PYTHON:-python3}" -m venv "${venv_args[@]}" .venv
fi
python="$root/.venv/bin/python"
export PIP_CACHE_DIR="$root/.cache/pip"
# Explicit official index avoids inheriting an unavailable machine-wide mirror.
"$python" -m pip install --index-url https://pypi.org/simple \
    -r requirements-linux.txt -e '.[dev,ui,ui-test]'
"$python" -m minicolmap doctor
if (( gpu )); then
    if ! "$python" -c 'import torch; assert str(torch.__version__) == "2.6.0+cu124"'; then
        "$python" -m pip install --index-url https://download.pytorch.org/whl/cu124 'torch==2.6.0+cu124'
    fi
    "$python" -m pip install --index-url https://pypi.org/simple \
        -c requirements-linux.txt -r requirements-gsplat.txt 'ninja==1.11.1.4'
    export PATH="$root/.venv/bin:$PATH"
    if [[ -z ${CUDA_HOME:-} ]]; then
        nvcc_path=$(command -v nvcc) || { echo 'CUDA Toolkit nvcc is required.' >&2; exit 1; }
        export CUDA_HOME
        CUDA_HOME=$(dirname -- "$(dirname -- "$(readlink -f -- "$nvcc_path")")")
    fi
    export TORCH_EXTENSIONS_DIR="$root/.cache/torch_extensions"
    export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-7}"
    export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-8.6}"
    export MAX_JOBS="${MAX_JOBS:-2}"
    "$python" scripts/check_render_gpu.py --output .cache/linux-gpu-check.json
    "$python" scripts/build_gsplat_extension.py
fi
