#!/bin/zsh
# SSH reverse tunnel: exposes the local FiaOS server (port 9000) on a VPS,
# so you can reach it as https://fia.your-domain.com.
#
# Set VPS_IP and DOMAIN below or via env vars.

VPS_IP="${VPS_IP:-YOUR-VPS-IP}"
DOMAIN="${DOMAIN:-fia.your-domain.com}"
LOCAL_PORT="${LOCAL_PORT:-9000}"
REMOTE_PORT="${REMOTE_PORT:-9000}"

if [[ "$VPS_IP" == "YOUR-VPS-IP" ]]; then
    echo "Set VPS_IP and DOMAIN env vars (or edit this script) before running."
    exit 1
fi

echo "Tunneling FiaOS to $VPS_IP..."
echo "Access at: https://$DOMAIN"
echo ""

while true; do
    ssh -N -R "$REMOTE_PORT:localhost:$LOCAL_PORT" "root@$VPS_IP" \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=no \
        -o ExitOnForwardFailure=yes
    echo "Tunnel disconnected. Reconnecting in 5 seconds..."
    sleep 5
done
