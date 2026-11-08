param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$gpuProjectRoot = Split-Path -Parent $PSScriptRoot
$gpuPython = Join-Path $gpuProjectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $gpuPython)) {
    throw 'Create the project .venv using the README before configuring GPU rendering.'
}
Push-Location -LiteralPath $gpuProjectRoot
try {
    if (-not $VerifyOnly) {
        # Close this project's Python workers before replacing a loaded torch DLL.
        & $gpuPython -m pip install -r requirements-render-gpu.txt --cache-dir .cache/pip-render-gpu
        if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed' }
        & $gpuPython -m pip install -e '.[render,dev,ui,ui-test]'
        if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed' }
    }
    & $gpuPython scripts/check_render_gpu.py --output reports/gpu-environment/latest.json
    if ($LASTEXITCODE -ne 0) { throw 'Actual CUDA tensor/gradient verification failed' }
}
finally {
    Pop-Location
}
