# revio uninstaller for Windows.
#
# Usage:
#   iwr https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/uninstall.ps1 | iex
#
# What it removes (after confirmation):
#   1. Install dir (the venv + cloned repo)            ~ 150 MB - 1.5 GB
#   2. Launcher (revio.cmd) + PATH entry
#   3. (optional, asks separately) ~/.cache/revio       fix history + checkpoints
#   4. (optional, asks separately) ~/.config/revio      user config + skills
#
# System-wide static analyzers (oxlint, cppcheck, etc.) installed via winget/
# scoop/npm are NOT touched - they may be useful to other tools.

$ErrorActionPreference = 'Continue'

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding            = [System.Text.UTF8Encoding]::new()
} catch { }

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }

function Ask-YesNo($prompt, $default = 'n') {
    $hint = if ($default -eq 'y') { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $r = (Read-Host "  $prompt $hint").Trim().ToLower()
        if ($r -eq '') { $r = $default }
        if ($r -in 'y','yes') { return $true }
        if ($r -in 'n','no')  { return $false }
    }
}

Write-Host ""
Write-Host "revio uninstaller" -ForegroundColor Cyan
Write-Host ""

# --- Discover install location ----------------------------------------------

$candidates = @()
if ($env:REVIO_HOME) { $candidates += $env:REVIO_HOME }
$candidates += Join-Path $env:LOCALAPPDATA 'revio'

$installDir = $null
foreach ($c in $candidates) {
    if (Test-Path (Join-Path $c '.venv\Scripts\revio.exe')) { $installDir = $c; break }
    if (Test-Path (Join-Path $c 'install-metadata.json'))   { $installDir = $c; break }
}

if (-not $installDir) {
    $custom = (Read-Host "  Install path not auto-detected. Enter it now (or blank to abort)").Trim('"').Trim()
    if (-not $custom -or -not (Test-Path $custom)) { Warn "nothing to uninstall"; exit 0 }
    $installDir = $custom
}
Info "Found: $installDir"

# Try to read metadata for the bin dir; fall back to convention.
$binDir = Join-Path $installDir 'bin'
$metaFile = Join-Path $installDir 'install-metadata.json'
if (Test-Path $metaFile) {
    try {
        $meta = Get-Content $metaFile -Raw | ConvertFrom-Json
        if ($meta.bin_dir) { $binDir = $meta.bin_dir }
    } catch { }
}

# --- Confirm primary removal ------------------------------------------------

$size = (Get-ChildItem -Path $installDir -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
$sizeMB = if ($size) { [math]::Round($size / 1MB, 1) } else { 'unknown' }
Info "Disk usage: $sizeMB MB"
Info ""

if (-not (Ask-YesNo "Remove install dir AND launcher? (caches stay)" 'y')) {
    Info "cancelled"; exit 0
}

# --- 1. Launcher + PATH -----------------------------------------------------

if (Test-Path $binDir) {
    Remove-Item -Path (Join-Path $binDir 'revio.cmd') -Force -ErrorAction SilentlyContinue
    Ok "removed launcher: $binDir\revio.cmd"
    # If bin dir is empty and was ours, remove it
    if (-not (Get-ChildItem -Path $binDir -ErrorAction SilentlyContinue)) {
        Remove-Item -Path $binDir -Force -ErrorAction SilentlyContinue
    }
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -and ($userPath.Split(';') -contains $binDir)) {
    $newPath = ($userPath.Split(';') | Where-Object { $_ -ne $binDir }) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Ok "removed $binDir from user PATH"
}

# --- 2. Install dir ---------------------------------------------------------

try {
    Remove-Item -Path $installDir -Recurse -Force -ErrorAction Stop
    Ok "removed install dir: $installDir"
} catch {
    Warn "could not fully remove $installDir : $_"
    Warn "rerun PowerShell as administrator if files are locked, or delete manually."
}

# --- 3. Caches (optional) ---------------------------------------------------

$cachePath  = Join-Path $env:USERPROFILE '.cache\revio'
$configPath = Join-Path $env:USERPROFILE '.config\revio'

if (Test-Path $cachePath) {
    $cSize = (Get-ChildItem $cachePath -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $cMB = if ($cSize) { [math]::Round($cSize / 1MB, 1) } else { 0 }
    Info ""
    Info "Cache (fix history, checkpoints, RAG index): $cachePath ($cMB MB)"
    if (Ask-YesNo "Remove cache?" 'n') {
        Remove-Item $cachePath -Recurse -Force -ErrorAction SilentlyContinue
        Ok "cache removed"
    } else {
        Info "kept (re-installing keeps your fix history + finding database)"
    }
}

if (Test-Path $configPath) {
    Info ""
    Info "Config (config.toml + skills): $configPath"
    if (Ask-YesNo "Remove config + custom skills?" 'n') {
        Remove-Item $configPath -Recurse -Force -ErrorAction SilentlyContinue
        Ok "config removed"
    } else {
        Info "kept (you can re-install without re-running the wizard)"
    }
}

# --- 4. HuggingFace embedding cache (separate question because shared) ----

$hfCache = Join-Path $env:USERPROFILE '.cache\huggingface'
if (Test-Path $hfCache) {
    $hSize = (Get-ChildItem $hfCache -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $hMB = if ($hSize) { [math]::Round($hSize / 1MB, 1) } else { 0 }
    Info ""
    Info "HuggingFace cache (RAG embedding models, may be used by other tools): $hfCache ($hMB MB)"
    if (Ask-YesNo "Remove HuggingFace cache too?" 'n') {
        Remove-Item $hfCache -Recurse -Force -ErrorAction SilentlyContinue
        Ok "HuggingFace cache removed"
    } else {
        Info "kept (shared with other ML tools)"
    }
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  revio removed" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Open a new PowerShell to refresh PATH."
Write-Host "  Re-install:  iwr https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.ps1 | iex"
Write-Host ""
