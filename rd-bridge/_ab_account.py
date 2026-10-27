#!/usr/bin/env python3
"""A/B a single resolve with and without the account-library walk.

Proves WHERE a pick comes from: if skip_account=1 returns a good 1080p while the
default returns a 4K library copy, the account walk is the thing to fix.

Usage: python3 _ab_account.py <type> <tmdb> [season] [episode]
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


mtype, tmdb = sys.argv[1], sys.argv[2]
season = sys.argv[3] if len(sys.argv) > 3 else None
episode = sys.argv[4] if len(sys.argv) > 4 else None


def call(skip):
    q = {"tmdb": tmdb, "type": mtype, "t": token(), "nocache": "1"}
    if season:
        q["season"], q["episode"] = season, episode or "1"
    if skip:
        q["skip_account"] = "1"
    url = "http://127.0.0.1:8801/api/rd/stream?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=200) as r:
        d = json.loads(r.read())
    name = d.get("name") or urllib.parse.unquote(str(d.get("url", "")).split("/")[-1])
    return {"audio": d.get("audio"), "remux": d.get("needs_remux"), "name": name[:90]}


for label, skip in (("account-first (default)", False), ("indexers only", True)):
    try:
        r = call(skip)
        print(f"{label:26s} audio={str(r['audio']):8s} remux={r['remux']}  {r['name']}")
    except Exception as e:
        print(f"{label:26s} ERROR {str(e)[:80]}")
