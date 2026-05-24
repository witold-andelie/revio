#!/usr/bin/env bash
# revio - one-click installer for macOS and Linux.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.sh | bash
#
# What it does (7 stages, with progress visible):
#   [1/7] Checks Python >= 3.11
#   [2/7] Checks git
#   [3/7] Asks where to install (default ~/.local/share/revio, can override)
#   [4/7] Clones the repo (with git --progress)
#   [5/7] Creates venv + installs revio core (~150 MB)
#   [6/7] Optionally installs RAG (~1 GB) + per-language static analyzers
#         (we ASK before each download)
#   [7/7] Adds launcher shim to ~/.local/bin/revio

set -euo pipefail

REPO_URL="https://github.com/witold-andelie/revio.git"
DEFAULT_DIR="${REVIO_HOME:-$HOME/.local/share/revio}"
BIN_DIR="${REVIO_BIN_DIR:-$HOME/.local/bin}"
PY_MIN_MAJOR=3
PY_MIN_MINOR=11
SCRIPT_START_EPOCH=$(date +%s)

# --- output helpers ----------------------------------------------------------

c_cyan()  { printf '\033[36m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_yellow(){ printf '\033[33m%s\033[0m' "$*"; }
c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_dim()   { printf '\033[2m%s\033[0m' "$*"; }

CUR_STEP=0
TOTAL_STEPS=7
stage() {
    CUR_STEP=$((CUR_STEP + 1))
    elapsed=$(( $(date +%s) - SCRIPT_START_EPOCH ))
    em=$((elapsed / 60)); es=$((elapsed % 60))
    printf "\n[%d/%d] %s  $(c_dim "(t+%02d:%02d)")\n" "$CUR_STEP" "$TOTAL_STEPS" "$1" "$em" "$es"
}
info() { echo "    $*"; }
ok()   { echo "    $(c_green '[OK]')   $*"; }
warn() { echo "    $(c_yellow '[WARN]') $*" >&2; }
die()  { echo "    $(c_red '[FAIL]') $*" >&2; exit 1; }

ask_yes_no() {
    # ask_yes_no "Prompt" "y|n"   (default)
    local prompt="$1" default="${2:-n}" hint="[y/N]" ans
    [ "$default" = "y" ] && hint="[Y/n]"
    while true; do
        # Read from /dev/tty so this works under `curl | bash`
        if [ -t 0 ]; then
            printf "    %s %s " "$prompt" "$hint"
            read -r ans </dev/tty || ans=""
        else
            printf "    %s %s " "$prompt" "$hint"
            read -r ans </dev/tty 2>/dev/null || ans=""
        fi
        ans="${ans:-$default}"
        case "${ans,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *)     echo "    please answer y or n" >&2 ;;
        esac
    done
}

# --- platform detection ------------------------------------------------------

OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *)      die "Unsupported OS: $OS (this script supports macOS + Linux; use install.ps1 on Windows)" ;;
esac

# === Banner ==================================================================

echo
echo "$(c_cyan 'revio installer')  $(c_dim '- agentic code review CLI')"
echo "Footprint: ~150 MB core, +1 GB if you opt into RAG (we'll ask)."
echo

# === [1/7] Python ===========================================================

stage "Checking Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+"
PYTHON=""
for c in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c "import sys; sys.exit(0 if sys.version_info >= (${PY_MIN_MAJOR},${PY_MIN_MINOR}) else 1)" >/dev/null 2>&1; then
            PYTHON="$c"; break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Need Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+. Install from https://www.python.org/downloads/ (or your distro's package manager)."
PYVER=$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
ok "using $PYTHON ($PYVER)"

# === [2/7] Git ==============================================================

stage "Checking git"
command -v git >/dev/null 2>&1 || die "git not found. Install with your package manager."
ok "git ready"

# === [3/7] Install location =================================================

stage "Choose install location"

PWD_DEV=$(df -P "$PWD"        | tail -1 | awk '{print $1}')
DEF_PARENT="$(dirname "$DEFAULT_DIR")"
mkdir -p "$DEF_PARENT" 2>/dev/null || true
DEF_DEV=$(df -P "$DEF_PARENT" | tail -1 | awk '{print $1}')
PWD_FREE=$(df -h "$PWD"         | tail -1 | awk '{print $4}')
DEF_FREE=$(df -h "$DEF_PARENT"  | tail -1 | awk '{print $4}')

info "Default: $DEFAULT_DIR  ($DEF_FREE free)"
if [ "$PWD_DEV" != "$DEF_DEV" ]; then
    info "You're currently on a different volume ($PWD with $PWD_FREE free)."
    echo "    Choose:"
    echo "      [1] Default   - $DEFAULT_DIR"
    echo "      [2] Current   - $PWD/revio"
    echo "      [3] Custom    - you type the path"
    while true; do
        printf "    Selection [1/2/3]: "
        read -r choice </dev/tty || choice=""
        case "${choice:-1}" in
            1) INSTALL_DIR="$DEFAULT_DIR"; break ;;
            2) INSTALL_DIR="$PWD/revio"; break ;;
            3) printf "    Full path: "; read -r INSTALL_DIR </dev/tty; [ -n "$INSTALL_DIR" ] && break ;;
            *) echo "    enter 1, 2, or 3" ;;
        esac
    done
