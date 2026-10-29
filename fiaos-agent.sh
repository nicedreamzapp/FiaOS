#!/bin/bash
# FiaOS LaunchAgent wrapper, written for install.sh (can also be run by hand).
# Loads the env file (only plain NAME=value lines, nothing is executed), then
# runs the server from this checkout's .venv. Variables already set in the
# environment (for example FIAOS_PORT from the plist) win over the file.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${FIAOS_ENV_FILE:-$HOME/.config/fiaos/fiaos.env}"

if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]] || continue
        name="${line%%=*}"
        value="${line#*=}"
        if [ -z "${!name+x}" ]; then
            export "$name=$value"
        fi
    done < "$ENV_FILE"
fi

cd "$DIR" || exit 1
exec "$DIR/.venv/bin/python3" -u server.py
