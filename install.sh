#!/bin/bash
# FiaOS installer for macOS.
#   curl -fsSL https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/install.sh | bash
# or, from a checkout:  bash install.sh
#
# Environment (all optional):
#   FIAOS_DIR            where to clone (default ~/FiaOS; ignored when run from a checkout)
#   FIAOS_REPO           git URL or local path to clone from
#   FIAOS_PORT           port the server listens on (default 9000)
#   FIAOS_LABEL          LaunchAgent label (default com.nicedreamzapp.fiaos)
#   FIAOS_ENV_FILE       env file holding FIAOS_PASSWORD (default ~/.config/fiaos/fiaos.env)
#   FIAOS_PASSWORD_FILE  read the password from this file instead of prompting
#   FIAOS_RESET_PASSWORD set to 1 to replace the password in an existing env file
#   FIAOS_PYTHON         Python 3.12+ interpreter to build the venv with
#   KIT_NONINTERACTIVE   set to 1 to never prompt
set -euo pipefail

REPO_URL="${FIAOS_REPO:-https://github.com/nicedreamzapp/FiaOS}"
DIR="${FIAOS_DIR:-$HOME/FiaOS}"
PORT="${FIAOS_PORT:-9000}"
LABEL="${FIAOS_LABEL:-com.nicedreamzapp.fiaos}"
ENV_FILE="${FIAOS_ENV_FILE:-$HOME/.config/fiaos/fiaos.env}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/fiaos.log"
DEPS=(aiohttp psutil numpy anthropic pillow pyobjc-framework-Quartz)

say() { printf '[fiaos] %s\n' "$*"; }
die() { printf '[fiaos] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS. Windows: see windows/ in the repo."
[[ "$PORT" =~ ^[0-9]+$ ]] || die "FIAOS_PORT must be a number"
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "FIAOS_LABEL may only hold letters, digits, dot, dash, underscore"

tty_ok() {
    [ "${KIT_NONINTERACTIVE:-0}" != "1" ] && { : </dev/tty; } 2>/dev/null
}

# --- 1. Checkout ---------------------------------------------------------
is_fiaos_checkout() {
    [ -f "$1/server.py" ] && [ -f "$1/screencast.py" ] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

src="${BASH_SOURCE[0]:-}"
here=""
if [ -n "$src" ] && [ -f "$src" ]; then
    here="$(cd "$(dirname "$src")" && pwd)"
fi
if [ -n "$here" ] && is_fiaos_checkout "$here"; then
    DIR="$here"
    say "using this checkout: $DIR"
elif [ -d "$DIR/.git" ]; then
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
        say "local edits in $DIR, skipping git pull"
    else
        say "updating $DIR"
        git -C "$DIR" pull --ff-only --quiet || say "git pull failed, continuing with the current copy"
    fi
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "$DIR exists and is not a git checkout. Move it or set FIAOS_DIR."
else
    command -v git >/dev/null || die "git not found. Install the Xcode command line tools: xcode-select --install"
    say "cloning $REPO_URL into $DIR"
    git clone --quiet "$REPO_URL" "$DIR"
fi
is_fiaos_checkout "$DIR" || die "$DIR does not look like a FiaOS checkout"

# --- 2. Python 3.12+ and the venv ----------------------------------------
py_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}
PY=""
if [ -n "${FIAOS_PYTHON:-}" ]; then
    py_ok "$FIAOS_PYTHON" || die "FIAOS_PYTHON=$FIAOS_PYTHON is not Python 3.12+"
    PY="$FIAOS_PYTHON"
else
    for cand in python3.12 python3.13 python3.14 python3 \
                /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 \
                /usr/local/bin/python3.12 /usr/local/bin/python3.13 /usr/local/bin/python3; do
        p="$(command -v "$cand" 2>/dev/null || true)"
        if [ -n "$p" ] && py_ok "$p"; then PY="$p"; break; fi
    done
fi
[ -n "$PY" ] || die "Python 3.12+ not found. Install it with 'brew install python@3.12' or from https://www.python.org/downloads/macos/ then re-run."

VENV="$DIR/.venv"
if [ -x "$VENV/bin/python3" ] && py_ok "$VENV/bin/python3"; then
    say "reusing $VENV"
else
    rm -rf "$VENV"
    say "creating $VENV with $PY"
    "$PY" -m venv "$VENV"
fi
say "installing ${DEPS[*]} (pyobjc can take a few minutes)"
"$VENV/bin/python3" -m pip install --quiet --disable-pip-version-check "${DEPS[@]}"

# --- 3. Password / env file ----------------------------------------------
gen_password() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(24))'; }

