param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Project .venv is missing. Create the environment and install .[ui] first.'
}
Set-Location -LiteralPath $projectRoot
$uiArguments = @('-m', 'minicolmap', 'ui', '--port', "$Port", '--project', $projectRoot)
if ($NoBrowser) { $uiArguments += '--no-browser' }
& $pythonPath @uiArguments
if ($LASTEXITCODE -ne 0) { throw "Workbench exited with code $LASTEXITCODE" }
