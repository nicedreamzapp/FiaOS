#!/bin/bash
# FiaOS uninstaller for macOS.
#   curl -fsSL https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/uninstall.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/uninstall.sh | bash -s -- --purge
# or:  bash ~/FiaOS/uninstall.sh [--purge]
#
# Stops and removes the LaunchAgent and the .venv. Keeps the checkout, the env
# file (your password) and the log unless --purge is given.
# Uses the same FIAOS_DIR / FIAOS_LABEL / FIAOS_ENV_FILE as install.sh.
set -euo pipefail

PURGE=0
for a in "$@"; do
    case "$a" in
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null; exit 0 ;;
        *) printf '[fiaos] unknown option: %s\n' "$a" >&2; exit 2 ;;
    esac
done

DIR="${FIAOS_DIR:-$HOME/FiaOS}"
LABEL="${FIAOS_LABEL:-com.nicedreamzapp.fiaos}"
ENV_FILE="${FIAOS_ENV_FILE:-$HOME/.config/fiaos/fiaos.env}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/fiaos.log"

say() { printf '[fiaos] %s\n' "$*"; }

[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { say "bad FIAOS_LABEL"; exit 2; }

is_fiaos_checkout() { [ -f "$1/server.py" ] && [ -f "$1/screencast.py" ]; }

src="${BASH_SOURCE[0]:-}"
if [ -n "$src" ] && [ -f "$src" ]; then
    here="$(cd "$(dirname "$src")" && pwd)"
    if is_fiaos_checkout "$here"; then DIR="$here"; fi
fi

if launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then say "stopped $LABEL"; fi
if [ -f "$PLIST" ]; then rm -f "$PLIST"; say "removed $PLIST"; fi

if is_fiaos_checkout "$DIR"; then
    if [ -d "$DIR/.venv" ]; then rm -rf "$DIR/.venv"; say "removed $DIR/.venv"; fi
fi

if [ "$PURGE" = 1 ]; then
    if [ -f "$ENV_FILE" ]; then
        rm -f "$ENV_FILE"; say "removed $ENV_FILE"
        rmdir "$(dirname "$ENV_FILE")" 2>/dev/null || true
    fi
    if [ -f "$LOG" ]; then rm -f "$LOG"; say "removed $LOG"; fi
    if is_fiaos_checkout "$DIR"; then
        cd /
        rm -rf "$DIR"; say "removed $DIR"
    fi
else
    say "kept $DIR and $ENV_FILE (use --purge to delete them)"
fi
say "done"