else
    if ask_yes_no "Install to default location ($DEFAULT_DIR)?" "y"; then
        INSTALL_DIR="$DEFAULT_DIR"
    else
        printf "    Full path: "
        read -r INSTALL_DIR </dev/tty
        [ -n "$INSTALL_DIR" ] || die "no path entered"
    fi
fi
ok "will install to: $INSTALL_DIR"

# === [4/7] Clone ============================================================

stage "Cloning repository"
if [ -d "$INSTALL_DIR/.git" ]; then
    info "existing checkout, pulling latest"
    git -C "$INSTALL_DIR" fetch --progress origin 2>&1 | sed 's/^/    /'
    git -C "$INSTALL_DIR" reset --hard origin/main >/dev/null
    ok "updated to latest main"
else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --progress --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>&1 | sed 's/^/    /'
    ok "cloned"
fi

# === [5/7] venv + core install ==============================================

stage "Creating virtualenv and installing core (~150 MB, 1-2 minutes)"
"$PYTHON" -m venv "$INSTALL_DIR/.venv"
VPY="$INSTALL_DIR/.venv/bin/python"
"$VPY" -m pip install --upgrade pip 2>&1 | grep -E 'Downloading|Installing|Successfully' | sed 's/^/    /' || true

# Core: agent runtime + CLI + base profiles. NO RAG, NO heavy ML deps.
# pip emits its native progress bar since we don't pass --quiet.
"$VPY" -m pip install -e "$INSTALL_DIR[js,plc,python,languages]" 2>&1 \
    | grep -E 'Downloading|Installing|Successfully|error' | sed 's/^/    /' || true
ok "core installed"

# === [6/7] Optional extras ==================================================

stage "Optional: RAG (heavy ~1 GB) + per-language static analyzers"
echo
echo "    Picking analyzers for the languages you ACTUALLY use significantly"
echo "    $(c_dim 'improves revio'\''s accuracy on those languages.')"
echo

# 6a. RAG --------------------------------------------------------------------
echo "    --- RAG (search your coding guidelines as context) ---"
info "Adds chromadb + sentence-transformers + torch (~1 GB on disk)."
info "Skip if you won't index company guidelines. Can be added later."
if ask_yes_no "Install RAG dependencies now?" "n"; then
    "$VPY" -m pip install -e "$INSTALL_DIR[rag]" 2>&1 \
        | grep -E 'Downloading|Installing|Successfully|error' | sed 's/^/    /' || true
    ok "RAG extras installed"
else
    info "skipping RAG. Add later: $VPY -m pip install -e $INSTALL_DIR[rag]"
fi

# 6b. Static analyzers per language -----------------------------------------
echo
echo "    --- Static analyzers (one tiny binary per language) ---"

# install_analyzer NAME CHECK_CMD BREW_PKG APT_PKG NPM_PKG MANUAL_HINT
install_analyzer() {
    local name="$1" check="$2" brew_pkg="${3:-}" apt_pkg="${4:-}" npm_pkg="${5:-}" hint="${6:-}"
    if command -v "$check" >/dev/null 2>&1; then ok "$name already installed"; return 0; fi
    if [ -n "$npm_pkg" ] && command -v npm >/dev/null 2>&1; then
        echo "    $(c_dim "-> npm install -g $npm_pkg")"
        if npm install -g "$npm_pkg" --silent >/dev/null 2>&1; then ok "$name installed via npm"; return 0; fi
        warn "$name npm install failed"
    fi
    if [ "$PLATFORM" = macos ] && [ -n "$brew_pkg" ] && command -v brew >/dev/null 2>&1; then
        echo "    $(c_dim "-> brew install $brew_pkg")"
        if brew install "$brew_pkg" >/dev/null 2>&1; then ok "$name installed via brew"; return 0; fi
        warn "$name brew install failed"
    fi
    if [ "$PLATFORM" = linux ] && [ -n "$apt_pkg" ] && command -v apt-get >/dev/null 2>&1; then
        echo "    $(c_dim "-> sudo apt-get install $apt_pkg")"
        if sudo apt-get install -y -qq "$apt_pkg" >/dev/null 2>&1; then ok "$name installed via apt"; return 0; fi
        warn "$name apt install failed"
    fi
    if [ -n "$hint" ]; then warn "$name: $hint"
    else warn "$name: no compatible package manager; revio falls back gracefully"; fi
    return 1
}

