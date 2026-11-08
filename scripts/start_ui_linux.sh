#!/usr/bin/env bash
# Foreground launcher; tmux may keep it running after the SSH session closes.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
port=${MINICOLMAP_PORT:-8765}
gpu=${CUDA_VISIBLE_DEVICES:-7}
while (( $# )); do
    case "$1" in
        --port|--gpu)
            (( $# >= 2 )) || { echo "Missing value for $1" >&2; exit 2; }
            if [[ $1 == --port ]]; then port=$2; else gpu=$2; fi
            shift 2 ;;
        *) echo 'Usage: bash scripts/start_ui_linux.sh [--port 8765] [--gpu 7]' >&2; exit 2 ;;
    esac
done
[[ $port =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) || {
    echo 'Port must be between 1 and 65535.' >&2; exit 2;
}
[[ -x "$root/.venv/bin/python" ]] || {
    echo 'Run bash scripts/setup_linux.sh first.' >&2; exit 1;
}
cd "$root"
export CUDA_VISIBLE_DEVICES="$gpu"
export TORCH_EXTENSIONS_DIR="$root/.cache/torch_extensions"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}"
export PATH="$root/.venv/bin:$PATH"
exec "$root/.venv/bin/python" -m minicolmap ui \
    --port "$port" --project "$root" --no-browser
