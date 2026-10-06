param([ValidateSet('Stop','Start')][string]$Action)
$ErrorActionPreference = 'Stop'
$lkaFrontendRoot = 'D:\agent-bot-frontend'
$lkaBackendRoot = 'C:\Users\xc133\projects\lka_backend'
if ($Action -eq 'Start') {
    & "$lkaFrontendRoot\run-lka-native-windows.ps1" -BackendRoot $lkaBackendRoot -NoPet -NoOpen
    exit
}
# Only recorded, identity-verified service process trees. QQ/NapCat and desktop
# pet are intentionally untouched. Run the Python read-only preflight first.
foreach ($service in @(@{Name='frontend';Root=$lkaFrontendRoot;Port=8780}, @{Name='backend';Root=$lkaBackendRoot;Port=8765})) {
    $recordPath = Join-Path "$lkaFrontendRoot\.runtime" "lka-native-$($service.Name)-$($service.Port).json"
    $record = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
    $expectedExecutable = Join-Path $service.Root '.venv\Scripts\python.exe'
    $process = Get-Process -Id $record.process_id -ErrorAction Stop
    if ($record.root -ne $service.Root -or $record.executable -ne $expectedExecutable -or
        $process.Path -ne $record.executable -or $process.StartTime.ToUniversalTime().ToString('o') -ne $record.started_at) {
        throw 'Recorded service identity mismatch; refusing to stop process'
    }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Verified service stop failed' }
    Write-Output "Stopped verified $($service.Name) $($process.Id)"
}
