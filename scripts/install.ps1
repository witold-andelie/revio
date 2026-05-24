# revio — one-click installer for Windows (PowerShell).
#
# Usage:
#   iwr https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.ps1 | iex
#
# What it does:
#   1. Checks Python >= 3.11 (offers winget install if missing)
#   2. Clones (or pulls) the repo to %LOCALAPPDATA%\revio
#   3. Creates a venv inside that directory
#   4. Installs revio + recommended language extras via pip
#   5. (Optional) installs static analyzers via winget / scoop / npm when present
#   6. Creates a launcher at %LOCALAPPDATA%\revio\bin\revio.cmd and adds it to PATH
#   7. Prompts you to run `revio` (which triggers the setup wizard)

$ErrorActionPreference = 'Stop'

$RepoUrl     = 'https://github.com/witold-andelie/revio.git'
$InstallDir  = if ($env:REVIO_HOME)    { $env:REVIO_HOME }    else { Join-Path $env:LOCALAPPDATA 'revio' }
$BinDir      = Join-Path $InstallDir 'bin'
$PyMinMajor  = 3
$PyMinMinor  = 11

function Step($m)    { Write-Host "▶ $m" -ForegroundColor Cyan }
function Ok($m)      { Write-Host "  ✓ $m" -ForegroundColor Green }
function Warn($m)    { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)     { Write-Host "  ✗ $m" -ForegroundColor Red; exit 1 }

# --- Python ------------------------------------------------------------------

Step "Looking for Python $PyMinMajor.$PyMinMinor+..."
$python = $null
foreach ($candidate in @('python3.13', 'python3.12', 'python3.11', 'python', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $ok = & $candidate -c "import sys; sys.exit(0 if sys.version_info >= ($PyMinMajor,$PyMinMinor) else 1)"
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch { }
}

if (-not $python) {
    Warn "No suitable Python found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Step "Attempting to install Python via winget..."
        winget install --silent --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        $python = 'python'
    } else {
        Die "Install Python $PyMinMajor.$PyMinMinor+ from https://www.python.org/downloads/ and re-run."
    }
}
$pyver = & $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Ok "using $python ($pyver)"

# --- git ---------------------------------------------------------------------

Step "Checking git..."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Step "Installing git via winget..."
        winget install --silent --id Git.Git --accept-source-agreements --accept-package-agreements
    } else {
        Die "git not found. Install from https://git-scm.com/download/win and re-run."
    }
}
Ok "git ready"

# --- clone or update ---------------------------------------------------------

if (Test-Path (Join-Path $InstallDir '.git')) {
    Step "Updating existing checkout at $InstallDir..."
    git -C $InstallDir fetch --quiet origin
    git -C $InstallDir reset --hard --quiet origin/main
    Ok "updated to latest main"
} else {
    Step "Cloning revio into $InstallDir..."
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
    git clone --quiet --depth 1 $RepoUrl $InstallDir
    Ok "cloned"
}

# --- venv + pip install ------------------------------------------------------

Step "Creating virtualenv..."
& $python -m venv (Join-Path $InstallDir '.venv')
$vpy = Join-Path $InstallDir '.venv\Scripts\python.exe'
& $vpy -m pip install --quiet --upgrade pip
Ok "venv at $InstallDir\.venv"

Step "Installing revio + extras (this may take a minute)..."
& $vpy -m pip install --quiet -e "$InstallDir[js,plc,python,languages]"
Ok "revio installed"

# --- launcher ----------------------------------------------------------------

Step "Creating launcher at $BinDir\revio.cmd..."
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$launcher = @"
@echo off
"$InstallDir\.venv\Scripts\revio.exe" %*
"@
Set-Content -Path (Join-Path $BinDir 'revio.cmd') -Value $launcher -Encoding ASCII
Ok "launcher ready"

# --- optional analyzers ------------------------------------------------------

Step "Looking for optional static analyzers (failures here are non-fatal)..."

function Install-Analyzer($name, $checkCmd, $wingetId, $scoopId, $npmId) {
    if (Get-Command $checkCmd -ErrorAction SilentlyContinue) { Ok "$name already installed"; return }
    if ($npmId -and (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "  → npm install -g $npmId" -ForegroundColor DarkGray
        try { npm install -g $npmId --silent 2>$null | Out-Null; Ok "$name installed via npm" } catch { Warn "$name npm install failed" }
        return
    }
    if ($wingetId -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "  → winget install $wingetId" -ForegroundColor DarkGray
        try { winget install --silent --id $wingetId --accept-source-agreements --accept-package-agreements 2>$null | Out-Null; Ok "$name installed via winget" } catch { Warn "$name winget install failed" }
        return
    }
    if ($scoopId -and (Get-Command scoop -ErrorAction SilentlyContinue)) {
        Write-Host "  → scoop install $scoopId" -ForegroundColor DarkGray
        try { scoop install $scoopId 2>$null | Out-Null; Ok "$name installed via scoop" } catch { Warn "$name scoop install failed" }
        return
    }
    Warn "$name not found, no compatible package manager available — falls back gracefully"
}

Install-Analyzer 'oxlint'        'oxlint'        ''                              ''         'oxlint'
Install-Analyzer 'cppcheck'      'cppcheck'      'Cppcheck.Cppcheck'             'cppcheck' ''
Install-Analyzer 'golangci-lint' 'golangci-lint' 'golangci-lint.golangci-lint'   'golangci-lint' ''
# spotbugs needs JDK — leave to user
# clippy ships with rustup — leave to user

# --- PATH ---------------------------------------------------------------------

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not ($userPath.Split(';') -contains $BinDir)) {
    Step "Adding $BinDir to your user PATH..."
    $newPath = if ([string]::IsNullOrEmpty($userPath)) { $BinDir } else { "$userPath;$BinDir" }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Ok "PATH updated (open a new shell to pick it up)"
} else {
    Ok "PATH already includes $BinDir"
}

# --- finale ------------------------------------------------------------------

Write-Host ""
Write-Host "✓ revio installed" -ForegroundColor Green
Write-Host ""
Write-Host "  Location:    $InstallDir"
Write-Host "  Launcher:    $BinDir\revio.cmd"
Write-Host ""
Write-Host "  Next step:   open a new PowerShell, run " -NoNewline
Write-Host "revio" -ForegroundColor Cyan -NoNewline
Write-Host " to start the setup wizard."
Write-Host ""
