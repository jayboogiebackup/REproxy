#!/usr/bin/env python3
"""What does a Torrentio-RD stream url actually resolve to?

Prints the redirect chain (status + host only, never the full url with its key)
for the first cached [RD+] stream of a title. This decides whether the resolver
can hand the url straight to the player or must follow it once first.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"


def env(key):
    for line in open(ENV):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


RD = env("REALDEBRID_API_KEY")
BASE = f"https://torrentio.strem.fun/realdebrid={RD}|qualityfilter=cam,scr"
IMDB = "tt0137523"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)
with urllib.request.urlopen(urllib.request.Request(f"{BASE}/stream/movie/{IMDB}.json",
                                                   headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
    streams = json.loads(r.read()).get("streams") or []

picks = [s for s in streams if "[RD+]" in (s.get("name") or "")][:2]
print("cached [RD+] streams found:", len(picks))
for s in picks:
    u = s.get("url", "")
    print("\n---", (s.get("behaviorHints") or {}).get("filename", "")[:70])
    print("  start host:", u.split("/")[2], "path:", "/".join(u.split("/")[3:6])[:60])
    cur, hops = u, 0
    while hops < 5:
        req = urllib.request.Request(cur, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        try:
            resp = opener.open(req, timeout=20)
            print(f"  hop{hops}: {resp.status} final host={cur.split('/')[2]} "
                  f"ct={resp.headers.get('content-type')} len={resp.headers.get('content-length')}")
            break
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            print(f"  hop{hops}: {e.code} -> host={(loc or '').split('/')[2] if loc else None}")
            if not loc:
                break
            cur = loc if loc.startswith("http") else urllib.parse.urljoin(cur, loc)
            hops += 1
        except Exception as e:
            print(f"  hop{hops}: ERR {str(e)[:70]}")
            break
