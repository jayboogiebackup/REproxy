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
    season = int(sys.argv[3]) if len(sys.argv) > 3 else None
    episode = int(sys.argv[4]) if len(sys.argv) > 4 else None
    quality = sys.argv[5] if len(sys.argv) > 5 else None
    result = server.resolve_stream(tmdb, mtype, season, episode, quality)
    print(__import__("json").dumps(result))
