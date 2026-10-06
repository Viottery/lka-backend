[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$BackendPort = 8765,
    [ValidateRange(1, 65535)][int]$FrontendPort = 8780,
    [switch]$NoPet,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Run this bundle with native Windows PowerShell.' }
if ($BackendPort -eq $FrontendPort) { throw 'Backend and frontend ports must differ.' }
$bundleRoot = $PSScriptRoot
$python = Join-Path $bundleRoot 'runtime\python\python.exe'
$bootstrap = Join-Path $bundleRoot 'scripts\bootstrap.py'
$frontendRoot = Join-Path $bundleRoot 'frontend'
$backendRoot = Join-Path $bundleRoot 'backend'
$workspaces = Join-Path $bundleRoot 'workspaces'
$savedEnvironment = @{}
$overrides = @{
    PYTHONNOUSERSITE = '1'; PYTHONHOME = $null; PYTHONPATH = $null;
    LKA_SESSION_WORKSPACE_BASE = $workspaces; LKA_WORKSPACE_ROOTS = $workspaces
}
try {
    foreach ($key in $overrides.Keys) {
        $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $overrides[$key], 'Process')
    }
    & $python $bootstrap prepare
    if ($LASTEXITCODE -ne 0) { throw "Bundle preparation failed (exit $LASTEXITCODE)." }
    New-Item -ItemType Directory -Force -Path $workspaces | Out-Null
    & (Join-Path $frontendRoot 'run-lka-native-windows.ps1') -BackendRoot $backendRoot `
        -BackendPort $BackendPort -FrontendPort $FrontendPort -NoPet -NoOpen
    $backendUrl = "http://127.0.0.1:$BackendPort"
    $frontendUrl = "http://127.0.0.1:$FrontendPort"
    & $python $bootstrap seed-config --backend-url $backendUrl
    if ($LASTEXITCODE -ne 0) { throw "Configuration import failed (exit $LASTEXITCODE)." }
    if (-not $NoPet) {
        & (Join-Path $frontendRoot 'scripts\start-desktop-pet-java.ps1') `
            -NoServer -Spine -BaseUrl $frontendUrl -BackendUrl $backendUrl
    } elseif (-not $NoOpen) {
        Start-Process "$frontendUrl/desktop-pet/chat.html?backend=$([uri]::EscapeDataString($backendUrl))"
    }
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
}
