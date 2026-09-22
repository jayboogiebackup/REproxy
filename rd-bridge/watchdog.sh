#!/usr/bin/env bash
# rd-watchdog - catches failures systemd Restart=always cannot see:
#  1. bridge process alive but hung (not answering /health)
#  2. tunnel process alive but not serving the public URL
set -uo pipefail
LOG=/var/log/rd-watchdog.log
S=/var/lib/rd-watchdog
mkdir -p "$S"
log(){ echo "$(date -Is) $*" >> "$LOG"; }

# ---- 1. local bridge health (2 tries) ----
ok=0
for i in 1 2; do
  if curl -s -m 8 http://127.0.0.1:8801/health | grep -q '"status": "ok"'; then ok=1; break; fi
  sleep 5
done
now=$(date +%s)
last=0; [ -f "$S/bridge" ] && last=$(cat "$S/bridge")
if [ "$ok" != 1 ] && [ $((now-last)) -gt 120 ]; then
  log "bridge unhealthy -> restart"
  echo "$now" > "$S/bridge"
  systemctl restart rd-bridge.service
fi

# ---- 2. public tunnel health (newest URLs in the tunnel log) ----
urls=$(grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' /var/log/rd-tunnel.log 2>/dev/null | awk '!seen[$0]++' | tail -2)
tok=0
for u in $urls; do
  h=${u#https://}
  ip=$(curl -s -m 10 "https://1.1.1.1/dns-query?name=${h}&type=A" -H 'accept: application/dns-json' | grep -oE '"data":"[0-9.]+"' | head -1 | cut -d'"' -f4)
  [ -z "${ip:-}" ] && continue
  if curl -s -m 12 --resolve "${h}:443:${ip}" "${u}/health" | grep -q '"status": "ok"'; then tok=1; break; fi
done
last=0; [ -f "$S/tunnel" ] && last=$(cat "$S/tunnel")
if [ "$tok" != 1 ] && [ $((now-last)) -gt 420 ]; then
  log "public tunnel unhealthy -> restart"
  echo "$now" > "$S/tunnel"
  systemctl restart rd-tunnel.service
fi
