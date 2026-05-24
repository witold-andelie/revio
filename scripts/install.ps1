# revio - one-click installer for Windows (PowerShell).
#
# Usage:
#   iwr https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.ps1 | iex
#
# What it does (7 stages, each with progress visible):
#   [1/7] Locates Python >= 3.11 (offers winget install if missing)
#   [2/7] Locates git
#   [3/7] Asks for install location  (defaults to %LOCALAPPDATA%\revio
#                                     but offers other drives if larger)
#   [4/7] Clones the repo
#   [5/7] Creates venv + installs revio core (~150 MB)
#   [6/7] Optional: installs RAG extras (heavy ~1 GB) and per-language
#         static analyzers (asks BEFORE downloading anything)
#   [7/7] Adds launcher to PATH

$ErrorActionPreference = 'Stop'

# Force UTF-8 output - Windows codepages render Unicode markers as '???'.
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding            = [System.Text.UTF8Encoding]::new()
} catch { }

$RepoUrl       = 'https://github.com/witold-andelie/revio.git'
$DefaultDir    = if ($env:REVIO_HOME) { $env:REVIO_HOME } else { Join-Path $env:LOCALAPPDATA 'revio' }
$PyMinMajor    = 3
$PyMinMinor    = 11
$ScriptStart   = Get-Date

# --- output helpers (ASCII-safe markers) ------------------------------------

$script:CurStep = 0
$script:TotalSteps = 7
function Stage($title) {
    $script:CurStep++
    $elapsed = '{0:mm\:ss}' -f ((Get-Date) - $ScriptStart)
    Write-Host ""
    Write-Host "[$script:CurStep/$script:TotalSteps] $title  " -ForegroundColor Cyan -NoNewline
    Write-Host "(t+$elapsed)" -ForegroundColor DarkGray
}
function Info($m)    { Write-Host "    $m" }
function Ok($m)      { Write-Host "    [OK]   $m" -ForegroundColor Green }
function Warn($m)    { Write-Host "    [WARN] $m" -ForegroundColor Yellow }
function Die($m)     { Write-Host "    [FAIL] $m" -ForegroundColor Red; exit 1 }

# Run a native command without letting stderr trip ErrorActionPreference=Stop.
function Invoke-Probe {
    param([scriptblock]$Body)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $null = & $Body 2>&1 | Out-Null; return $LASTEXITCODE }
    catch { return 1 }
    finally { $ErrorActionPreference = $prev }
}

# Stream a native command's stdout+stderr through Write-Host without
# letting stderr writes trip ErrorActionPreference=Stop. Filter is an
# optional regex to suppress noise.
function Invoke-NativeStream {
    param(
        [scriptblock]$Body,
        [string]$Prefix = '    ',
        [string]$Filter = $null
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Body 2>&1 | ForEach-Object {
            $line = $_.ToString()
            if (-not $Filter -or $line -match $Filter) {
                Write-Host "$Prefix$line" -ForegroundColor DarkGray
            }
        }
        return $LASTEXITCODE
    } catch {
        return 1
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Ask-YesNo($prompt, $default = 'n') {
    $hint = if ($default -eq 'y') { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $r = (Read-Host "$prompt $hint").Trim().ToLower()
        if ($r -eq '') { $r = $default }
        if ($r -in 'y','yes') { return $true }
        if ($r -in 'n','no')  { return $false }
        Write-Host "    please answer y or n" -ForegroundColor Yellow
    }
}

function Get-DriveFreeGB($driveLetter) {
    try {
        $d = Get-PSDrive -Name $driveLetter -ErrorAction Stop
        return [math]::Round($d.Free / 1GB, 1)
    } catch { return 'unknown' }
}

# === Banner =================================================================

Write-Host ""
Write-Host "revio installer" -ForegroundColor Cyan -NoNewline
Write-Host "  -  agentic code review CLI" -ForegroundColor DarkGray
Write-Host "Footprint: ~150 MB core, +1 GB if you opt into RAG (we'll ask)."
Write-Host ""

# === [1/7] Python ==========================================================

Stage "Checking Python $PyMinMajor.$PyMinMinor+"
$python = $null
foreach ($cand in 'python3.13','python3.12','python3.11','python','py') {
    if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
    if ((Invoke-Probe { & $cand -c "import sys;sys.exit(0 if sys.version_info >= ($PyMinMajor,$PyMinMinor) else 1)" }) -eq 0) {
        $python = $cand; break
    }
}
if (-not $python) {
    Warn "No Python >= $PyMinMajor.$PyMinMinor found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask-YesNo "Install Python 3.12 via winget now?" 'y') {
            Invoke-NativeStream { winget install --silent --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements } | Out-Null
            $python = 'python'
        } else { Die "Install Python from https://www.python.org/downloads/ and re-run." }
    } else { Die "winget not available. Install Python from https://www.python.org/downloads/ and re-run." }
}
$pyver = (& $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null)
if (-not $pyver) { $pyver = '?' }
Ok "$python ($pyver)"

