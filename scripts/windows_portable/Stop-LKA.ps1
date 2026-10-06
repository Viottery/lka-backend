[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$BackendPort = 8765,
    [ValidateRange(1, 65535)][int]$FrontendPort = 8780
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Run this bundle with native Windows PowerShell.' }
$bundleRoot = $PSScriptRoot
$frontendRoot = Join-Path $bundleRoot 'frontend'
$backendRoot = Join-Path $bundleRoot 'backend'
$runtimeDir = Join-Path $frontendRoot '.runtime'
$script:stopFailed = $false
function Stop-RecordedProcess {
    param([string]$RecordPath, [string]$Root, [string]$Executable)
    if (-not (Test-Path -LiteralPath $RecordPath -PathType Leaf)) { return }
    try {
        $record = Get-Content -Raw -LiteralPath $RecordPath | ConvertFrom-Json
        if ($record.root -ne $Root -or $record.executable -ne $Executable) {
            $script:stopFailed = $true
            Write-Warning "Record belongs to a different installation: $RecordPath"
            return
        }
        $process = Get-Process -Id $record.process_id -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            Remove-Item -LiteralPath $RecordPath -Force
            return
        }
        if ($process.Path -ne $Executable -or
            $process.StartTime.ToUniversalTime().ToString('o') -ne $record.started_at) {
            $script:stopFailed = $true
            Write-Warning "Process identity no longer matches: $RecordPath"
            return
        }
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Could not stop recorded process $($process.Id)." }
        Remove-Item -LiteralPath $RecordPath -Force
    } catch {
        $script:stopFailed = $true
        Write-Warning "Could not stop process recorded in ${RecordPath}: $($_.Exception.Message)"
    }
}
Stop-RecordedProcess (Join-Path $runtimeDir "lka-portable-pet-$BackendPort-$FrontendPort.json") `
    $frontendRoot (Join-Path $bundleRoot 'runtime\java\bin\java.exe')
Stop-RecordedProcess (Join-Path $runtimeDir "lka-native-frontend-$FrontendPort.json") `
    $frontendRoot (Join-Path $frontendRoot '.venv\Scripts\python.exe')
Stop-RecordedProcess (Join-Path $runtimeDir "lka-native-backend-$BackendPort.json") `
    $backendRoot (Join-Path $backendRoot '.venv\Scripts\python.exe')

if ($script:stopFailed) { throw "Some recorded processes could not be stopped; see the warnings above." }
