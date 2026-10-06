[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8780',
    [string]$BackendUrl = 'http://127.0.0.1:8765',
    [string]$ProfileId = '',
    [ValidateRange(100, 4096)][int]$Width = 360,
    [ValidateRange(100, 4096)][int]$Height = 520,
    [switch]$NoServer,
    [switch]$NoOpen,
    [switch]$Spine
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Run this bundle with native Windows PowerShell.' }
$frontendRoot = Split-Path -Parent $PSScriptRoot
$bundleRoot = Split-Path -Parent $frontendRoot
$java = Join-Path $bundleRoot 'runtime\java\bin\java.exe'
$javaRoot = Join-Path $frontendRoot 'desktop-pet-java'
$runtimeDir = Join-Path $frontendRoot '.runtime'
$logsDir = Join-Path $frontendRoot 'logs'
$BaseUrl = $BaseUrl.TrimEnd('/')
$BackendUrl = $BackendUrl.TrimEnd('/')
$frontendPort = ([uri]$BaseUrl).Port
$backendPort = ([uri]$BackendUrl).Port
if (-not $NoServer) {
    & (Join-Path $bundleRoot 'Start-LKA.ps1') -BackendPort $backendPort -FrontendPort $frontendPort -NoPet -NoOpen
}
if ($NoOpen) { return }
if (-not (Test-Path -LiteralPath $java -PathType Leaf)) { throw "Bundled Java runtime is missing: $java" }
New-Item -ItemType Directory -Force -Path $runtimeDir, $logsDir | Out-Null
$recordPath = Join-Path $runtimeDir "lka-portable-pet-$backendPort-$frontendPort.json"
if (Test-Path -LiteralPath $recordPath -PathType Leaf) {
    try {
        $record = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
        $process = Get-Process -Id $record.process_id -ErrorAction Stop
        if ($record.root -eq $frontendRoot -and $record.executable -eq $java -and
            $record.backend_url -eq $BackendUrl -and $record.frontend_url -eq $BaseUrl -and
            $process.Path -eq $java -and
            $process.StartTime.ToUniversalTime().ToString('o') -eq $record.started_at) {
            Write-Host "Reusing portable desktop pet (PID $($process.Id))."
            return
        }
    } catch { }
}
$profilesPath = Join-Path $frontendRoot 'data\pet\profiles.json'
$statePath = Join-Path $frontendRoot 'data\pet\state.json'
$profiles = Get-Content -Raw -Encoding UTF8 -LiteralPath $profilesPath | ConvertFrom-Json
if (-not $ProfileId -and (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    try { $ProfileId = [string](Get-Content -Raw -Encoding UTF8 -LiteralPath $statePath | ConvertFrom-Json).active_profile_id } catch { }
}
if (-not $ProfileId -and $profiles.Count -gt 0) { $ProfileId = [string]$profiles[0].id }
$profile = $profiles | Where-Object { $_.id -eq $ProfileId } | Select-Object -First 1
if (-not $profile -or -not $profile.spine.skeleton_url -or -not $profile.spine.atlas_url) {
    throw "No valid Spine profile found for: $ProfileId"
}
$animation = if ($profile.spine.animation_map.idle) { [string]$profile.spine.animation_map.idle } else { [string]$profile.default_animation }
$scale = if ($profile.spine.skeleton_scale) { [string]$profile.spine.skeleton_scale } else { '0.52' }
$xOffset = if ($null -ne $profile.spine.x_offset) { [string]$profile.spine.x_offset } else { '0' }
$floorOffset = if ($null -ne $profile.spine.floor_offset) { [string]$profile.spine.floor_offset } else { '24' }
$classpath = "$javaRoot\build\classes\java\main;$javaRoot\build\resources\main;$javaRoot\lib\*"
$arguments = @("-Djavafx.cachedir=$runtimeDir\javafx-cache", '-cp', $classpath, 'com.agenticrag.pet.SpinePetGdxLauncher',
    "--project-root=$frontendRoot", "--profile-id=$ProfileId",
    "--skeleton-url=$($profile.spine.skeleton_url)", "--atlas-url=$($profile.spine.atlas_url)",
    "--animation=$animation", "--panel-url=$BaseUrl/desktop-pet/?mode=panel",
    "--chat-url=$BaseUrl/desktop-pet/chat.html?backend=$([uri]::EscapeDataString($BackendUrl))",
    "--width=$Width", "--height=$Height", "--skeleton-scale=$scale", "--x-offset=$xOffset", "--floor-offset=$floorOffset")
# Start-Process joins ArgumentList; apply Windows argv escaping to each value.
$argumentLine = ($arguments | ForEach-Object {
    $escaped = [regex]::Replace($_, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    '"' + $escaped + '"'
}) -join ' '
$process = Start-Process -FilePath $java -ArgumentList $argumentLine -WorkingDirectory $frontendRoot `
    -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logsDir "portable-pet-$backendPort-$frontendPort.out.log") `
    -RedirectStandardError (Join-Path $logsDir "portable-pet-$backendPort-$frontendPort.err.log") -PassThru
Start-Sleep -Milliseconds 800
$process.Refresh()
if ($process.HasExited) { throw "Desktop pet exited. See $logsDir\portable-pet-$backendPort-$frontendPort.err.log" }
@{ process_id = $process.Id; started_at = $process.StartTime.ToUniversalTime().ToString('o');
    root = $frontendRoot; executable = $java; backend_url = $BackendUrl; frontend_url = $BaseUrl } |
    ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
Write-Host "Started portable desktop pet (PID $($process.Id))."