# === [2/7] Git ==============================================================

Stage "Checking git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if (Ask-YesNo "Install git via winget?" 'y') {
            Invoke-NativeStream { winget install --silent --id Git.Git --accept-source-agreements --accept-package-agreements } | Out-Null
        } else { Die "git required. Install from https://git-scm.com/download/win" }
    } else { Die "git not found. Install from https://git-scm.com/download/win" }
}
Ok "git ready"

# === [3/7] Install location ================================================

Stage "Choose install location"

$pwdRoot = (Get-Location).Drive.Name  # current drive letter, e.g. 'D'
$defRoot = (Split-Path -Qualifier $DefaultDir).TrimEnd(':')

$defFree = Get-DriveFreeGB $defRoot
$pwdFree = Get-DriveFreeGB $pwdRoot

Info "Default: $DefaultDir  (${defRoot}: has $defFree GB free)"
if ($pwdRoot -ne $defRoot) {
    Info "You're currently on ${pwdRoot}: which has $pwdFree GB free."
    Write-Host "    Choose:" -ForegroundColor White
    Write-Host "      [1] Default   - $DefaultDir"
    Write-Host "      [2] Current   - $(Join-Path $PWD 'revio')"
    Write-Host "      [3] Custom    - you type the path"
    while ($true) {
        $choice = (Read-Host "    Selection [1/2/3]").Trim()
        if ($choice -in '','1') { $InstallDir = $DefaultDir; break }
        if ($choice -eq '2')    { $InstallDir = (Join-Path $PWD 'revio'); break }
        if ($choice -eq '3')    {
            $custom = (Read-Host "    Full path").Trim('"').Trim()
            if ($custom) { $InstallDir = $custom; break }
        }
        Write-Host "    enter 1, 2, or 3" -ForegroundColor Yellow
    }
} else {
    if (Ask-YesNo "Install to default location ($DefaultDir)?" 'y') {
        $InstallDir = $DefaultDir
    } else {
        $custom = (Read-Host "    Full path").Trim('"').Trim()
        if (-not $custom) { Die "no path entered" }
        $InstallDir = $custom
    }
}
$BinDir = Join-Path $InstallDir 'bin'
Ok "will install to: $InstallDir"

# === [4/7] Clone ============================================================

Stage "Cloning repository"
if (Test-Path (Join-Path $InstallDir '.git')) {
    Info "existing checkout found, pulling latest"
    $rc = Invoke-NativeStream { git -C $InstallDir fetch --progress origin }
    Invoke-Probe { git -C $InstallDir reset --hard origin/main } | Out-Null
    if ($rc -ne 0) { Die "git fetch failed (rc=$rc)" }
    Ok "updated to latest main"
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
    # --progress makes git emit byte/object counters to stderr; visible to user.
    $rc = Invoke-NativeStream { git clone --progress --depth 1 $RepoUrl $InstallDir }
    if ($rc -ne 0) { Die "git clone failed (rc=$rc)" }
    Ok "cloned"
}