have_pw=0
if [ -f "$ENV_FILE" ] && grep -q '^FIAOS_PASSWORD=.' "$ENV_FILE"; then
    have_pw=1
fi

if [ "$have_pw" = 1 ] && [ "${FIAOS_RESET_PASSWORD:-0}" != "1" ]; then
    say "keeping the password in $ENV_FILE (FIAOS_RESET_PASSWORD=1 to change it)"
    chmod 600 "$ENV_FILE"
else
    pw=""
    pw_source=""
    if [ -n "${FIAOS_PASSWORD_FILE:-}" ]; then
        [ -r "$FIAOS_PASSWORD_FILE" ] || die "FIAOS_PASSWORD_FILE=$FIAOS_PASSWORD_FILE is not readable"
        IFS= read -r pw < "$FIAOS_PASSWORD_FILE" || true
        pw="${pw%$'\r'}"
        [ -n "$pw" ] || die "FIAOS_PASSWORD_FILE is empty"
        pw_source="from FIAOS_PASSWORD_FILE"
    elif tty_ok; then
        while :; do
            printf 'FiaOS password (blank = generate one): ' >/dev/tty
            IFS= read -rs pw </dev/tty || pw=""
            printf '\n' >/dev/tty
            [ -z "$pw" ] && break
            printf 'Again: ' >/dev/tty
            IFS= read -rs pw2 </dev/tty || pw2=""
            printf '\n' >/dev/tty
            [ "$pw" = "$pw2" ] && break
            printf 'They did not match, try again.\n' >/dev/tty
        done
        unset pw2
        pw_source="typed"
    fi
    if [ -z "$pw" ]; then
        pw="$(gen_password)"
        pw_source="generated"
    fi
    case "$pw" in *$'\n'*) die "password may not contain a newline" ;; esac

    mkdir -p "$(dirname "$ENV_FILE")"
    old_umask="$(umask)"
    umask 077
    tmp="$ENV_FILE.tmp.$$"
    {
        if [ -f "$ENV_FILE" ]; then grep -v '^FIAOS_PASSWORD=' "$ENV_FILE" || true; fi
        printf 'FIAOS_PASSWORD=%s\n' "$pw"
    } > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$ENV_FILE"
    umask "$old_umask"
    unset pw
    say "password ($pw_source) saved to $ENV_FILE (mode 600)"
fi

# --- 4. LaunchAgent ------------------------------------------------------
xml() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }

chmod +x "$DIR/fiaos-agent.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
AGENT_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$(xml "$LABEL")</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$(xml "$DIR/fiaos-agent.sh")</string>
    </array>
    <key>WorkingDirectory</key><string>$(xml "$DIR")</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>FIAOS_PORT</key><string>$PORT</string>
        <key>FIAOS_ENV_FILE</key><string>$(xml "$ENV_FILE")</string>
        <key>HOME</key><string>$(xml "$HOME")</string>
        <key>PATH</key><string>$(xml "$AGENT_PATH")</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>$(xml "$LOG")</string>
    <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
# bootout returns before the job is fully gone; bootstrap fails (error 5) until it is.
for _ in $(seq 1 20); do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
done
# Give launchd a moment to release the old process and its port.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
done
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    rm -f "$PLIST"
    die "port $PORT is already in use by another program. Pick another with FIAOS_PORT=<port>."
fi
launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || { sleep 2; launchctl bootstrap "$DOMAIN" "$PLIST"; }

say "waiting for the server on port $PORT"
up=0
for _ in $(seq 1 40); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/login"; then up=1; break; fi
    sleep 1
done

echo
if [ "$up" = 1 ]; then
    say "done. FiaOS is running at http://localhost:$PORT (LaunchAgent $LABEL, log $LOG)"
else
    say "installed, but the server did not answer yet. Check $LOG"
fi
cat <<EOF
[fiaos] The password is in $ENV_FILE
[fiaos] Grant permissions in System Settings > Privacy & Security:
[fiaos]   Screen Recording and Accessibility for the Python that runs FiaOS:
[fiaos]   $("$VENV/bin/python3" -c 'import os,sys; print(os.path.realpath(sys.executable))' 2>/dev/null || echo "$VENV/bin/python3")
[fiaos] Optional extras, not installed: the VPS reverse tunnel (deploy/tunnel_with_stale_port_clear.sh)
[fiaos]   and a caffeinate agent to keep the display awake. See docs/quickstart.md.
[fiaos] Uninstall: curl -fsSL https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/uninstall.sh | bash
[fiaos]   (add  -s -- --purge  to also delete the checkout and the env file)
EOF
[ "$up" = 1 ]
