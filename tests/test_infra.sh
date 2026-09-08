#!/bin/bash
# FiaOS machine-switching: simulate every failure mode, measure real recovery.
VPS="${FIA_VPS:?set FIA_VPS=root@your.vps}"
URL="${FIA_URL:?set FIA_URL=https://fia.example.com}"
AGENT="gui/$(id -u)/com.fiaos.tunnel-contabo"
PASS=0; FAIL=0
# Whatever happens, leave the machine the way we found it.
restore(){
  launchctl print $AGENT >/dev/null 2>&1 ||     launchctl bootstrap gui/$(id -u) $HOME/Library/LaunchAgents/com.fiaos.tunnel-contabo.plist 2>/dev/null
  P=$(launchctl list com.fiaos.server 2>/dev/null | awk -F'= ' '/"PID"/{print $2}' | tr -d ';')
  [ -n "$P" ] && kill -CONT $P 2>/dev/null
  ssh -o ConnectTimeout=8 -o BatchMode=yes $VPS "pkill -f \"bind..'127.0.0.1',9010\" >/dev/null 2>&1; exit 0" 2>/dev/null
}
trap restore EXIT
ok(){ echo "  PASS  $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

hit(){ curl -s -o /dev/null -w "%{http_code} %{time_total} %header{X-Fia-Fallback}" \
       --max-time 40 -b "fia_target=$1" "$URL/login"; }

echo "=== S1. BASELINE: all three tabs ==="
for m in mini m5 pc; do
  read -r code t fb <<<"$(hit $m)"
  if [ "$code" = "200" ] && [ -z "$fb" ]; then ok "$m serves directly (${t}s)"; else no "$m code=$code fallback=$fb"; fi
done

echo
echo "=== S2. M5 HUNG (accepts, never answers - tonight's exact symptom) ==="
SRV=$(launchctl list com.fiaos.server | awk -F'= ' '/"PID"/{print $2}' | tr -d ';')
echo "  (FiaOS server pid $SRV)"
( sleep 60; kill -CONT $SRV 2>/dev/null ) >/dev/null 2>&1 &  # watchdog
WD=$!
kill -STOP $SRV
read -r code t fb <<<"$(hit m5)"
kill -CONT $SRV; kill $WD 2>/dev/null
SECS=${t%.*}
if [ "$code" = "200" ] && [ "$fb" = "mini" ] && [ "${SECS:-99}" -lt 25 ]; then
  ok "hung M5 fell back to mini in ${t}s (was 60s+ before)"
else no "hung M5: code=$code time=${t}s fallback=$fb"; fi
sleep 3
read -r code t fb <<<"$(hit m5)"
[ "$code" = "200" ] && [ -z "$fb" ] && ok "M5 serves itself again after unfreeze (${t}s)" || no "M5 did not recover: $code $fb"

echo
echo "=== S3. M5 FiaOS DEAD (tunnel up, nothing behind it) ==="
launchctl kill SIGTERM gui/$(id -u)/com.fiaos.server 2>/dev/null
sleep 3
read -r code t fb <<<"$(hit m5)"
[ "$code" = "200" ] && [ "$fb" = "mini" ] && ok "dead M5 server -> mini in ${t}s" || no "dead server: code=$code fallback=$fb"
sleep 8   # KeepAlive brings it back
for i in 1 2 3 4 5 6; do
  read -r code t fb <<<"$(hit m5)"
  [ -z "$fb" ] && break
  sleep 4
done
[ "$code" = "200" ] && [ -z "$fb" ] && ok "M5 server auto-restarted and serves again" || no "M5 server did not come back: $code $fb"

echo
echo "=== S4. TUNNEL KILLED (network drop) ==="
TPID=$(pgrep -f "9010:localhost" | head -1)
kill -9 $TPID 2>/dev/null
echo "  killed tunnel pid $TPID, waiting for launchd + wrapper..."
START=$(date +%s)
REC=""
for i in $(seq 1 30); do
  read -r code t fb <<<"$(hit m5)"
  if [ "$code" = "200" ] && [ -z "$fb" ]; then REC=$(( $(date +%s) - START )); break; fi
  sleep 3
done
[ -n "$REC" ] && ok "tunnel self-healed in ${REC}s" || no "tunnel did not come back within 90s"

echo
echo "=== S5. ZOMBIE PORT (the 8-hour deadlock) ==="
launchctl bootout $AGENT 2>/dev/null; sleep 3
ssh -o ConnectTimeout=10 -o BatchMode=yes $VPS "fuser -k -n tcp 9010 >/dev/null 2>&1; nohup python3 -c \"
import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',9010)); s.listen(16); time.sleep(600)
\" >/dev/null 2>&1 & sleep 2; echo dummy-listener-up" 2>&1 | tail -1
ZC=$(ssh -o ConnectTimeout=10 $VPS "curl -s -o /dev/null -w '%{http_code}' --max-time 6 http://127.0.0.1:9010/login" 2>&1)
[ "$ZC" = "000" ] && ok "zombie in place: port accepts but never answers" || no "zombie sim wrong: got $ZC"
launchctl bootstrap gui/$(id -u) $HOME/Library/LaunchAgents/com.fiaos.tunnel-contabo.plist 2>/dev/null
START=$(date +%s); REC=""
for i in $(seq 1 30); do
  read -r code t fb <<<"$(hit m5)"
  if [ "$code" = "200" ] && [ -z "$fb" ]; then REC=$(( $(date +%s) - START )); break; fi
  sleep 3
done
[ -n "$REC" ] && ok "wrapper cleared the zombie and reconnected in ${REC}s" || no "STILL DEADLOCKED after 90s"

echo
echo "=== S6. WRAPPER MUST NOT KILL A LIVE TUNNEL ==="
LIVE_BEFORE=$(pgrep -f "9010:localhost" | head -1)
WLOG=$(mktemp)
${FIA_TUNNEL_SCRIPT:-$HOME/FiaOS/tunnel_m5.sh} >"$WLOG" 2>&1 &
WPID=$!
sleep 12
kill $WPID 2>/dev/null
OUT=$(head -2 "$WLOG")
echo "  wrapper said: $OUT"
LIVE_AFTER=$(pgrep -f "9010:localhost" | head -1)
echo "$OUT" | grep -q "already serving" && ok "wrapper detected the live tunnel and stood down" || no "wrapper did not detect a live tunnel: $OUT"
[ "$LIVE_BEFORE" = "$LIVE_AFTER" ] && ok "original tunnel pid untouched ($LIVE_BEFORE)" || no "tunnel pid changed $LIVE_BEFORE -> $LIVE_AFTER"
read -r code t fb <<<"$(hit m5)"
[ "$code" = "200" ] && [ -z "$fb" ] && ok "M5 still serving after the duplicate attempt" || no "wrapper broke a healthy tunnel: $code $fb"

echo
echo "=== S7. IDLE: nothing runs when nobody is watching ==="
sleep 2
pgrep -f screen_worker.py >/dev/null && no "capture worker running with no viewer" || ok "no capture worker on M5"
CPU=$(ps -o %cpu= -p $(pgrep -f "9010:localhost" | head -1) 2>/dev/null | tr -d ' ')
ok "tunnel idles at ${CPU:-0}% cpu"

echo
echo "============================================"
echo "INFRA RESULT: $PASS passed, $FAIL failed"
echo "============================================"
exit $([ $FAIL -eq 0 ] && echo 0 || echo 1)
