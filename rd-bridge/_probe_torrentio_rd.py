#!/usr/bin/env python3
"""Compare plain Torrentio vs Torrentio-with-Real-Debrid config.

Stremio's speed comes from Torrentio RD: the addon is configured with the debrid
key, so its /stream/ response already carries RESOLVED Real-Debrid links
(`url`) instead of bare infoHashes our bridge would have to add and poll.

This prints, for both URLs: how many streams, how many carry a direct url, how
many are marked RD+/cached, and a sample name. The key is never printed.
"""
import json
import re
import time
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"


def env(key):
    for line in open(ENV):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


RD = env("REALDEBRID_API_KEY")
print("rd_key_present:", bool(RD), "len:", len(RD))

IDS = [("movie", "tt21909764", "Culpa Mia"), ("movie", "tt0137523", "Fight Club")]

VARIANTS = {
    "plain": "https://torrentio.strem.fun/qualityfilter=cam,scr",
    "torrentio-rd": f"https://torrentio.strem.fun/realdebrid={RD}|qualityfilter=cam,scr",
}

for kind, sid, label in IDS:
    print(f"\n=== {label} ({sid})")
    for vname, base in VARIANTS.items():
        url = f"{base}/stream/{kind}/{sid}.json"
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        except Exception as e:
            print(f"  {vname:14s} ERROR {str(e)[:70]}")
            continue
        ms = int((time.time() - t0) * 1000)
        streams = d.get("streams") or []
        direct = [s for s in streams if str(s.get("url", "")).startswith("http")]
        rd_direct = [s for s in direct if "real-debrid" in s.get("url", "")]
        cached = [s for s in streams if re.search(r"(RD\+|\[RD\]|⚡|cached)", s.get("name", ""), re.I)]
        print(f"  {vname:14s} {len(streams):3d} streams, {len(direct):3d} with url, "
              f"{len(rd_direct):3d} real-debrid url, {len(cached):3d} RD/cached tags, {ms}ms")
        for s in (rd_direct or direct)[:2]:
            print(f"      url host: {s.get('url','').split('/')[2]}  "
                  f"| {s.get('name','')[:28]} | {(s.get('behaviorHints') or {}).get('filename','')[:56]}")
