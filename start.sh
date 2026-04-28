#!/bin/zsh
# FiaOS — Start all services for development.
# Usage: ./start.sh [--watchdog]
#
# Required env vars:
#   FIAOS_PASSWORD   — long random string; server refuses to start without it
#   VPS_IP           — optional, enables the reverse tunnel
#   DOMAIN           — optional, only used to print the right URL at the end
#   FIA_PROMPT       — optional, persona system prompt for the voice agent

set -e

if [[ -z "$FIAOS_PASSWORD" ]]; then
    echo "FIAOS_PASSWORD env var is not set. Generate one with:"
    echo '  python3 -c "import secrets; print(secrets.token_urlsafe(24))"'
    echo 'and re-run as: FIAOS_PASSWORD="..." ./start.sh'
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VPS_IP="${VPS_IP:-}"
DOMAIN="${DOMAIN:-localhost:9000}"
WATCHDOG=false
[[ "$1" == "--watchdog" ]] && WATCHDOG=true

echo "========================================="
echo "  FiaOS — Remote Mac Control Center"
echo "========================================="

echo "[1/3] Cleaning up old processes..."
pkill -if "personaplex_mlx.local_web" 2>/dev/null || true
lsof -ti :9000 | xargs kill -9 2>/dev/null || true
sleep 1

echo "       PersonaPlex: on-demand (starts when voice tab is opened)"

echo "[2/3] Starting FiaOS server (port 9000)..."
if $WATCHDOG; then
    (while true; do
        .venv/bin/python3 -u server.py >> /tmp/fiaos.log 2>&1
        echo "[watchdog] FiaOS crashed, restarting in 3s..." >> /tmp/fiaos.log
        sleep 3
    done) &
    echo "       FiaOS: UP (watchdog enabled)"
else
    nohup .venv/bin/python3 -u server.py > /tmp/fiaos.log 2>&1 &
    echo "       FiaOS: UP"
fi
sleep 2

echo "[3/3] SSH reverse tunnel..."
if [[ -z "$VPS_IP" ]]; then
    echo "       VPS_IP not set — skipping tunnel (local-only mode)"
elif ps aux | grep "ssh.*-R 9000:localhost:9000.*$VPS_IP" | grep -v grep >/dev/null 2>&1; then
    echo "       Tunnel: already running"
else
    ssh "root@$VPS_IP" "fuser -k 9000/tcp" 2>/dev/null || true
    sleep 1
    ssh -f -N -R 9000:localhost:9000 "root@$VPS_IP" \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=no \
        -o ExitOnForwardFailure=yes
    echo "       Tunnel: UP"
fi

# Prevent system + display sleep so screen capture stays alive
pkill -f "caffeinate" 2>/dev/null || true
caffeinate -dims &
echo "       Sleep prevention: ON (system + display)"

echo ""
echo "========================================="
echo "  All services running."
echo "  Local:  http://localhost:9000"
[[ -n "$VPS_IP" ]] && echo "  Remote: https://$DOMAIN"
echo "========================================="
