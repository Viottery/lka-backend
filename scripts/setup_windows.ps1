param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "Run this script with native Windows PowerShell."
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    $uvPath = if ($uv) { $uv.Source } else { Join-Path $repoRoot ".bootstrap\Scripts\uv.exe" }
    if (-not (Test-Path -LiteralPath $uvPath)) {
        & $Python -m venv .bootstrap
        if ($LASTEXITCODE -ne 0) { throw "Could not create the local bootstrap environment." }
        & .\.bootstrap\Scripts\python.exe -m pip install uv
        if ($LASTEXITCODE -ne 0) { throw "Could not install uv in the bootstrap environment." }
    }
    $arguments = @("sync", "--locked", "--python", $Python)
    & $uvPath @arguments
    if ($LASTEXITCODE -ne 0) { throw "Dependency sync failed." }
    if (-not (Test-Path -LiteralPath ".env")) { Copy-Item .env.example .env }
    if (-not (Test-Path -LiteralPath "config\local.toml")) {
        Copy-Item config\local.example.toml config\local.toml
    }
    Write-Host "Ready. Start: .\.venv\Scripts\python.exe scripts\start_backend.py personal"
} finally {
    Pop-Location
}
