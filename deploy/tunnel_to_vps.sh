#!/bin/zsh
# SSH tunnel: exposes PersonaPlex (port 8998) to VPS
# This lets you access Fia voice from anywhere via https://fia.example.com

VPS_IP="YOUR_VPS_IP"
LOCAL_PORT=8998
REMOTE_PORT=8998

echo "Tunneling PersonaPlex to VPS ($VPS_IP)..."
echo "Access at: https://fia.example.com"
echo ""

while true; do
    ssh -N -R $REMOTE_PORT:localhost:$LOCAL_PORT root@$VPS_IP \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=no \
        -o ExitOnForwardFailure=yes
    echo "Tunnel disconnected. Reconnecting in 5 seconds..."
    sleep 5
done
