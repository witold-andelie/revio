#!/usr/bin/env bash
# revio uninstaller for macOS and Linux.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/uninstall.sh | bash
#
# What it removes (after confirmation):
#   1. Install dir (the venv + cloned repo)
#   2. Launcher (~/.local/bin/revio) + PATH note
#   3. (optional, asks separately) ~/.cache/revio       fix history + checkpoints
#   4. (optional, asks separately) ~/.config/revio      user config + skills
#   5. (optional, asks separately) ~/.cache/huggingface RAG embedding models
#
# System-wide static analyzers (oxlint, cppcheck, etc.) installed via brew/
# apt are NOT touched - they may be useful to other tools.

set -u

c_cyan()  { printf '\033[36m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_yellow(){ printf '\033[33m%s\033[0m' "$*"; }
c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_dim()   { printf '\033[2m%s\033[0m' "$*"; }

info() { echo "  $*"; }
ok()   { echo "  $(c_green '[OK]')   $*"; }
warn() { echo "  $(c_yellow '[WARN]') $*" >&2; }

ask_yes_no() {
    local prompt="$1" default="${2:-n}" hint="[y/N]" ans
    [ "$default" = "y" ] && hint="[Y/n]"
    while true; do
        printf "  %s %s " "$prompt" "$hint"
        read -r ans </dev/tty || ans=""
        ans="${ans:-$default}"
        case "${ans,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *)     echo "  please answer y or n" >&2 ;;
        esac
    done
}

dir_size_mb() {
    if [ -d "$1" ]; then du -sm "$1" 2>/dev/null | awk '{print $1}'; else echo 0; fi
}

echo
echo "$(c_cyan 'revio uninstaller')"
echo

# --- Discover install location ---------------------------------------------

CANDIDATES=()
[ -n "${REVIO_HOME:-}" ] && CANDIDATES+=("$REVIO_HOME")
CANDIDATES+=("$HOME/.local/share/revio")

INSTALL_DIR=""
for c in "${CANDIDATES[@]}"; do
    if [ -x "$c/.venv/bin/revio" ] || [ -f "$c/install-metadata.json" ]; then
        INSTALL_DIR="$c"; break
    fi
done

if [ -z "$INSTALL_DIR" ]; then
    printf "  Install path not auto-detected. Enter it now (or blank to abort): "
    read -r INSTALL_DIR </dev/tty || INSTALL_DIR=""
    if [ -z "$INSTALL_DIR" ] || [ ! -d "$INSTALL_DIR" ]; then
        warn "nothing to uninstall"; exit 0
    fi
fi
info "Found: $INSTALL_DIR"

# Bin dir from metadata, with sensible default
BIN_DIR="$HOME/.local/bin"
META="$INSTALL_DIR/install-metadata.json"
if [ -f "$META" ]; then
    # tolerant parse: grep the bin_dir field; trust default if missing
    bd=$(grep -o '"bin_dir":[^,}]*' "$META" 2>/dev/null | sed -E 's/.*"bin_dir":\s*"([^"]+)".*/\1/')
    [ -n "$bd" ] && BIN_DIR="$bd"
fi

size_mb=$(dir_size_mb "$INSTALL_DIR")
info "Disk usage: ${size_mb} MB"
info ""

ask_yes_no "Remove install dir AND launcher? (caches stay)" "y" || { info "cancelled"; exit 0; }

# --- 1. Launcher ------------------------------------------------------------

if [ -f "$BIN_DIR/revio" ]; then
    rm -f "$BIN_DIR/revio"
    ok "removed launcher: $BIN_DIR/revio"
fi

# We don't touch shell rc files - we'd have to identify which line we added
# and surgically remove it. Print a note instead.
info "PATH entry (if any) in your shell rc was left in place; safe to remove manually."

# --- 2. Install dir ---------------------------------------------------------

if rm -rf "$INSTALL_DIR" 2>/dev/null; then
    ok "removed install dir: $INSTALL_DIR"
else
    warn "could not fully remove $INSTALL_DIR (permission?)"
fi

# --- 3. Caches (optional) ---------------------------------------------------

CACHE="$HOME/.cache/revio"
CONFIG="$HOME/.config/revio"
HF_CACHE="$HOME/.cache/huggingface"

if [ -d "$CACHE" ]; then
    c_mb=$(dir_size_mb "$CACHE")
    info ""
    info "Cache (fix history, checkpoints, RAG index): $CACHE (${c_mb} MB)"
    if ask_yes_no "Remove cache?" "n"; then
        rm -rf "$CACHE"
        ok "cache removed"
    else
        info "kept (re-installing keeps your fix history + finding database)"
    fi
fi

if [ -d "$CONFIG" ]; then
    info ""
    info "Config (config.toml + skills): $CONFIG"
    if ask_yes_no "Remove config + custom skills?" "n"; then
        rm -rf "$CONFIG"
        ok "config removed"
    else
        info "kept (you can re-install without re-running the wizard)"
    fi
fi

if [ -d "$HF_CACHE" ]; then
    h_mb=$(dir_size_mb "$HF_CACHE")
    info ""
    info "HuggingFace cache (RAG embedding models, may be used by other tools): $HF_CACHE (${h_mb} MB)"
    if ask_yes_no "Remove HuggingFace cache too?" "n"; then
        rm -rf "$HF_CACHE"
        ok "HuggingFace cache removed"
    else
        info "kept (shared with other ML tools)"
    fi
fi

echo
echo "$(c_green '================================================================')"
echo "$(c_green '  revio removed')"
echo "$(c_green '================================================================')"
echo
echo "  Open a new terminal to refresh PATH."
echo "  Re-install:  curl -sSL https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.sh | bash"
echo
