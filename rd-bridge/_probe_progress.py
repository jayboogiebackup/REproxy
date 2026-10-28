#!/usr/bin/env python3
"""Verify the torrent-screen progress feed end to end.

Fires a resolve with a rid, polls /api/rd/progress exactly like the player will,
and prints the record over time. Proves the subprocess writes are visible to the
parent endpoint (the file-backed design) and that each candidate's verdict lands.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"


def token():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("BRIDGE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")


TOK = token()
RID = f"bench{int(time.time())}"
q = {"tmdb": "209867", "type": "tv", "season": "1", "episode": "1",
     "nocache": "1", "t": TOK, "rid": RID}
STREAM = "http://127.0.0.1:8801/api/rd/stream?" + urllib.parse.urlencode(q)
PROG = "http://127.0.0.1:8801/api/rd/progress?t=" + TOK + "&rid=" + RID

result = {}


def run():
    with urllib.request.urlopen(STREAM, timeout=300) as r:
        result.update(json.loads(r.read()))


th = threading.Thread(target=run, daemon=True)
th.start()

seen = []
t0 = time.time()
while th.is_alive() or time.time() - t0 < 3:
    try:
        with urllib.request.urlopen(PROG, timeout=10) as r:
            d = json.loads(r.read())
    except Exception as e:
        print("poll err", str(e)[:60])
        time.sleep(0.5)
        continue
    phase = d.get("phase") or "-"
    trying = d.get("trying") or []
    tried = d.get("tried") or []
    snap = (phase, len(tried), tuple(trying))
    if snap not in seen:
        seen.append(snap)
        print(f"[{time.time()-t0:5.1f}s] phase={phase:14s} tried={len(tried):2d} "
              f"trying={[t[:38] for t in trying]}")
    if d.get("done"):
        break
    time.sleep(0.4)

th.join(timeout=5)
print("\nresolve:", (result.get("name") or result.get("error") or "")[:70],
      "| audio:", result.get("audio"), "| ms:", int((time.time() - t0) * 1000))

with urllib.request.urlopen(PROG, timeout=10) as r:
    d = json.loads(r.read())
print("final phase:", d.get("phase"), "| done:", d.get("done"), "| source:", d.get("source"))
for row in (d.get("tried") or [])[:8]:
    print(f"  - {row['verdict']:10s} {row['name'][:64]}")
