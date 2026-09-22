#!/usr/bin/env bash
# Keeps REproxy's RD_BRIDGE_URL fallback pointing at the live quick tunnel.
#
# Why this exists: cloudflared quick tunnels get a NEW random hostname every
# restart. When that happened, the bridge stayed up but REproxy kept calling the
# dead hostname, so Real-Debrid resolution failed and the player silently fell
# back to CineSrc for everything. This script re-syncs + pushes automatically.
set -uo pipefail

REPO=/home/xason/repeaks/hanime-proxy
FILE="$REPO/api/index.py"
LOG=/var/log/rd-tunnel.log
STATE="$REPO/rd-bridge/.last_synced_url"

URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1)
[ -z "${URL:-}" ] && exit 0

CUR=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$FILE" 2>/dev/null | head -1)
[ "$URL" = "$CUR" ] && exit 0

HOST=${URL#https://}

# Health-check via DNS-over-HTTPS + --resolve so a stale local resolver (or a
# just-created tunnel whose DNS hasn't propagated) can't cause a false negative.
alive() {
  for _ in 1 2 3 4 5 6; do
    IP=$(curl -s -m 10 "https://1.1.1.1/dns-query?name=${HOST}&type=A" \
         -H "accept: application/dns-json" 2>/dev/null \
         | grep -oE '"data":"[0-9.]+"' | head -1 | cut -d'"' -f4)
    if [ -n "${IP:-}" ]; then
      if curl -s -m 15 --resolve "${HOST}:443:${IP}" "${URL}/health" 2>/dev/null \
           | grep -q '"status": "ok"'; then
        return 0
      fi
    fi
    sleep 10
  done
  return 1
}

# Never publish a hostname that isn't actually serving the bridge.
alive || exit 0

cd "$REPO" || exit 0
sed -i "s|${CUR}|${URL}|g" api/index.py
git add api/index.py
git -c user.email=woofer@repeaks.xyz -c user.name=woofer \
    commit -m "auto: RD bridge tunnel URL -> ${URL}" >/dev/null 2>&1
git push origin main >/dev/null 2>&1
echo "$URL" > "$STATE"
logger -t rd-tunnel-sync "RD bridge URL synced: ${CUR} -> ${URL}"
