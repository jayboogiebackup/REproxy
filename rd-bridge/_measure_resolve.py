#!/usr/bin/env python3
"""Measure the TV resolve path: per-source query time, account-walk time, and
the end-to-end resolve_stream time (nocache) with and without the account walk.

Read-only diagnostic. It DOES hit the real RD API (adds torrents) exactly like
production, because that is what we are measuring. Never modifies server state
beyond the normal resolve caches.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("RD_SKIP_CACHE_PURGE", "1")
import server as S  # noqa: E402


def t(label, fn):
    t0 = time.time()
    try:
        r = fn()
    except Exception as e:
        r = f"ERR {e!r}"
    dt = time.time() - t0
    n = len(r) if isinstance(r, list) else (r if not isinstance(r, dict) else "dict")
    print(f"  {label:34s} {dt:7.2f}s  -> {n}")
    return dt, r


def sources(tmdb, mtype, season, episode):
    imdb = S.tmdb_to_imdb(tmdb, "tv" if mtype != "movie" else "movie")
    print(f"\n== [{mtype} {tmdb} S{season}E{episode} imdb={imdb}] per-source (SERIAL, as coded) ==")
    tot = 0.0
    tot += t("search_torrentio", lambda: S.search_torrentio(imdb, mtype, season, episode))[0]
    tot += t("search_mediafusion", lambda: S.search_mediafusion(imdb or f"tmdb:{tmdb}", mtype, season, episode))[0]
    tot += t("search_torznab", lambda: S.search_torznab(None, imdb=imdb, mtype=mtype, season=season, episode=episode))[0]
    print(f"  {'SERIAL total':34s} {tot:7.2f}s")
    return imdb


def account(tmdb, mtype, season, episode):
    print(f"  -- account library walk (rd_find_by_title) --")
    t0 = time.time()
    st, body = S.http_get(f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}/{tmdb}?api_key={S.TMDB_KEY}", timeout=15)
    tdata = json.loads(body)
    names = set()
    for k in ("title", "name", "original_title", "original_name"):
        if tdata.get(k):
            names.add(tdata[k])
    print(f"  {'tmdb title fetch':34s} {time.time()-t0:7.2f}s  -> {sorted(names)}")
    hit = None
    t1 = time.time()
    for q in list(names)[:4]:
        if not q:
            continue
        url = S.rd_find_by_title(q, season, episode, None)
        if url:
            hit = (q, url)
            break
    print(f"  {'rd_find_by_title loop':34s} {time.time()-t1:7.2f}s  -> {'HIT ' + hit[0] if hit else 'MISS'}")
    if hit:
        print(f"      url: {hit[1][:110]}")
    return hit


def full(tmdb, mtype, season, episode, skip_account):
    label = f"resolve_stream nocache skip_account={skip_account}"
    t0 = time.time()
    r = S.resolve_stream(tmdb, mtype, season, episode, None, skip_account, None, None, nocache=True)
    dt = time.time() - t0
    print(f"  {label:34s} {dt:7.2f}s")
    print(f"      title={str((r or {}).get('title'))[:70]!r} remux={(r or {}).get('needs_remux')} "
          f"src={(r or {}).get('source')} _sources={(r or {}).get('_sources')}")
    if (r or {}).get("error"):
        print(f"      ERROR={r.get('error')}")
    return dt, r


if __name__ == "__main__":
    targets = [
        (2316, "tv", 1, 1, "The Office S01E01"),
    ]
    if len(sys.argv) > 1 and sys.argv[1] == "season":
        targets = [(1396, "tv", 1, 1, "Breaking Bad S01E01 (season-pack)")]
    for tmdb, mtype, s, e, label in targets:
        print(f"\n############ {label} ############")
        sources(tmdb, mtype, s, e)
        account(tmdb, mtype, s, e)
        full(tmdb, mtype, s, e, False)
        full(tmdb, mtype, s, e, True)
