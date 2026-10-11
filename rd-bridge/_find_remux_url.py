#!/usr/bin/env python3
"""Find an UNCACHED account file that NEEDS the eng remux (first audio track is
not eng/und but an eng track exists). Zero writes to live caches.

Usage: RD_SKIP_CACHE_PURGE=1 python3 _find_remux_url.py
Prints  <direct-url>\t<size MB>\t<tracks>
"""
import os
import sys

os.environ["RD_SKIP_CACHE_PURGE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import server as S  # noqa: E402

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_cache")
cached_ids = set()
for fn in os.listdir(CACHE_DIR):
    if fn.endswith(".mp4"):
        cached_ids.add(fn.split("_")[0])

ts = S.rd_get_torrents()
print(f"# account torrents: {len(ts)}", file=sys.stderr)
found = []
for t in ts:
    if t.get("status") != "downloaded" or not t.get("links"):
        continue
    tid = t.get("id")
    try:
        info = S.rd_get_cached(tid)
    except Exception as e:
        print(f"# info {tid} failed: {e}", file=sys.stderr)
        continue
    files = info.get("files") or []
    links = info.get("links") or []
    for f in files:
        path = f.get("path") or ""
        low = path.lower()
        if not low.endswith((".mkv", ".mp4")):
            continue
        if f.get("bytes", 0) < 200 * 1024 * 1024:  # skip samples
            continue
        idx = f["id"] - 1
        if not (0 <= idx < len(links)):
            continue
        try:
            ur = S.rd_post("/unrestrict/link", {"link": links[idx]})
        except Exception as e:
            print(f"# unrestrict failed: {e}", file=sys.stderr)
            continue
        dl = ur.get("download")
        if not dl or not S._is_rd_url(dl):
            continue
        fid = dl.split("/d/")[1].split("/")[0]
        if fid in cached_ids:
            print(f"# skip cached {fid} {path[:60]}", file=sys.stderr)
            continue
        try:
            tracks = S.audio_tracks(dl)
        except Exception as e:
            print(f"# probe failed {fid}: {e}", file=sys.stderr)
            continue
        plan = S.audio_plan(tracks, ("eng",))
        langs = [_norm for _norm in (S._norm_lang(x.get("lang")) for x in (tracks or []))]
        print(f"# {plan} {langs} {fid} {path[:70]}", file=sys.stderr)
        if plan == "remux":
            found.append((dl, f.get("bytes", 0), langs))
            print(f"\nFOUND\t{fid}\t{langs}\t{f.get('bytes',0)/1e6:.0f}MB")
            print(dl)
            sys.exit(0)
print("# no remux candidate found", file=sys.stderr)
sys.exit(2)
