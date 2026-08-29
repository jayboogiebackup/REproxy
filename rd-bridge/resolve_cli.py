#!/usr/bin/env python3
"""CLI entry for the RD bridge resolve — run as a subprocess with a hard timeout.
Usage: resolve_cli.py <tmdb> <movie|tv> [season] [episode]"""
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
    codec = None
    for a in sys.argv:
        if a.startswith("--codec="):
            codec = a.split("=", 1)[1]
    result = server.resolve_stream(tmdb, mtype, season, episode, quality, skip_account, codec)
    print(__import__("json").dumps(result))
