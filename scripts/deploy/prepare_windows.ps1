param(
    [string]$PlanPath = (Join-Path $PSScriptRoot 'deployment.json'),
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'LKA'),
    [string]$Python = 'python',
    [string]$CloudHost = '',
    [string]$CloudUser = '',
    [ValidateRange(1, 65535)][int]$CloudPort = 22,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$RemoteStage = 'lka-deployment-incoming',
    [switch]$SkipDependencies,
    [switch]$SkipDownload
)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('Command failed: ' + [IO.Path]::GetFileName($Program)) }
}

function Get-PinnedCheckout {
    param([string]$Directory, [string]$Repository, [string]$Commit)
    if ($Commit -notmatch '^[a-f0-9]{40}$') { throw 'Invalid deployment commit.' }
    if (Test-Path -LiteralPath $Directory) {
        $top = (& git -C $Directory rev-parse --show-toplevel | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($top) -ne [IO.Path]::GetFullPath($Directory)) {
            throw ('Existing directory is not this checkout root: ' + $Directory)
        }
        $head = (& git -C $Directory rev-parse HEAD | Out-String).Trim()
        $status = (& git -C $Directory status --porcelain --untracked-files=all | Out-String).Trim()
        $origin = (& git -C $Directory remote get-url origin | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $head -ne $Commit -or $status -or $origin -ne $Repository) {
            throw ('Existing checkout differs from the pinned delivery. Preserve it and choose a new InstallRoot: ' + $Directory)
        }
        Write-Host ('Reusing pinned checkout: ' + $Directory)
        return
    }
    Invoke-Checked git @('init', '-b', 'main', $Directory)
    Invoke-Checked git @('-C', $Directory, 'remote', 'add', 'origin', $Repository)
    Invoke-Checked git @('-C', $Directory, 'fetch', '--no-tags', 'origin', $Commit)
    Invoke-Checked git @('-C', $Directory, 'checkout', '-b', 'main', 'FETCH_HEAD')
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Run this preparation tool with native Windows PowerShell.'
}
$plan = Get-Content -Raw -LiteralPath $PlanPath | ConvertFrom-Json
if ($plan.format -ne 1 -or
    $plan.backend.repository -ne 'https://github.com/Viottery/lka-backend.git' -or
    $plan.frontend.repository -ne 'https://github.com/Viottery/lka-desktop.git' -or
    $plan.handoff.repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
    $plan.handoff.tag -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') {
    throw 'Invalid deployment.json. Use the reviewed private handoff repository.'
}
Invoke-Checked git @('--version')
Invoke-Checked $Python @('-c', "import sys; assert sys.platform == 'win32' and sys.version_info >= (3,12), 'Native Windows Python 3.12+ is required'")
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$backend = Join-Path $InstallRoot 'lka-backend'
$frontend = Join-Path $InstallRoot 'lka-desktop'
Get-PinnedCheckout $backend $plan.backend.repository $plan.backend.commit
Get-PinnedCheckout $frontend $plan.frontend.repository $plan.frontend.commit
if (-not $SkipDependencies) {
    & (Join-Path $backend 'scripts\setup_windows.ps1') -Python $Python
    if ($LASTEXITCODE -ne 0) { throw 'Backend setup failed.' }
    $frontPython = Join-Path $frontend '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $frontPython)) {
        Invoke-Checked $Python @('-m', 'venv', (Join-Path $frontend '.venv'))
    }
    Invoke-Checked $frontPython @('-m', 'pip', 'install', '-r', (Join-Path $frontend 'requirements.txt'))
    if (-not (Test-Path -LiteralPath (Join-Path $frontend '.env'))) {
        Copy-Item -LiteralPath (Join-Path $frontend '.env.example') -Destination (Join-Path $frontend '.env')
    }
}
$package = Join-Path $InstallRoot ('migration-' + $plan.handoff.tag)
if (-not $SkipDownload) {
    Invoke-Checked $Python @((Join-Path $backend 'scripts\deploy\download_handoff.py'),
        '--repository', $plan.handoff.repository, '--tag', $plan.handoff.tag, '--output', $package)
}
if ($CloudHost) {
    if ($SkipDownload) { throw 'Do not combine CloudHost with SkipDownload; the transfer must first verify the package.' }
    if ($CloudHost -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$' -or
        ($CloudUser -and $CloudUser -notmatch '^[A-Za-z_][A-Za-z0-9_-]*$')) {
        throw 'Use a valid SSH host/alias and a separate CloudUser; no shell syntax is accepted.'
    }
    $target = if ($CloudUser) { $CloudUser + '@' + $CloudHost } else { $CloudHost }
    # The stage is one relative directory under the SSH user's home. It is never
    # /etc/lka or the live database. No remote services are changed here.
    Invoke-Checked ssh @('-p', "$CloudPort", $target,
        "umask 077; test -d '$RemoteStage' || mkdir -m 700 '$RemoteStage'")
    Invoke-Checked scp @('-P', "$CloudPort", '-r', $package, ($target + ':' + $RemoteStage + '/'))
    Invoke-Checked scp @('-P', "$CloudPort", $PlanPath, ($target + ':' + $RemoteStage + '/deployment.json'))
    Write-Host ('Encrypted package uploaded to ' + $target + ':~/' + $RemoteStage)
}
Write-Host ('Backend: ' + $backend)
Write-Host ('Frontend: ' + $frontend)
Write-Host ('Encrypted package: ' + $package)
Write-Host 'Preparation complete. Follow CODEX_DEPLOY.md to restore the cloud and enroll this independent Windows node.'
Write-Host 'No services were started. Do not start the default backend config before configuring replica authentication.'
