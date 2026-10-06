param(
    [Parameter(Mandatory=$true)][ValidateSet('Stop','Start')][string]$Action,
    [string]$BackendRoot = 'C:\Users\xc133\projects\lka_backend',
    [string]$FrontendRoot = 'D:\agent-bot-frontend',
    [int]$BackendPort = 8765,
    [int]$FrontendPort = 8780
)
$ErrorActionPreference = 'Stop'
if ($Action -eq 'Start') {
    # The native launcher reuses the still-running frontend and paired credential.
    & "$FrontendRoot\run-lka-native-windows.ps1" -BackendRoot $BackendRoot `
        -BackendPort $BackendPort -FrontendPort $FrontendPort -NoPet -NoOpen
    exit
}
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Native Windows only.' }
$reader = Invoke-RestMethod "http://127.0.0.1:$FrontendPort/plugins/qq-reader/status" -TimeoutSec 5
if ($null -eq $reader.pending_count -or $reader.pending_count -ge 8000) {
    throw 'QQ outbox unavailable or close to capacity; leave the backend running.'
}
$recordPath = Join-Path "$FrontendRoot\.runtime" "lka-native-backend-$BackendPort.json"
$record = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
$expectedExecutable = Join-Path $BackendRoot '.venv\Scripts\python.exe'
$process = Get-Process -Id $record.process_id -ErrorAction Stop
if ($record.root -ne $BackendRoot -or $record.executable -ne $expectedExecutable -or
    $process.Path -ne $expectedExecutable -or
    $process.StartTime.ToUniversalTime().ToString('o') -ne $record.started_at) {
    throw 'Backend process identity mismatch; refusing to stop.'
}
& $expectedExecutable (Join-Path $PSScriptRoot 'production_release.py') preflight --target $BackendRoot
if ($LASTEXITCODE -ne 0) { throw 'Agent or background work is in flight; wait for an idle point.' }
# Only this verified API process. No /T process-tree kill: frontend, QQ, bridge,
# desktop pet, capture credentials, message schedules and media are untouched.
Stop-Process -Id $process.Id -ErrorAction Stop
$process.WaitForExit(5000) | Out-Null
if (-not $process.HasExited) { throw 'Backend did not stop.' }
Write-Output "Stopped verified backend PID $($process.Id); frontend/QQ collector remain running."
