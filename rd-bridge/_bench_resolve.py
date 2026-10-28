#!/usr/bin/env python3
"""REPEAKS RD resolver benchmark — the gate for every playback change.

Usage:
    python3 _bench_resolve.py            # cold + warm pass over the fixed set
    python3 _bench_resolve.py --warm     # warm only (cache already hot)

For each title it records:
  * resolve time (ms) through the bridge, exactly as the player asks for it
  * the release that was picked (name, quality, codec)
  * the audio verdict: fits / remux / unknown  (+ cam flag)
  * whether the URL is a playable RD direct link

WHY THIS EXISTS: the resolver is tuned by measured rules (quality-first, audio
playability bounds, plausibility gates). Changing any of them without a fixed
baseline is how a "speed fix" quietly starts serving Japanese audio or 4K
remuxes. Every future change must be compared against this table.

Read-only: no RD writes beyond what a normal resolve does, no server restart.
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

ENV = "/home/xason/repeaks/hanime-proxy/rd-bridge/.env"
BASE = "http://127.0.0.1:8801"
TIMEOUT = 200

# A fixed, representative set: mainstream movie, big TV, anime (sub), anime
# (dub), a 4K-heavy show, and a title RD often lacks (fallback path).
CASES = [
    ("movie", 1010581, None, None, "movie · Culpa Mia"),
    ("tv", 1399, 1, 1, "tv · Game of Thrones S1E1"),
    ("tv", 209867, 1, 1, "tv · Frieren S1E1 (anime sub)"),
    ("anime", 1429, 1, 1, "anime · Attack on Titan S1E1"),
    ("tv", 94997, 1, 1, "tv · House of the Dragon S1E1"),
    ("movie", 550, None, None, "movie · Fight Club"),
]


def token():
    for line in open(ENV):
        line = line.strip()
        if line.startswith("BRIDGE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("BRIDGE_TOKEN not found in .env")


def resolve(tok, mtype, tmdb, season, episode, extra=""):
    q = {"tmdb": tmdb, "type": mtype, "t": tok}
    if season:
        q["season"] = season
        q["episode"] = episode
    url = f"{BASE}/api/rd/stream?{urllib.parse.urlencode(q)}{extra}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            d = json.loads(r.read())
    except Exception as e:
        return {"ms": int((time.time() - t0) * 1000), "error": str(e)[:100]}
    ms = int((time.time() - t0) * 1000)
    name = d.get("name") or d.get("filename") or ""
    if not name and d.get("url"):
        name = urllib.parse.unquote(d["url"].split("/")[-1])[:90]
    return {
        "ms": ms,
        "name": name[:88],
        "audio": d.get("audio"),
        "cam": bool(d.get("cam")),
        "remux": bool(d.get("needs_remux")),
        "playable": bool(d.get("url") and "real-debrid" in str(d.get("url"))),
        "error": None if d.get("url") else (d.get("error") or "no url"),
    }


def flag(name, audio, remux, cam):
    """One-line verdict for the things the owner actually complained about."""
    bad = []
    if cam:
        bad.append("CAM")
    if remux:
        bad.append("REMUX(slow)")
    if not audio or audio == "unknown":
        bad.append("AUDIO?")
    # foreign-language sniff on the release name
    if re.search(r"\b(ITA|FRE|GER|SPA|HIN|TAM|TEL|RUS|JPN|CHS|CHT)\b", name or "", re.I):
        bad.append("lang-tag")
    return "OK" if not bad else "+".join(bad)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", action="store_true", help="warm pass only")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--nocache", action="store_true",
                    help="force a fresh resolve (bridge ?nocache=1) — the only "
                         "honest way to see a ranking change")
    args = ap.parse_args()

    tok = token()
    extra = "&nocache=1" if args.nocache else ""
    passes = [("fresh", extra)] if args.nocache else ([("warm", "")] if args.warm else [("cold", ""), ("warm", "")])

    rows = []
    for label, extra in passes:
        print(f"\n──── {label.upper()} PASS ────")
        print(f"{'case':38s} {'ms':>7s}  {'audio':8s} {'flags':22s} release")
        for mtype, tmdb, s, e, desc in CASES:
            r = resolve(tok, mtype, tmdb, s, e, extra)
            f = flag(r.get("name", ""), r.get("audio"), r.get("remux"), r.get("cam"))
            print(f"{desc:38s} {r['ms']:7d}  {str(r.get('audio')):8s} {f:22s} {r.get('name','')[:52]}")
            if r.get("error"):
                print(f"{'':38s} {'':>7s}  ERROR: {r['error']}")
            rows.append({"pass": label, "case": desc, **r, "flags": f})

    # summary
    print("\n──── SUMMARY ────")
    for label, _ in passes:
        sel = [r for r in rows if r["pass"] == label]
        times = sorted(r["ms"] for r in sel)
        p50 = statistics.median(times)
        bad = [r for r in sel if r["flags"] != "OK"]
        print(f"{label:5s} n={len(sel)}  p50={p50:.0f}ms  max={max(times)}ms  "
              f"not-clean={len(bad)}/{len(sel)}")
        for r in bad:
            # error rows have no 'name' (resolve() returns {ms,error} on failure)
            label = (r.get("name") or r.get("error") or "")[:44]
            print(f"   ! {r['case']:36s} {r['flags']:22s} {label}")
    if args.json:
        print(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
