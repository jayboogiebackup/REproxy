#!/usr/bin/env python3
"""Why is audio 'unknown' on RD links?

Resolves a title, then runs ffprobe against the exact resolved url and prints:
  - whether the url answers at all (status, content-type, range support)
  - the probe's raw stderr (the bridge's audio_tracks() swallows it)
This distinguishes "RD serves a redirect the probe can't follow" from
"the probe is timing out" from "the file genuinely has no audio metadata".
"""
import json
import subprocess
import urllib.parse
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"


def token():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("BRIDGE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")


def resolve(mtype, tmdb, season=None, episode=None):
    q = {"tmdb": tmdb, "type": mtype, "t": token(), "nocache": "1"}
    if season:
        q["season"], q["episode"] = season, episode or "1"
    with urllib.request.urlopen("http://127.0.0.1:8801/api/rd/stream?" + urllib.parse.urlencode(q),
                                timeout=200) as r:
        return json.loads(r.read())


import sys
d = resolve(sys.argv[1] if len(sys.argv) > 1 else "movie", sys.argv[2] if len(sys.argv) > 2 else 550)
url = d.get("url") or ""
print("resolved:", (d.get("name") or "")[:70], "| audio:", d.get("audio"), "| remux:", d.get("needs_remux"))
if not url:
    raise SystemExit("no url: " + json.dumps(d)[:200])

req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"})
try:
    with urllib.request.urlopen(req, timeout=25) as r:
        print("GET range:", r.status, "| ct:", r.headers.get("content-type"),
              "| len:", r.headers.get("content-length"), "| final:", r.geturl().split("/")[2])
except Exception as e:
    print("GET range ERR:", str(e)[:120])

print("\nffprobe (same call the bridge makes):")
cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
       "stream=index,codec_name:stream_tags=language", "-of", "json",
       "-rw_timeout", "8000000", url]
try:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    print(" rc:", out.returncode)
    print(" stdout:", out.stdout.strip()[:400])
    print(" stderr:", out.stderr.strip()[:400])
except subprocess.TimeoutExpired:
    print(" ffprobe TIMED OUT after 30s")
