#!/usr/bin/env bash
# revio — one-click installer for macOS and Linux.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.sh | bash
#
# What it does:
#   1. Checks Python >= 3.11
#   2. Clones (or pulls) the repo to ~/.local/share/revio
#   3. Creates a venv inside that directory
#   4. Installs revio + recommended language extras via pip
#   5. (Optional) installs static analyzers via brew (macOS) or apt (Linux)
#   6. Creates a shim at ~/.local/bin/revio
#   7. Prompts you to run `revio` (which triggers the setup wizard)

set -euo pipefail

REPO_URL="https://github.com/witold-andelie/revio.git"
INSTALL_DIR="${REVIO_HOME:-$HOME/.local/share/revio}"
BIN_DIR="${REVIO_BIN_DIR:-$HOME/.local/bin}"
PY_MIN_MAJOR=3
PY_MIN_MINOR=11

# --- pretty output -----------------------------------------------------------

c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_cyan()  { printf '\033[36m%s\033[0m' "$*"; }
c_dim()   { printf '\033[2m%s\033[0m' "$*"; }

step()  { echo "$(c_cyan '▶') $*"; }
ok()    { echo "  $(c_green '✓') $*"; }
warn()  { echo "  $(c_red '!') $*" >&2; }
die()   { echo "  $(c_red '✗') $*" >&2; exit 1; }

# --- platform detection ------------------------------------------------------

OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *)      die "Unsupported OS: $OS (this script supports macOS + Linux; use install.ps1 on Windows)" ;;
esac

# --- find a usable Python ----------------------------------------------------

step "Looking for Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+..."
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (${PY_MIN_MAJOR},${PY_MIN_MINOR}) else 1)" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Need Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+ on PATH. Install from https://www.python.org/downloads/"
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "using $PYTHON ($PYVER)"

# --- check git ---------------------------------------------------------------

step "Checking git..."
command -v git >/dev/null 2>&1 || die "git not found. Install it first."
ok "git ready"

# --- clone or update ---------------------------------------------------------

if [ -d "$INSTALL_DIR/.git" ]; then
    step "Updating existing checkout at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" fetch --quiet origin
    git -C "$INSTALL_DIR" reset --hard --quiet origin/main
    ok "updated to latest main"
else
    step "Cloning revio into $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --quiet --depth 1 "$REPO_URL" "$INSTALL_DIR"
    ok "cloned"
fi

# --- venv + pip install ------------------------------------------------------

step "Creating virtualenv..."
"$PYTHON" -m venv "$INSTALL_DIR/.venv"
VPY="$INSTALL_DIR/.venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
ok "venv at $INSTALL_DIR/.venv"

step "Installing revio + extras (this may take a minute)..."
"$VPY" -m pip install --quiet -e "$INSTALL_DIR[js,plc,python,languages]"
ok "revio installed"

# --- shim --------------------------------------------------------------------

step "Creating launcher at $BIN_DIR/revio..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/revio" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/.venv/bin/revio" "\$@"
EOF
chmod +x "$BIN_DIR/revio"
ok "launcher ready"

# --- optional analyzers ------------------------------------------------------

step "Looking for optional static analyzers (failures here are non-fatal)..."

install_analyzer() {
    local name="$1" check="$2" brew_pkg="${3:-}" apt_pkg="${4:-}"
    if command -v "$check" >/dev/null 2>&1; then
        ok "$name already installed"
        return
    fi
    if [ "$PLATFORM" = macos ] && command -v brew >/dev/null 2>&1 && [ -n "$brew_pkg" ]; then
        echo "  $(c_dim "→ brew install $brew_pkg")"
        brew install --quiet "$brew_pkg" >/dev/null 2>&1 && ok "$name installed via brew" \
            || warn "$name install failed — falls back gracefully"
    elif [ "$PLATFORM" = linux ] && command -v apt-get >/dev/null 2>&1 && [ -n "$apt_pkg" ]; then
        echo "  $(c_dim "→ sudo apt-get install $apt_pkg")"
        sudo apt-get install -y -qq "$apt_pkg" >/dev/null 2>&1 && ok "$name installed via apt" \
            || warn "$name install failed — falls back gracefully"
    else
        warn "$name not found, no package manager available — falls back gracefully"
    fi
}

install_analyzer "oxlint"        "oxlint"        "oxlint"        ""
install_analyzer "cppcheck"      "cppcheck"      "cppcheck"      "cppcheck"
install_analyzer "golangci-lint" "golangci-lint" "golangci-lint" "golangci-lint"
install_analyzer "shellcheck"    "shellcheck"    "shellcheck"    "shellcheck"
install_analyzer "luacheck"      "luacheck"      "luacheck"      "lua-check"
# sqlfluff is a pip package — installed above with the [languages] extras? No.
# It's not in pyproject extras, so install it now into revio's venv:
if ! "$VPY" -m sqlfluff --version >/dev/null 2>&1; then
    echo "  $(c_dim "→ pip install sqlfluff (into revio venv)")"
    "$VPY" -m pip install --quiet sqlfluff >/dev/null 2>&1 && ok "sqlfluff installed" \
        || warn "sqlfluff install failed — falls back gracefully"
else
    ok "sqlfluff already in revio venv"
fi
# Ruby (rubocop) needs `gem`, PHP (phpstan) needs `composer`, Kotlin (detekt)
# needs a JDK — too varied to standardize. Documented in the README install table.
# clippy ships with rustup; spotbugs needs a JDK — leave to user

# --- PATH hint + finale ------------------------------------------------------

echo
echo "$(c_green '✓ revio installed')"
echo
echo "  Location:    $INSTALL_DIR"
echo "  Launcher:    $BIN_DIR/revio"
echo

# Ensure BIN_DIR is on PATH; if not, hint at fix
case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;  # already there
    *)
        warn "$BIN_DIR is not on your PATH."
        case "${SHELL:-}" in
            */zsh)  rcfile="$HOME/.zshrc" ;;
            */bash) rcfile="$HOME/.bashrc" ;;
            *)      rcfile="your shell config" ;;
        esac
        echo "  Add this line to $rcfile, then restart your shell:"
        echo "    $(c_cyan "export PATH=\"\$HOME/.local/bin:\$PATH\"")"
        echo
        ;;
esac

echo "  Next step:   run $(c_cyan 'revio') to start the setup wizard."
echo
