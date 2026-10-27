#!/usr/bin/env python3
"""Diagnose the RD bridge's candidate sources for a few representative titles.

Reads the bridge .env, replicates tmdb_to_imdb + the two source calls, and
prints counts + timings. Read-only: no RD writes, no server restart.
"""
import json
import os
import re
import time
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"
SRV = "/home/xason/repeaks/hanime-proxy/rd-bridge/server.py"

env = {}
for line in open(ENV):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

src = open(SRV, encoding="utf-8").read()
m = re.search(r"TMDB_KEY = os.environ.get\(\"TMDB_API_KEY\", \"([a-f0-9]+)\"\)", src)
TMDB_KEY = m.group(1) if m else (env.get("TMDB_API_KEY") or os.environ.get("TMDB_API_KEY"))
print("tmdb_key_present:", bool(TMDB_KEY))

MF = (env.get("MEDIAFUSION_CONFIG_URL") or "").split("?")[0].rstrip("/")
if MF.endswith("/manifest.json"):
    MF = MF[: -len("/manifest.json")]
MF = MF.rstrip("/")


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body, int((time.time() - t0) * 1000)


def imdb_for(tmdb, mtype):
    if not TMDB_KEY:
        return None
    try:
        body, ms = get(f"https://api.themoviedb.org/3/{mtype}/{tmdb}/external_ids?api_key={TMDB_KEY}", 15)
        return json.loads(body).get("imdb_id"), ms
    except Exception as e:
        return f"ERR {e}"[:40], -1


CASES = [
    ("movie", 1010581, 1, 1),
    ("tv", 209867, 1, 1),
    ("tv", 1399, 1, 1),
]

for mtype, tmdb, s, e in CASES:
    imdb, ms = imdb_for(tmdb, mtype)
    print(f"\n=== {mtype} {tmdb} -> imdb {imdb} ({ms}ms)")
    if not imdb or not str(imdb).startswith("tt"):
        print("  no imdb id -> MediaFusion gets tmdb: form and answers 0")
        continue
    kind = "movie" if mtype == "movie" else "series"
    meta = imdb if mtype == "movie" else f"{imdb}:{s}:{e}"
    try:
        body, ms = get(f"{MF}/stream/{kind}/{meta}.json", 25)
        streams = json.loads(body).get("streams") or []
        print(f"  mediafusion: {len(streams)} streams in {ms}ms")
        for st in streams[:4]:
            bh = st.get("behaviorHints") or {}
            name = (bh.get("filename") or st.get("title") or st.get("description") or "")[:80]
            print(f"    - {name}")
    except Exception as ex:
        print("  mediafusion ERR", str(ex)[:80])

    # Torrentio for comparison
    try:
        body, ms = get(f"https://torrentio.strem.fun/qualityfilter=cam,scr/stream/{kind}/{meta}.json", 25)
        streams = json.loads(body).get("streams") or []
        print(f"  torrentio:   {len(streams)} streams in {ms}ms")
    except Exception as ex:
        print("  torrentio ERR", str(ex)[:80])
