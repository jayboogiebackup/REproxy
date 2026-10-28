#!/usr/bin/env python3
"""Exercise ext_sources.resolve() the way the player will."""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/home/xason/repeaks/hanime-proxy/rd-bridge")
import ext_sources

TMDB_KEY = "7bb9c66a1ae7bc73f2da92bd0f552345"


def tmdb_info(tmdb, mtype):
    kind = "movie" if mtype == "movie" else "tv"
    url = f"https://api.themoviedb.org/3/{kind}/{tmdb}?api_key={TMDB_KEY}&language=en-US"
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read())
    year = None
    if d.get("release_date"):
        year = int(d["release_date"][:4])
    elif d.get("first_air_date"):
        year = int(d["first_air_date"][:4])
    return {
        "title": d.get("title") or d.get("name"),
        "originalTitle": d.get("original_title") or d.get("original_name"),
        "year": year,
        "originalLanguage": d.get("original_language"),
    }


CASES = [
    ("movie", 550, None, None, "Fight Club"),
    ("movie", 157336, None, None, "Interstellar"),
    ("tv", 1396, 1, 1, "Breaking Bad S1E1"),
    ("movie", 27205, None, None, "Inception"),
]


def main():
    for mtype, tmdb, s, e, label in CASES:
        print(f"\n=== {label} ({mtype} {tmdb}) ===")
        try:
            info = tmdb_info(tmdb, mtype)
        except Exception as ex:
            print(f"  tmdb fail: {ex}")
            continue
        print(f"  tmdb: {info['title']} ({info['year']}) orig={info['originalLanguage']}")
        t0 = time.time()
        try:
            res = ext_sources.resolve(tmdb, mtype, s, e, source="castle", info=info)
        except Exception as ex:
            print(f"  resolve EXC: {type(ex).__name__}: {ex}")
            continue
        ms = int((time.time() - t0) * 1000)
        if res.get("error"):
            print(f"  -> ERROR ({ms}ms): {res['error']}")
            for rj in res.get("rejected") or []:
                print(f"     rejected: {rj.get('langs')} {rj.get('reason')}")
        else:
            print(f"  -> OK ({ms}ms) source={res.get('source')} audio={res.get('audio')} "
                  f"via={res.get('via')}")
            print(f"     langs={res.get('langs')}")
            print(f"     url={str(res.get('url'))[:105]}")
    print("\n=== vixsrc ===")
    try:
        res = ext_sources.resolve(550, "movie", source="vixsrc")
        print(f"  {res.get('error') or res.get('url')[:105]}")
    except Exception as ex:
        print(f"  vixsrc EXC: {ex}")
    print("\nstatus:", ext_sources.status())


if __name__ == "__main__":
    main()
