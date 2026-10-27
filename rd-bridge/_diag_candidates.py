#!/usr/bin/env python3
"""Show what the resolver ranks for a title, through the bridge's own
/api/rd/candidates preview (read-only: never adds a torrent).

Usage: python3 _diag_candidates.py <type> <tmdb> [season] [episode] [lang]
"""
import json
import sys
import urllib.parse
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"


def token():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("BRIDGE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")


mtype = sys.argv[1]
tmdb = sys.argv[2]
season = sys.argv[3] if len(sys.argv) > 3 else None
episode = sys.argv[4] if len(sys.argv) > 4 else None
lang = sys.argv[5] if len(sys.argv) > 5 else None

q = {"tmdb": tmdb, "type": mtype, "t": token()}
if season:
    q["season"] = season
    q["episode"] = episode or 1
if lang:
    q["lang"] = lang
url = "http://127.0.0.1:8801/api/rd/candidates?" + urllib.parse.urlencode(q)
with urllib.request.urlopen(url, timeout=120) as r:
    d = json.loads(r.read())

print("counts:", d.get("counts"), " total:", d.get("total"), " imdb:", d.get("imdb"))
acct = d.get("account") or []
if acct:
    print("account library:", [str(a)[:60] for a in acct[:3]])
print("\nranked candidates (top 14):")
for i, c in enumerate(d.get("candidates", [])[:14]):
    print(f"{i:2d}. [{c.get('source','')[:10]:10s}] s={c.get('seeders',0):4d} "
          f"{str(c.get('size_gb')):>6s}GB  {c.get('name','')[:74]}")