# === [5/7] venv + core install =============================================

Stage "Creating virtualenv and installing core (~150 MB, 1-2 minutes)"
Invoke-Probe { & $python -m venv (Join-Path $InstallDir '.venv') } | Out-Null
$vpy = Join-Path $InstallDir '.venv\Scripts\python.exe'
Invoke-NativeStream { & $vpy -m pip install --upgrade pip } '    ' 'Downloading|Installing|Successfully' | Out-Null

# Core: agent runtime + CLI + base profiles. NO RAG, NO heavy ML deps.
$rc = Invoke-NativeStream { & $vpy -m pip install -e "$InstallDir[js,plc,python,languages]" } '    ' 'Downloading|Installing|Successfully|error|ERROR'
if ($rc -ne 0) { Die "core install failed (rc=$rc)" }
Ok "core installed"

# === [6/7] Optional extras ==================================================

Stage "Optional: RAG (heavy ~1 GB) + per-language static analyzers"
Write-Host ""
Write-Host "    Picking analyzers for the languages you ACTUALLY use" -ForegroundColor White
Write-Host "    significantly improves revio's accuracy on those languages." -ForegroundColor DarkGray
Write-Host ""

# 6a. RAG -------------------------------------------------------------------
Write-Host "    --- RAG (search your coding guidelines as context) ---" -ForegroundColor White
Info "Adds chromadb + sentence-transformers + torch (~1 GB on disk)."
Info "If you won't index company guidelines, skip this. Can be added later."
if (Ask-YesNo "Install RAG dependencies now?" 'n') {
    $rc = Invoke-NativeStream { & $vpy -m pip install -e "$InstallDir[rag]" } '    ' 'Downloading|Installing|Successfully|error|ERROR'
    if ($rc -eq 0) { Ok "RAG extras installed" }
    else { Warn "RAG install failed (rc=$rc) - revio works fine without it" }
} else {
    Info "skipping RAG. Add later: $vpy -m pip install -e ${InstallDir}[rag]"
}

# 6b. Static analyzers per language ----------------------------------------
Write-Host ""
Write-Host "    --- Static analyzers (one per language; tiny binaries) ---" -ForegroundColor White

