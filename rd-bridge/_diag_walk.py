#!/usr/bin/env python3
"""Diagnostic #2: capture the EXACT candidate walk order the resolver uses for a
SUB vs a DUB anime request, without spending any RD adds (rd_add_and_stream is
stubbed to log + return None). This is the order the real walk would spend its
15-candidate / 40s / 115s budget on.

We also cross-reference the persisted probe-verdict cache (real ffprobe results
from earlier resolves) so a candidate that PROVABLY carries no accepted audio
("reject") is flagged — those are the ones the dub walk must not lead with.
"""
import os, sys, json
os.environ["RD_SKIP_CACHE_PURGE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S

RESULT = {}
def fake_add(info_hash, file_idx=None, filename=None, season=None, episode=None):
    RESULT.setdefault("walk", []).append((info_hash, filename))
    return None
S.rd_add_and_stream = fake_add

def verdict_of(info_hash, accept):
    return S._cand_verdict_get({"info_hash": info_hash}, accept)

def run(tmdb, imdb, season, episode, lang, label):
    RESULT.clear()
    accept = S._accept_langs("anime", lang)
    # skip_account=True: keep the harness quick/offline for the account walk
    # (its remux fallback only TIGHTENS the walk bound in production).
    res = S.resolve_stream(tmdb, "anime", season, episode, None, True, "h264", lang, nocache=True)
    walk = RESULT.get("walk", [])
    print(f"\n===== {label}: lang={lang} accept={accept} — walk order ({len(walk)} candidates tried) =====")
    print(f"  result: {json.dumps(res)[:200]}")
    n_never = 0
    for i, (h, name) in enumerate(walk[:20]):
        v = verdict_of(h, accept)
        flag = ""
        if v == "reject":
            flag = "  <-- PROVEN: no accepted audio"
            n_never += 1
        elif v == "remux":
            flag = "  (proven: needs remux)"
        print(f"  {i:2d} hash={h[:10]} verdict={str(v):7s}{flag}")
        print(f"        {name[:90]}")
    print(f"  proven-no-audio candidates in the first 20 tried: {n_never}")

if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "77939"
    im = sys.argv[2] if len(sys.argv) > 2 else "tt7965802"
    s = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    ep = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    run(int(t), im, s, ep, "dub", "ANIME DUB")
    run(int(t), im, s, ep, None, "ANIME SUB")
