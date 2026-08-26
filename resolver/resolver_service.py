# RE:player Resolver Service — standalone stream resolution for the RE:player API.
#
# Resolves direct HLS stream URLs (nebula CDN) for any TMDB title by driving
# headless Chromium. URLs are deterministic per title → resolved once, cached
# in-memory for 24h.
#
# Endpoints:
#   GET /resolve?tmdb=387&type=tv&season=1&episode=1   → { tmdb, type, url, quality, cached, ms }
#   GET /health                                        → { status, cache_size, uptime }
#
# Run:
#   python resolver_service.py          (serves on :8799)
#   node resolver.cjs tv 387 1 1        (CLI form)

import json
import os
import subprocess
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESOLVER_DIR = os.path.dirname(os.path.abspath(__file__))
NODE = os.environ.get("NODE", "node")
NODE_PATH = os.environ.get("NODE_PATH", "/tmp/pwtest/node_modules")
PORT = int(os.environ.get("RESOLVER_PORT", "8799"))

cache = {}  # key -> (expires_at, payload)
cache_lock = threading.Lock()
CACHE_TTL = int(os.environ.get("RESOLVER_CACHE_TTL", str(24 * 3600)))
start_time = time.time()


def resolve(tmdb, mtype, season, episode):
    key = f"{mtype}:{tmdb}:{season or ''}:{episode or ''}"
    now = time.time()

    with cache_lock:
        hit = cache.get(key)
        if hit and hit[0] > now:
            return {**hit[1], "cached": True}

    args = [NODE, os.path.join(RESOLVER_DIR, "resolver.cjs"), mtype, str(tmdb)]
    if mtype == "tv":
        args += [str(season or 1), str(episode or 1)]

    env = dict(os.environ, NODE_PATH=NODE_PATH)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=90, env=env)
    if proc.returncode != 0:
        return {"error": "resolve_failed", "detail": proc.stdout.strip() or proc.stderr.strip()}

    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {"error": "bad_resolver_output", "detail": proc.stdout.strip()[:300]}

    if "error" in data:
        return data

    with cache_lock:
        cache[key] = (now + CACHE_TTL, data)
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/health":
            with cache_lock:
                size = len(cache)
            return self._json({"status": "ok", "service": "RE:player Resolver", "cache_size": size, "uptime_s": int(time.time() - start_time)})

        if u.path != "/resolve":
            return self._json({"error": "not_found"}, 404)

        try:
            tmdb = int(q.get("tmdb", [""])[0])
        except ValueError:
            return self._json({"error": "invalid_tmdb"}, 400)
        mtype = q.get("type", [""])[0]
        if mtype not in ("movie", "tv"):
            return self._json({"error": "type must be movie|tv"}, 400)
        season = int(q.get("season", [""])[0]) if q.get("season", [""])[0] else None
        episode = int(q.get("episode", [""])[0]) if q.get("episode", [""])[0] else None

        try:
            result = resolve(tmdb, mtype, season, episode)
        except subprocess.TimeoutExpired:
            return self._json({"error": "resolve_timeout"}, 504)

        status = 200 if "url" in result else 502
        return self._json(result, status)


if __name__ == "__main__":
    print(f"RE:player Resolver on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