function Install-Analyzer {
    param(
        [string]$Name, [string]$CheckCmd,
        [string]$WingetId, [string]$ScoopId, [string]$NpmId,
        [string]$ManualHint
    )
    if (Get-Command $CheckCmd -ErrorAction SilentlyContinue) { Ok "$Name already installed"; return $true }
    if ($NpmId -and (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "    -> npm install -g $NpmId" -ForegroundColor DarkGray
        $rc = Invoke-Probe { npm install -g $NpmId --silent }
        if ($rc -eq 0) { Ok "$Name installed via npm"; return $true } else { Warn "$Name npm install failed (rc=$rc)"; return $false }
    }
    if ($WingetId -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "    -> winget install $WingetId" -ForegroundColor DarkGray
        $rc = Invoke-Probe { winget install --silent --id $WingetId --accept-source-agreements --accept-package-agreements }
        if ($rc -eq 0) { Ok "$Name installed via winget"; return $true } else { Warn "$Name winget install failed (rc=$rc)"; return $false }
    }
    if ($ScoopId -and (Get-Command scoop -ErrorAction SilentlyContinue)) {
        Write-Host "    -> scoop install $ScoopId" -ForegroundColor DarkGray
        $rc = Invoke-Probe { scoop install $ScoopId }
        if ($rc -eq 0) { Ok "$Name installed via scoop"; return $true } else { Warn "$Name scoop install failed (rc=$rc)"; return $false }
    }
    if ($ManualHint) { Warn "${Name}: $ManualHint" } else { Warn "${Name}: no compatible package manager; revio falls back gracefully" }
    return $false
}

# Each analyzer keyed by a single mnemonic letter.
# Layout: code | full label | command-probe | winget | scoop | npm | manual hint
$AnalyzerMap = [ordered]@{
    'j' = @{ Label='JS / TypeScript    (oxlint)';     Check='oxlint';        Npm='oxlint' }
    'c' = @{ Label='C / C++            (cppcheck)';   Check='cppcheck';      Winget='Cppcheck.Cppcheck';           Scoop='cppcheck' }
    'g' = @{ Label='Go                 (golangci-lint)'; Check='golangci-lint'; Winget='golangci-lint.golangci-lint'; Scoop='golangci-lint' }
    'r' = @{ Label='Rust               (clippy)';     Check='cargo-clippy';  Manual='comes with rustup: rustup component add clippy' }
    'a' = @{ Label='Java               (spotbugs)';   Check='spotbugs';      Winget='SpotBugs.SpotBugs'; Manual='needs JDK; download from spotbugs.github.io' }
    's' = @{ Label='Shell              (shellcheck)'; Check='shellcheck';    Winget='koalaman.shellcheck';        Scoop='shellcheck' }
    'l' = @{ Label='Lua                (luacheck)';   Check='luacheck';      Scoop='luacheck';   Manual='install Scoop (https://scoop.sh) then: scoop install luacheck' }
    'q' = @{ Label='SQL                (sqlfluff)';   Check='__SQLFLUFF__' }                              # pip-installed into revio venv
    'v' = @{ Label='Verilog            (verilator)'; Check='verilator';     Scoop='verilator';  Manual='install Scoop then: scoop install verilator, or use WSL' }
    'u' = @{ Label='Ruby               (rubocop)';   Check='rubocop';       Manual='install Ruby + gem install rubocop' }
    'h' = @{ Label='PHP                (phpstan)';   Check='phpstan';       Manual='install PHP + Composer + composer global require phpstan/phpstan' }
    'k' = @{ Label='Kotlin             (detekt)';    Check='detekt';        Scoop='detekt';     Manual='needs JDK; download detekt-cli from GitHub' }
}

# --- Print the letter-coded menu (two columns to save vertical space) ---
$letters = @($AnalyzerMap.Keys)
$rows    = [math]::Ceiling($letters.Count / 2)
Write-Host ""
Write-Host "    Static analyzers - type letter codes (no separator needed):" -ForegroundColor White
Write-Host ""
for ($i = 0; $i -lt $rows; $i++) {
    $leftKey  = $letters[$i]
    $rightKey = if ($i + $rows -lt $letters.Count) { $letters[$i + $rows] } else { $null }
    $left  = "  [$leftKey]  $($AnalyzerMap[$leftKey].Label)".PadRight(48)
    $right = if ($rightKey) { "  [$rightKey]  $($AnalyzerMap[$rightKey].Label)" } else { "" }
    Write-Host "  $left$right"
}
Write-Host ""
Write-Host "    Python (bandit) is auto-installed via the [python] extra." -ForegroundColor DarkGray
Write-Host ""
Write-Host "    Type the letters for languages you use, e.g. " -NoNewline
Write-Host "jcqs" -ForegroundColor Cyan -NoNewline
Write-Host " for JS+C+SQL+Shell."
Write-Host "    Or type " -NoNewline; Write-Host "*" -ForegroundColor Cyan -NoNewline; Write-Host " to install ALL, or press " -NoNewline
Write-Host "Enter" -ForegroundColor Cyan -NoNewline; Write-Host " to skip all."
Write-Host ""

$rawSelection = (Read-Host "    Your selection").Trim().ToLower()

# --- Resolve selection into ordered, deduped list of letters ----------------
$selectedLetters = @()
if ($rawSelection -eq '*') {
    $selectedLetters = $letters
} elseif ($rawSelection -ne '') {
    # Walk char-by-char, keep order of first appearance, ignore unknown chars
    $seen = @{}
    foreach ($ch in $rawSelection.ToCharArray()) {
        $c = $ch.ToString()
        if ($AnalyzerMap.Contains($c) -and -not $seen.ContainsKey($c)) {
            $selectedLetters += $c
            $seen[$c] = $true
        } elseif ($c -match '[a-z]' -and -not $AnalyzerMap.Contains($c)) {
            Warn "unknown letter '$c' - skipping"
        }
    }
}

if ($selectedLetters.Count -eq 0) {
    Info "no analyzers selected; LLM + AST still work. Re-run installer anytime to add."
} else {
    Info "installing: $($selectedLetters -join ', ')"
    foreach ($code in $selectedLetters) {
        $entry = $AnalyzerMap[$code]
        if ($entry.Check -eq '__SQLFLUFF__') {
            $rc = Invoke-Probe { & $vpy -m sqlfluff --version }
            if ($rc -eq 0) { Ok 'sqlfluff already in revio venv' }
            else {
                Write-Host "    -> $vpy -m pip install sqlfluff" -ForegroundColor DarkGray
                $ircSql = Invoke-NativeStream { & $vpy -m pip install sqlfluff } '    ' 'Downloading|Installing|Successfully|error|ERROR'
                if ($ircSql -eq 0) { Ok 'sqlfluff installed' } else { Warn "sqlfluff install failed (rc=$ircSql)" }
            }
            continue
        }
        Install-Analyzer -Name $entry.Label -CheckCmd $entry.Check `
            -WingetId $entry.Winget -ScoopId $entry.Scoop -NpmId $entry.Npm `
            -ManualHint $entry.Manual | Out-Null
    }
}

# === [7/7] Launcher + PATH =================================================

Stage "Installing launcher"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$launcher = @"
@echo off
"$InstallDir\.venv\Scripts\revio.exe" %*
"@
Set-Content -Path (Join-Path $BinDir 'revio.cmd') -Value $launcher -Encoding ASCII
Ok "launcher at $BinDir\revio.cmd"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not ($userPath.Split(';') -contains $BinDir)) {
    $newPath = if ([string]::IsNullOrEmpty($userPath)) { $BinDir } else { "$userPath;$BinDir" }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Ok "PATH updated (user scope)"
} else {
    Ok "PATH already contains $BinDir"
}

