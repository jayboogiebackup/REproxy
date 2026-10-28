#!/usr/bin/env python3
"""CLI entry for the RD bridge resolve — run as a subprocess with a hard timeout.
Usage: resolve_cli.py <tmdb> <movie|tv> [season] [episode] [quality]
                      [--codec=h264] [--lang=dub] [--skip-account]
                      [--nocache] [--dead=<rd-url>] [--rid=<id>]"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

if __name__ == "__main__":
    tmdb = int(sys.argv[1])
    mtype = sys.argv[2]
    season = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
    episode = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else None
    quality = sys.argv[5] if len(sys.argv) > 5 and not sys.argv[5].startswith("--") else None
    skip_account = "--skip-account" in sys.argv
    nocache = "--nocache" in sys.argv
    dead = None
    codec = None
    lang = None
    rid = None
    for a in sys.argv:
        if a.startswith("--codec="):
            codec = a.split("=", 1)[1]
        if a.startswith("--lang="):
            lang = a.split("=", 1)[1]
        if a.startswith("--dead="):
            dead = a.split("=", 1)[1]
        if a.startswith("--rid="):
            rid = a.split("=", 1)[1]
    result = server.resolve_stream(tmdb, mtype, season, episode, quality, skip_account, codec, lang,
                                   nocache=nocache, dead=dead, rid=rid)
    # Mark the progress record finished so the player's torrent screen can stop
    # polling and show the outcome (this process is the one that owns the walk).
    if rid:
        server._progress_set(rid, phase="done", done=True,
                             source=result.get("source"),
                             playing=bool(result.get("url")),
                             error=result.get("error"))
    print(__import__("json").dumps(result))
