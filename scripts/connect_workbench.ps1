param(
    [Parameter(Mandatory = $true)]
    [string]$SshHost,
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 18765,
    [ValidateRange(1, 65535)]
    [int]$RemotePort = 8765
)

$ErrorActionPreference = 'Stop'
Write-Host "Workbench: http://127.0.0.1:$LocalPort"
Write-Host 'Keep this terminal open. Ctrl+C closes only the tunnel.'
& ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 `
    -o ServerAliveCountMax=3 -L "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}" $SshHost
if ($LASTEXITCODE -ne 0) { throw "SSH tunnel exited with code $LASTEXITCODE" }
