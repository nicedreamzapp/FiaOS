#!/bin/bash
# FiaOS reverse tunnel: this M5 -> VPS 127.0.0.1:9010 -> nginx fia_m5 upstream.
#
# Why this is a script and not just `ssh -R` in the plist (2026-09-07):
# When the laptop sleeps or changes networks the SSH connection dies without a
# FIN. The VPS sshd used to keep the dead session's :9010 listener open forever,
# which produced two symptoms at once:
#   - nginx CONNECTED to :9010 fine and then hung on read, so the M5 tab took
#     ~60s per request and finally fell back to the mini
#   - this tunnel could never rebind: ExitOnForwardFailure=yes made it exit 255
#     instantly with "remote port forwarding failed for listen port 9010",
#     and launchd relooped that forever (8+ hours, the day this was found)
# The VPS now has ClientAliveInterval 30 so a corpse is reaped in ~90s. This
# script closes the remaining gap: it frees the port itself, so a reconnect is
# immediate instead of waiting on that timer.
set -u

REMOTE="${FIA_VPS:-root@your.vps.here}"
RPORT="${FIA_REMOTE_PORT:-9010}"   # this machine's slot on the VPS
LPORT="${FIA_LOCAL_PORT:-9000}"    # local FiaOS
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

# Is something on the far end already SERVING our port? If it answers, a live
# tunnel exists and we must not touch it -- just let this instance exit so
# launchd does not stack two tunnels on one port.
if ssh "${SSH_OPTS[@]}" "$REMOTE" \
     "curl -sf -o /dev/null --max-time 5 http://127.0.0.1:$RPORT/login" 2>/dev/null; then
  echo "$(date '+%F %T') :$RPORT already serving, another tunnel is live - exiting" >&2
  sleep 30   # keep launchd from hot-looping
  exit 0
fi

# Port is either free or held by a corpse that accepts and never answers.
# Free it. Nothing else on the VPS binds 9010, so this only ever kills a corpse.
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "fuser -k -n tcp $RPORT >/dev/null 2>&1; exit 0" 2>/dev/null
echo "$(date '+%F %T') cleared stale :$RPORT, connecting" >&2

# ServerAliveInterval makes THIS side notice a dead VPS; ExitOnForwardFailure
# makes a failed bind exit loudly so launchd retries instead of running a
# tunnel that forwards nothing.
exec /usr/bin/ssh -N -R "$RPORT:localhost:$LPORT" "$REMOTE" \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=2 \
  -o StrictHostKeyChecking=accept-new \
  -o ExitOnForwardFailure=yes
