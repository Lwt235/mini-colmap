param(
    [string[]]$PythonArgs = @('-m', 'minicolmap', 'ui')
)

# Configure only this process and its children; never change system/user PATH.
$ErrorActionPreference = 'Stop'
$taskRepo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRepo '.venv/Scripts/python.exe'
$taskLocalCuda = Join-Path $taskRepo '.cache/cuda-toolkit/12.8'
$taskCudaHome = if (Test-Path -LiteralPath (Join-Path $taskLocalCuda 'bin/nvcc.exe')) { $taskLocalCuda } else { $env:CUDA_HOME }
if (-not $taskCudaHome -or -not (Test-Path -LiteralPath (Join-Path $taskCudaHome 'bin/nvcc.exe'))) {
    $taskCudaRoot = 'C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA'
    $taskCuda = Get-ChildItem -LiteralPath $taskCudaRoot -Directory |
        Where-Object { $_.Name -match '^v\d+\.\d+$' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'bin/nvcc.exe')) } |
        Sort-Object { [version]$_.Name.Substring(1) } -Descending | Select-Object -First 1
    if (-not $taskCuda) { throw 'CUDA Toolkit/nvcc.exe not found; see docs/usage.md' }
    $taskCudaHome = $taskCuda.FullName
}

$taskVswhere = 'C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path -LiteralPath $taskVswhere)) { throw 'Visual Studio Build Tools/vswhere.exe not found' }
$taskVsRoot = & $taskVswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $taskVsRoot) { throw 'MSVC x64 C++ build tools not found' }
$taskHostConfig = Get-Content -LiteralPath (Join-Path $taskCudaHome 'include/crt/host_config.h') -Raw
$taskCompilerCap = [regex]::Match($taskHostConfig, '_MSC_VER\s*>=\s*(\d+)')
$taskToolsets = Get-ChildItem -LiteralPath (Join-Path $taskVsRoot 'VC/Tools/MSVC') -Directory |
    Sort-Object { [version]$_.Name } -Descending
$taskCompatible = $taskToolsets | Where-Object {
    $taskVersion = [version]$_.Name
    -not $taskCompilerCap.Success -or (($taskVersion.Major + 5) * 100 + $taskVersion.Minor) -lt [int]$taskCompilerCap.Groups[1].Value
} | Select-Object -First 1
if (-not $taskCompatible) {
    throw "Installed CUDA Toolkit rejects the installed MSVC toolset ($($taskToolsets.Name -join ', ')). Configure compatible versions before using gsplat; reference + CUDA already works."
}
$taskToolsetVersion = [version]$taskCompatible.Name
$taskVcVersion = "$($taskToolsetVersion.Major).$($taskToolsetVersion.Minor)"
$taskVcvars = Join-Path $taskVsRoot 'VC/Auxiliary/Build/vcvars64.bat'
$taskVcEnv = & cmd.exe /d /s /c ('call "' + $taskVcvars + '" -vcvars_ver=' + $taskVcVersion + ' >nul && set')
if ($LASTEXITCODE -ne 0) { throw 'MSVC vcvars64 setup failed' }
foreach ($taskLine in $taskVcEnv) {
    $taskSeparator = $taskLine.IndexOf('=')
    if ($taskSeparator -gt 0) {
        [Environment]::SetEnvironmentVariable($taskLine.Substring(0, $taskSeparator), $taskLine.Substring($taskSeparator + 1), 'Process')
    }
}

$taskBuildRoot = [IO.Path]::GetFullPath((Join-Path $taskRepo '.cache/torch_extensions'))
if (-not $taskBuildRoot.StartsWith($taskRepo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Extension build directory must remain inside the project'
}
# gsplat may clean its own build directory, so constrain it to this checked path.
New-Item -ItemType Directory -Force -Path $taskBuildRoot | Out-Null
$env:TORCH_EXTENSIONS_DIR = $taskBuildRoot
$env:CUDA_HOME = $taskCudaHome
$env:CUDA_PATH = $taskCudaHome
$env:PATH = (Join-Path $taskCudaHome 'bin') + ';' + $env:PATH
$env:DISTUTILS_USE_SDK = '1'
if (-not $env:MAX_JOBS) { $env:MAX_JOBS = '2' }
if (-not $env:TORCH_CUDA_ARCH_LIST) {
    $env:TORCH_CUDA_ARCH_LIST = & $taskPython -c 'import torch; print("%d.%d" % torch.cuda.get_device_capability())'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to query CUDA device capability' }
}
$env:PYTHONIOENCODING = 'utf-8'
# English MSVC diagnostics avoid a Windows OEM decoding failure in PyTorch.
$env:VSLANG = '1033'
Write-Host "CUDA Toolkit: $taskCudaHome"
Write-Host "MSVC: $taskVsRoot"
Write-Host "Extension cache: $taskBuildRoot"
Push-Location -LiteralPath $taskRepo
try {
    & $taskPython @PythonArgs
    $taskCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $taskCode
