#!/usr/bin/env python3
"""Hermetic tests for the FLAG-GATED streaming remux (`_serve_streaming_remux`
+ the job machinery) in rd-bridge/server.py.

Run: RD_SKIP_CACHE_PURGE=1 python3 _test_streaming_remux.py

Covers the traps from references/streaming-remux-and-cached-first.md:
  * `os.replace(tmp, cache)` moving the file out from under a lagging client
    must NOT truncate the response (the tail trap);
  * the live stream answers 200 + `Accept-Ranges: none` + `Connection: close`
    (no Content-Length), so the browser does not treat it as seekable;
  * a Range OFFSET is honoured when the job has already written past it;
  * a no-growth stall bails instead of holding the socket forever;
  * the flag defaults OFF and only turns on for `BRIDGE_STREAMING_REMUX=1`;
  * N concurrent readers tail one job (the .tmp IS the shared buffer).

Does NOT touch the live resolve / verdict / remux caches.
"""
import io
import os
import sys
import tempfile
import threading
import time

os.environ["RD_SKIP_CACHE_PURGE"] = "1"
os.environ.setdefault("REALDEBRID_API_KEY", "test-key")
os.environ.setdefault("TMDB_API_KEY", "test-key")
os.environ.pop("BRIDGE_STREAMING_REMUX", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S  # noqa: E402

FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler on the track path."""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.sent = []
        self.close_connection = False

    def send_response(self, code):
        self.sent.append(("__status__", code))

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass

    def _acao(self):
        return "https://embed.repeaks.xyz"


def headers_of(h):
    return {k: v for k, v in h.sent if k != "__status__"}


def status_of(h):
    return next(v for k, v in h.sent if k == "__status__")


def new_job(tmp, cache):
    return {"tmp": tmp, "cache": cache, "done": threading.Event(),
            "ok": False, "pos": 0, "proc": None}


# ── 1. flag defaults OFF ────────────────────────────────────────────────
check("flag: default OFF", S.streaming_remux_enabled() is False)
os.environ[S.STREAMING_REMUX_ENV] = "1"
check("flag: ON for BRIDGE_STREAMING_REMUX=1", S.streaming_remux_enabled() is True)
os.environ[S.STREAMING_REMUX_ENV] = "0"
check("flag: OFF again for ==0", S.streaming_remux_enabled() is False)

# ── 2. the tail trap: os.replace(tmp, cache) while the client is behind ──
with tempfile.TemporaryDirectory() as d:
    tmp = os.path.join(d, "file.mp4.tmp")
    cache = os.path.join(d, "file.mp4")
    job = new_job(tmp, cache)
    h = FakeHandler()
    total = bytearray()

    def writer():
        with open(tmp, "wb") as f:
            for i in range(6):
                blk = bytes([65 + i]) * (256 * 1024)
                f.write(blk)
                f.flush()
                job["pos"] += len(blk)
                total.extend(blk)
                time.sleep(0.05)
        # Simulate a clean job exit: the tail trap. The reader may still be
        # behind the download head when the file MOVES to the cache name.
        os.replace(tmp, cache)
        job["ok"] = True
        job["done"].set()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    S._serve_streaming_remux(h, job, cache, tmp, "TESTID1")
    t.join(5)

    got = h.wfile.getvalue()
    hdrs = headers_of(h)
    check("tail trap: full body delivered across os.replace",
          got == bytes(total) and len(got) == len(total))
    check("tail trap: status 200 + Accept-Ranges none + no Content-Length",
          status_of(h) == 200 and hdrs.get("Accept-Ranges") == "none"
          and "Content-Length" not in hdrs)
    check("tail trap: Connection close set", hdrs.get("Connection") == "close")
    check("tail trap: close_connection raised", h.close_connection is True)

# ── 3. Range OFFSET honoured while the job is mid-flight ────────────────
with tempfile.TemporaryDirectory() as d:
    tmp = os.path.join(d, "r.mp4.tmp")
    cache = os.path.join(d, "r.mp4")
    job = new_job(tmp, cache)
    payload = bytes(range(256)) * 4096  # 1 MiB
    with open(tmp, "wb") as f:
        f.write(payload)
    job["pos"] = len(payload)
    job["done"].set()
    h = FakeHandler(headers={"Range": "bytes=524288-"})
    S._serve_streaming_remux(h, job, cache, tmp, "TESTID2")
    check("range offset: body starts at the requested offset",
          h.wfile.getvalue() == payload[524288:])

# ── 4. no-growth stall bails instead of hanging ────────────────────────
with tempfile.TemporaryDirectory() as d:
    tmp = os.path.join(d, "s.mp4.tmp")
    cache = os.path.join(d, "s.mp4")
    job = new_job(tmp, cache)
    with open(tmp, "wb") as f:
        f.write(b"x" * 1024)
    job["pos"] = 1024
    old = S._REMUX_STALL_BAIL
    S._REMUX_STALL_BAIL = 0.6  # never completes, nothing new arrives
    try:
        h = FakeHandler()
        t0 = time.time()
        S._serve_streaming_remux(h, job, cache, tmp, "TESTID3")
        elapsed = time.time() - t0
    finally:
        S._REMUX_STALL_BAIL = old
    check("stall bail: returns within the bail window", elapsed < 3.0)
    check("stall bail: delivered what existed", h.wfile.getvalue() == b"x" * 1024)

# ── 5. two concurrent readers tail ONE job (the .tmp is the buffer) ─────
with tempfile.TemporaryDirectory() as d:
    tmp = os.path.join(d, "c.mp4.tmp")
    cache = os.path.join(d, "c.mp4")
    job = new_job(tmp, cache)
    blob = bytes(0x5A for _ in range(512 * 1024))
    with open(tmp, "wb") as f:
        f.write(blob)
    job["pos"] = len(blob)
    job["done"].set()
    h1, h2 = FakeHandler(), FakeHandler()
    th1 = threading.Thread(target=S._serve_streaming_remux,
                           args=(h1, job, cache, tmp, "TESTID4"))
    th2 = threading.Thread(target=S._serve_streaming_remux,
                           args=(h2, job, cache, tmp, "TESTID4"))
    th1.start(); th2.start(); th1.join(5); th2.join(5)
    check("two readers: both received the whole file",
          h1.wfile.getvalue() == blob and h2.wfile.getvalue() == blob)

# ── 6. a failed job ends the stream honestly (no hang, no garbage) ──────
with tempfile.TemporaryDirectory() as d:
    tmp = os.path.join(d, "f.mp4.tmp")
    cache = os.path.join(d, "f.mp4")
    job = new_job(tmp, cache)
    with open(tmp, "wb") as f:
        f.write(b"partial")
    job["pos"] = 7
    os.unlink(tmp)          # a failed job deletes its .tmp
    job["done"].set()
    h = FakeHandler()
    t0 = time.time()
    S._serve_streaming_remux(h, job, cache, tmp, "TESTID5")
    check("failed job: response ends promptly", time.time() - t0 < 2.0)
    check("failed job: no bytes invented", h.wfile.getvalue() == b"")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("ALL STREAMING-REMUX CHECKS PASSED")
