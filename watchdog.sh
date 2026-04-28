#!/bin/zsh
# FiaOS Watchdog — keeps PersonaPlex, the FiaOS server, and the SSH tunnel alive.
# Checks every 30 seconds; restarts anything that has died.
#
# Required env vars (set them in your LaunchAgent plist or shell profile):
#   FIAOS_PASSWORD   — long random string; the server refuses to start without it
#   VPS_IP           — VPS for the reverse tunnel (or unset to skip the tunnel)
#   FIA_PROMPT       — optional: persona system prompt for the voice agent

if [[ -z "$FIAOS_PASSWORD" ]]; then
    echo "[Watchdog] FIAOS_PASSWORD not set. Aborting."
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

FIA_PROMPT="${FIA_PROMPT:-You are a helpful local voice assistant. Keep replies short.}"

start_personaplex() {
    echo "[Watchdog] Starting PersonaPlex..."
    nohup .venv/bin/python3 -u -m personaplex_mlx.local_web -q 4 --no-browser \
        --voice NATF0 \
        --text-prompt "$FIA_PROMPT" \
        --text-temp 0.1 \
        --audio-temp 0.5 > /tmp/personaplex.log 2>&1 &
    echo "[Watchdog] PersonaPlex PID: $!"
}

start_server() {
    echo "[Watchdog] Starting FiaOS server..."
    nohup .venv/bin/python3 -u server.py > /tmp/fiaos.log 2>&1 &
    echo "[Watchdog] Server PID: $!"
}

start_tunnel() {
    if [[ -z "$VPS_IP" ]]; then
        return
    fi
    echo "[Watchdog] Starting SSH tunnel to $VPS_IP..."
    nohup ssh -N -R 9000:localhost:9000 "root@$VPS_IP" \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=no \
        -o ExitOnForwardFailure=yes > /tmp/fiaos-tunnel.log 2>&1 &
    echo "[Watchdog] Tunnel PID: $!"
}

echo "[Watchdog] FiaOS watchdog started at $(date)"

while true; do
    if ! lsof -i:8998 -sTCP:LISTEN > /dev/null 2>&1; then
        echo "[Watchdog] $(date) — PersonaPlex DOWN, restarting..."
        pkill -9 -f personaplex_mlx 2>/dev/null
        sleep 2
        start_personaplex
        sleep 30
    fi

    if ! lsof -i:9000 -sTCP:LISTEN > /dev/null 2>&1; then
        echo "[Watchdog] $(date) — FiaOS server DOWN, restarting..."
        pkill -9 -f "server.py" 2>/dev/null
        sleep 2
        start_server
        sleep 3
    fi

    if [[ -n "$VPS_IP" ]] && ! pgrep -f "ssh.*9000:localhost" > /dev/null 2>&1; then
        echo "[Watchdog] $(date) — SSH tunnel DOWN, restarting..."
        start_tunnel
        sleep 3
    fi

    sleep 30
done