# Save metadata for the uninstall script
$metaPath = Join-Path $InstallDir 'install-metadata.json'
@{
    install_dir   = $InstallDir
    bin_dir       = $BinDir
    installed_at  = (Get-Date).ToString('o')
    py_version    = $pyver
} | ConvertTo-Json | Set-Content -Path $metaPath -Encoding UTF8

# === Finale =================================================================

# Refresh the CURRENT shell's $env:Path so the user can run `revio`
# immediately, without opening a new PowerShell. PATH change via
# [Environment]::SetEnvironmentVariable('User') only propagates to
# NEW processes — existing shells keep the old PATH cached at startup.
try {
    $env:Path = `
        [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + `
        [Environment]::GetEnvironmentVariable('Path','User')
} catch { }

$total = '{0:mm\:ss}' -f ((Get-Date) - $ScriptStart)
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  revio installed in $total" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Location: $InstallDir"
Write-Host "  Launcher: $BinDir\revio.cmd  (in PATH)"
Write-Host ""
Write-Host "  NEXT STEPS:" -ForegroundColor Cyan
Write-Host "    1. cd into any code folder you want to review"
Write-Host "    2. Run:  " -NoNewline; Write-Host "revio" -ForegroundColor Cyan -NoNewline; Write-Host " (interactive REPL)"
Write-Host "       or:  " -NoNewline; Write-Host "revio audit ." -ForegroundColor Cyan -NoNewline; Write-Host "  (one-shot full-repo scan)"
Write-Host ""
Write-Host "  [The PATH was refreshed for this PowerShell window, so 'revio'" -ForegroundColor DarkGray
Write-Host "   works immediately. New windows pick it up automatically.]" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Uninstall later:" -ForegroundColor DarkGray
Write-Host "    iwr https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/uninstall.ps1 | iex"
Write-Host ""