cat <<EOF
    [A] Install ALL languages (recommended for evaluation)
    [C] Custom - pick per language
    [N] None  - skip all (LLM + AST still work; add later)
EOF
printf "    Selection [A/C/N]: "
read -r MODE </dev/tty || MODE=""
MODE="${MODE:-A}"
MODE="${MODE^^}"

# Menu: name | check-cmd | brew | apt | npm | manual_hint | default-Y?
# (use '__SQLFLUFF__' as a sentinel for the pip-into-venv case)
declare -a ANALYZERS=(
    "JS/TS (oxlint)|oxlint|oxlint||oxlint||y"
    "Python (bandit) - already installed via [python] extra|bandit||||(installed)|y"
    "C/C++ (cppcheck)|cppcheck|cppcheck|cppcheck|||y"
    "Go (golangci-lint)|golangci-lint|golangci-lint|golangci-lint|||y"
    "Shell (shellcheck)|shellcheck|shellcheck|shellcheck|||y"
    "Lua (luacheck)|luacheck|luacheck|lua-check||luarocks install luacheck|n"
    "SQL (sqlfluff)|__SQLFLUFF__|||||y"
    "Verilog (verilator)|verilator|verilator|verilator||scoop on Windows or WSL|n"
    "Rust (clippy)|cargo-clippy|||||rustup component add clippy|n"
    "Java (spotbugs)|spotbugs|spotbugs|||needs JDK + download|n"
    "Ruby (rubocop)|rubocop||||gem install rubocop|n"
    "PHP (phpstan)|phpstan||||composer global require phpstan/phpstan|n"
    "Kotlin (detekt)|detekt|detekt|||needs JDK|n"
)

run_one() {
    local entry="$1"
    IFS='|' read -r name check brew_pkg apt_pkg npm_pkg hint _def <<<"$entry"
    if [ "$check" = "__SQLFLUFF__" ]; then
        if "$VPY" -m sqlfluff --version >/dev/null 2>&1; then ok "sqlfluff already in revio venv"; return 0; fi
        echo "    $(c_dim "-> $VPY -m pip install sqlfluff")"
        if "$VPY" -m pip install sqlfluff >/dev/null 2>&1; then ok "sqlfluff installed"
        else warn "sqlfluff install failed"; fi
        return 0
    fi
    install_analyzer "$name" "$check" "$brew_pkg" "$apt_pkg" "$npm_pkg" "$hint"
}

case "$MODE" in
    A) for entry in "${ANALYZERS[@]}"; do run_one "$entry" || true; done ;;
    N) info "skipping all analyzers" ;;
    *)
        echo "    Type Y to install, Enter to skip, for each:"
        for entry in "${ANALYZERS[@]}"; do
            IFS='|' read -r name _ _ _ _ _ def <<<"$entry"
            if ask_yes_no "      $name?" "$def"; then run_one "$entry" || true; fi
        done
        ;;
esac

# === [7/7] Launcher + PATH ==================================================

stage "Installing launcher"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/revio" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/.venv/bin/revio" "\$@"
EOF
chmod +x "$BIN_DIR/revio"
ok "launcher at $BIN_DIR/revio"

case ":$PATH:" in
    *":$BIN_DIR:"*) ok "$BIN_DIR already on PATH" ;;
    *)
        warn "$BIN_DIR is not on PATH yet."
        case "${SHELL:-}" in
            */zsh)  rcfile="$HOME/.zshrc" ;;
            */bash) rcfile="$HOME/.bashrc" ;;
            *)      rcfile="your shell config" ;;
        esac
        info "Add to $rcfile:"
        info "  $(c_cyan "export PATH=\"\$HOME/.local/bin:\$PATH\"")"
        ;;
esac

# Save metadata for the uninstall script
cat > "$INSTALL_DIR/install-metadata.json" <<EOF
{
  "install_dir": "$INSTALL_DIR",
  "bin_dir":     "$BIN_DIR",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "py_version":   "$PYVER"
}
EOF

# === Finale =================================================================

total=$(( $(date +%s) - SCRIPT_START_EPOCH ))
echo
echo "$(c_green '================================================================')"
echo "$(c_green '  revio installed in ') ${total}s"
echo "$(c_green '================================================================')"
echo
echo "  Location: $INSTALL_DIR"
echo "  Launcher: $BIN_DIR/revio  (in PATH)"
echo
echo "  $(c_cyan 'NEXT STEPS:')"
echo "    1. OPEN A NEW terminal (so PATH change loads), OR run 'source $rcfile'"
echo "    2. cd into any code folder you want to review"
echo "    3. Run:  $(c_cyan revio)  (interactive REPL)"
echo "       or:  $(c_cyan 'revio audit .')  (one-shot full-repo scan)"
echo
echo "  $(c_dim 'Uninstall later:')"
echo "    curl -sSL https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/uninstall.sh | bash"
echo
