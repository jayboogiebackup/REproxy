#!/usr/bin/env python3
"""
RE:player — Real-Debrid bridge (runs on the Raspberry Pi).
Scrapes torrent indexes (APIBay/TPB + EZTV + Nyaa) for a title, checks
Real-Debrid instant availability, and returns a DIRECT playable stream URL
that our native player can play (no browser, no third-party player).

Flow:  tmdb+type → imdb id (TMDB) → torrent search (APIBay/EZTV/Nyaa)
       → RD instantAvailability (cached?) → addMagnet → selectFiles
       → /streaming/transcode → direct MP4/mkv URL

The Pi only talks to RD's API — RD does the actual fetching, so the
Pi's IP is never exposed to torrent peers. Proxy/Tor optional for the
indexer scrapes (via HTTP(S)_PROXY env).

Endpoints:
  GET /health
  GET /api/rd/stream?tmdb=&type=&season=&episode=   → direct stream URL
  GET /api/rd/search?q=                              → raw torrent hits

Config:
  REALDEBRID_API_KEY  (required)
  TMDB_API_KEY        (defaults to the repeaks key)
  HTTP_PROXY/HTTPS_PROXY  (optional, for indexer scrapes)
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rd_flow import match_episode_file_index, pick_rd_files, pick_link_index, VIDEO_EXTS  # noqa: E402

_info_cache = {}  # torrent_id -> (timestamp, info) — big packs are slow to fetch
_codec_cache = {}  # url -> (timestamp, codec_name) — ffprobe is ~3s, cache it
_resolve_cache = {}  # key -> (timestamp, result dict) — successful resolves,
#                     # cached 6h so repeat plays skip the 20s account+Torrentio
#                     # walk. Persisted to disk across restarts.


def _resolve_cache_get_entry(key):
    """(timestamp, result) for a live cache entry, else (None, None)."""
    entry = _resolve_cache.get(key)
    if entry and time.time() - entry[0] < 21600:
        return entry[0], entry[1]
    return None, None


def _resolve_cache_get(key):
    return _resolve_cache_get_entry(key)[1]


def _write_resolve_cache():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resolve_cache.json")
        with open(p, "w") as f:
            json.dump({k: (v[0], v[1]) for k, v in _resolve_cache.items()}, f)
    except Exception:
        pass


def _resolve_cache_set(key, result):
    _resolve_cache[key] = (time.time(), result)
    _write_resolve_cache()


def _resolve_cache_load():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resolve_cache.json")
        with open(p) as f:
            data = json.load(f)
        for k, (ts, v) in data.items():
            _resolve_cache[k] = (ts, v)
    except Exception:
        pass


_resolve_cache_load()


def _codec_cache_save():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".codec_cache.json")
        with open(p, "w") as f:
            json.dump({k: (v[0], v[1]) for k, v in _codec_cache.items()}, f)
    except Exception:
        pass


def _codec_cache_load():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".codec_cache.json")
        with open(p) as f:
            data = json.load(f)
        for k, (ts, v) in data.items():
            _codec_cache[k] = (ts, v)
    except Exception:
        pass


_codec_cache_load()


# ── audio probe (codec + real language per stream) ────────────────────
# ONE header-only ffprobe serves both the codec check and the audio-language
# check. Cached in memory + on disk so the resolve subprocess, the main
# server (track endpoint) and later resolves all share it.
_audio_probe_cache = {}  # RD file id -> (timestamp, {"tracks": [...], "safe": bool|None})

SAFE_AUDIO_CODECS = ("aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le",
                     "pcm_s24le", "pcm_f32le", "pcm_u8")
UNSAFE_AUDIO_CODECS = ("ac3", "eac3", "dts", "dts-hd", "mlp", "truehd")


def _audio_probe_cache_save():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".audio_probe_cache.json")
        with open(p, "w") as f:
            json.dump({k: (v[0], v[1]) for k, v in _audio_probe_cache.items()}, f)
    except Exception:
        pass


def _audio_probe_cache_load():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".audio_probe_cache.json")
        with open(p) as f:
            data = json.load(f)
        for k, (ts, v) in data.items():
            _audio_probe_cache[k] = (ts, v)
    except Exception:
        pass


_audio_probe_cache_load()


def _probe_key(url):
    """Stable cache key for an RD file: the file id in /d/{ID}/... (survives
    CDN host rotation)."""
    try:
        for frag in ("/d/", "/stream/"):
            if frag in url:
                return url.split(frag)[1].split("/")[0]
    except Exception:
        pass
    return url


def audio_tracks(url):
    """Audio streams of the RD file in FILE ORDER as [{"codec","lang"}].
    None when it can't be probed (callers must never block playback then).
    Order matters: the first entry is the track a browser plays by default."""
    return _probe_audio(url).get("tracks")


# ── real video codec (Firefox has NO HEVC decoder) ────────────────────
# A filename hint ("x265" in the release name) MISSES unmarked 2160p releases,
# and the account walk's torrent name often says nothing about the codec. The
# video codec is read from the SAME header probe as the audio, so knowing it
# costs one extra field, not one extra ffprobe.
_HEVC_VIDEO_CODECS = ("hevc", "h265", "x265")
# Cover art ("attached_pic") is exposed by ffprobe as a real video stream with
# one of these codecs. It must never be mistaken for the feature's codec — a
# JPEG poster in front of a HEVC feature would otherwise read as "playable".
_IMAGE_CODECS = ("mjpeg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "jpeg2000")


def _is_hevc_codec(vc):
    return (vc or "").lower() in _HEVC_VIDEO_CODECS


def _real_video_codec(codec_name):
    """The ffprobe codec_name if it is an actual video codec, else None
    (empty, or an image codec used for embedded cover art)."""
    c = (codec_name or "").lower()
    if not c or c in _IMAGE_CODECS:
        return None
    return c


def video_codec(url):
    """The file's REAL video codec ("h264" / "hevc" / "av1" / ...), probed once
    and cached. None when it cannot be determined — callers must then fall back
    to the filename hint and never hard-block on it."""
    info = _probe_audio(url)
    if not isinstance(info, dict):
        return None
    if "vcodec" in info:
        return info.get("vcodec")
    # Cache entry written before the video codec was probed — one extra
    # (video-only) header read, remembered so it happens at most once per file.
    # A read that TIMED OUT is not cached (same as _probe_audio): a transient
    # failure must never be remembered as "unprobeable" and mask a real HEVC.
    import subprocess as _sp
    vc = None
    probed = False
    try:
        proc = _sp.run(["ffprobe", "-v", "error",
                        "-show_entries", "stream=codec_type,codec_name",
                        "-of", "json", url],
                       capture_output=True, text=True, timeout=25)
        data = json.loads(proc.stdout or "{}")
        probed = True
        for st in (data.get("streams") or []):
            if (st.get("codec_type") or "").lower() != "video":
                continue
            vc = _real_video_codec(st.get("codec_name"))
            if vc:
                break
    except Exception:
        probed = False
    if not probed:
        return None
    try:
        info["vcodec"] = vc
        _audio_probe_cache[_probe_key(url)] = (time.time(), info)
        _audio_probe_cache_save()
    except Exception:
        pass
    return vc


def firefox_video_ok(url, name=None):
    """False when a Firefox (codec=h264) request must NOT be given this file:
    Firefox cannot decode HEVC at all. Decided on the REAL probed codec, falling
    back to the filename hint when the probe cannot tell. AV1 is deliberately
    allowed — Firefox/Chrome/Edge all decode AV1 natively."""
    vc = video_codec(url)
    if vc is not None:
        return not _is_hevc_codec(vc)
    return not _is_hevc_name(name if name is not None else _url_name(url))


def _probe_audio(url, ttl=21600):
    """Header-only ffprobe → {"tracks": [{"codec","lang"}...], "safe": bool|None,
    "vcodec": str|None} in a SINGLE call (~3s, cached on disk for 6h).
    "safe" mirrors audio_codec_is_browser_safe: True when the FIRST audio track
    decodes in browsers, False for AC-3/DTS/TrueHD (silent), None when unknown.
    "vcodec" is the first VIDEO stream's codec (e.g. "h264", "hevc", "av1") —
    Firefox has no HEVC decoder at all, so a Firefox (codec=h264) resolve needs
    the REAL video codec, not just a filename hint. Costing nothing extra: the
    audio and video streams come from the same header read."""
    key = _probe_key(url)
    now = time.time()
    c = _audio_probe_cache.get(key)
    if c and now - c[0] < ttl:
        return c[1]
    info = {"tracks": None, "safe": None, "vcodec": None}  # type: dict
    import subprocess as _sp
    try:
        # NO -select_streams: one read returns every stream (video + audio), so
        # the video codec is known without a second ffprobe.
        proc = _sp.run(["ffprobe", "-v", "error",
                        "-show_entries", "stream=codec_type,codec_name:stream_tags=language",
                        "-of", "json", url],
                       capture_output=True, text=True, timeout=25)
        d = json.loads(proc.stdout or "{}")
        tracks = []
        for st in (d.get("streams") or []):
            ctype = (st.get("codec_type") or "").lower()
            cname = (st.get("codec_name") or "").lower()
            if ctype == "video" and info["vcodec"] is None:
                # Cover-art streams (mjpeg/png) are codec_type=video too — the
                # first REAL video codec wins, never a poster.
                info["vcodec"] = _real_video_codec(cname)
            if ctype != "audio":
                continue
            tracks.append({"codec": cname,
                           "lang": ((st.get("tags") or {}).get("language") or "").lower()})
        info["tracks"] = tracks
        codec = tracks[0]["codec"] if tracks else ""
        if codec:
            info["safe"] = codec in SAFE_AUDIO_CODECS
            if codec in UNSAFE_AUDIO_CODECS:
                # TrueHD is NOT browser-playable (silent in Chrome/Edge) —
                # same family as DTS/AC-3.
                info["safe"] = False
        # A filename fast-path verdict (if we already have one) wins: it is
        # what the previous code returned without probing.
        fc = _codec_cache.get(key)
        if fc and isinstance(fc[1], bool) and codec:
            info["safe"] = fc[1]
        _audio_probe_cache[key] = (now, info)
        _audio_probe_cache_save()
        return info
    except Exception:
        return info  # can't probe — never block playback on it


# ── audio language decision ───────────────────────────────────────────
# Languages that must NEVER be served to an English request. "jpn" is
# deliberately absent: Japanese is the ORIGINAL audio of anime (it is only
# wrong when the user asked for the English dub, which the plan checks).
FOREIGN_AUDIO_LANGS = {
    "spa", "esp", "ita", "fre", "fra", "ger", "deu", "por", "rus", "pol",
    "ukr", "nld", "dut", "swe", "nor", "hun", "ron", "ell", "bul", "hrv",
    "srp", "slo", "ces", "cze", "heb", "tha", "vie", "tur", "hin", "ara",
    "zho", "chi", "kor", "ben", "tam", "tel", "msa", "fil",
}


def _norm_lang(code):
    """Normalise an ffprobe language tag (ISO 639-2, sometimes 639-1 or a
    full name) to a 3-letter code; "" / unknown → "und"."""
    c = (code or "").strip().lower().rstrip(".")
    return {
        "en": "eng", "english": "eng", "eng": "eng",
        "ja": "jpn", "japanese": "jpn", "jpn": "jpn",
        "es": "spa", "esp": "spa", "spanish": "spa", "spa": "spa",
        "it": "ita", "italian": "ita", "ita": "ita",
        "fr": "fre", "fra": "fre", "french": "fre", "fre": "fre",
        "de": "ger", "deu": "ger", "german": "ger", "ger": "ger",
        "pt": "por", "portuguese": "por", "por": "por",
        "ru": "rus", "russian": "rus", "rus": "rus",
        "und": "und", "unknown": "und", "": "und",
    }.get(c, c) or "und"


def _accept_langs(mtype, lang):
    """Which audio languages satisfy a request:
      lang=dub (anime)      → English only (that is the dub track)
      anime (sub)           → Japanese, English acceptable
      movie/tv (default)    → English
    """
    if (lang or "").lower() in ("dub", "eng", "en", "english"):
        return ("eng",)
    if mtype == "anime":
        return ("jpn", "eng")
    return ("eng",)


def audio_plan(tracks, accept):
    """How a file must be played for the requested audio, from its REAL
    audio streams (ffprobe order):
      "fits"    the first audio track already is an accepted language → play
                the file DIRECTLY: no remux, instant start.
      "remux"   an accepted language exists but is not first → the track
                endpoint has to remux to it.
      "reject"  no accepted language anywhere (e.g. a Spanish/Italian dub for
                an English request) → never serve this file.
      "unknown" can't tell (no probe / untagged streams) → play directly;
                blocking playback on an unknown is worse than trying it.
    """
    if not tracks:
        return "unknown"
    langs = [_norm_lang(t.get("lang")) for t in tracks]
    if langs[0] in accept:
        return "fits"
    for l in langs[1:]:
        if l in accept:
            return "remux"
    if all(l == "und" for l in langs):
        return "unknown"
    return "reject"


# Release-name markers that state a foreign audio track. Matched on whole
# tokens so titles are never misread ("Captain America" has no ITA token),
# and deliberately WITHOUT plain "SPANISH"/"ITALIAN"/"FRENCH" — those are
# common words in English titles ("The Italian Job"). They are only a cheap
# pre-filter; the probe of the real audio tracks is the actual decision.
FOREIGN_NAME_TOKENS = {
    "SPA", "ESP", "ITA", "FRE", "FRA", "GER", "DEU", "POR", "RUS", "POL",
    "UKR", "NLD", "DUT", "SWE", "NOR", "HUN", "RON", "ELL", "BUL", "HRV",
    "SRP", "SLO", "CES", "CZE", "HEB", "THA", "VIE", "TUR", "HIN", "ARA",
    "ZHO", "KOR", "BEN", "TAM", "TEL", "MSA", "LATINO", "CASTELLANO",
    "ESPANOL", "SUBTITULADO", "SUBTITULADA", "DUBBED",
    # NOTE: "VOSTFR" is deliberately NOT here. VOSTFR (version originale
    # sous-titrée en français) describes FRENCH SUBTITLES over the ORIGINAL
    # audio — usually English. Treating it as a foreign-audio marker rejected
    # valid English-audio releases outright. Subtitling never changes audio.
}
ENGLISH_NAME_TOKENS = {"ENG", "ENGLISH", "EN", "MULTI", "DUAL", "MULTIAUDIO",
                       "DUALAUDIO", "ENGDUB", "VOSTENG"}


def _foreign_only_name(name):
    """True when a release name declares foreign audio and gives no sign of
    English/multi audio. Cheap pre-filter — the real decision is the probe."""
    if not name:
        return False
    toks = set(re.split(r"[^A-Za-z0-9]+", name.upper()))
    if not (toks & FOREIGN_NAME_TOKENS):
        return False
    return not (toks & ENGLISH_NAME_TOKENS)


def _url_name(url):
    """The release filename from an RD URL (percent-decoded)."""
    try:
        return urllib.parse.unquote((url or "").split("/")[-1] or "")
    except Exception:
        return ""


# ── remux coordination ────────────────────────────────────────────────
# The track endpoint is a ThreadingHTTPServer: the player (and its
# stall-recovery watchdog) can ask for the same remux twice at once. One
# lock per RD file id keeps a second request from starting a duplicate
# multi-GB ffmpeg — it waits for the first to finish and then serves the
# cached file.
_remux_locks = {}
_remux_locks_guard = threading.Lock()
_remux_failed = {}  # file id -> timestamp of the last failed remux


def _remux_lock(file_id):
    with _remux_locks_guard:
        lk = _remux_locks.get(file_id)
        if lk is None:
            lk = threading.Lock()
            _remux_locks[file_id] = lk
        return lk


def _remux_mark_failed(file_id):
    _remux_failed[file_id] = time.time()


def _remux_recently_failed(file_id, ttl=60):
    ts = _remux_failed.get(file_id)
    return bool(ts and time.time() - ts < ttl)


def audio_codec_is_browser_safe(url, ttl=3600, filename_hint=None):
    """Probe the file's audio codec via ffprobe (header-only, ~3s, cached).
    Returns True when the audio will play in browsers (AAC/MP3/Opus/Vorbis/
    FLAC/PCM), False for AC-3/DTS/TrueHD/E-AC-3 (silent in Chrome) — or
    None when it can't be determined (play anyway, don't block).

    filename_hint: when the torrent filename explicitly names the audio codec,
    decide from the name and skip the ~3s ffprobe entirely."""
    # Fast path: explicit codec markers in the filename → no ffprobe needed.
    # (The URL's last segment IS the file name — e.g. .../Zootopia%202...DolbyD%205.1.mp4)
    if not filename_hint:
        try:
            import urllib.parse as _up
            filename_hint = _up.unquote(url.split("/")[-1] or "")
        except Exception:
            pass
    if filename_hint:
        n = (filename_hint or "").upper()
        if any(x in n for x in ("AC3", "AC-3", "EAC3", "E-AC-3", "DTS", "TRUEHD", "TRUE-HD", "ATMOS", "DOLBYD", "DOLBY DIGITAL", "DD 5.1", "DD5.1", "DDP", "DTS-HD")):
            _codec_cache[_probe_key(url)] = (time.time(), False)
            return False
        if any(x in n for x in ("AAC", "AAC2.0", "MP3", "OPUS", "VORBIS", "FLAC", "MP4A", "MPEG-4 AUDIO", "LC-AAC")):
            _codec_cache[_probe_key(url)] = (time.time(), True)
            return True
    # Ambiguous name → probe once (codec + languages in ONE ffprobe, cached
    # for 6h in .audio_probe_cache.json).
    return _probe_audio(url).get("safe")


def rd_get(path):
    req = urllib.request.Request(f"{RD_API}{path}", headers={"Authorization": f"Bearer {RD_KEY}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def rd_get_cached(tid, ttl=20):
    """rd_get with a short cache — fetching a 71-file pack takes ~10s+."""
    now = time.time()
    c = _info_cache.get(tid)
    if c and now - c[0] < ttl:
        return c[1]
    info = rd_get(f"/torrents/info/{tid}")
    _info_cache[tid] = (now, info)
    return info

# Optional .env loader (key stays out of git)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    for line in open(_env_path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

RD_KEY = os.environ.get("REALDEBRID_API_KEY", "").strip()
TMDB_KEY = os.environ.get("TMDB_API_KEY", "7bb9c66a1ae7bc73f2da92bd0f552345")
RD_API = "https://api.real-debrid.com/rest/1.0"
PORT = int(os.environ.get("RD_PORT", "8801"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


# =========================================================================
# CACHE PROVENANCE + DEAD-LINK FAILOVER
#
# A Real-Debrid direct link is minted against the account/session that created
# it and is time-limited. MEASURED: after a subscription renewal (same token
# text is irrelevant — RD rotates the session), every previously cached
# `.../d/<FILEID>/...` URL answers HTTP 404 while a fresh resolve of the SAME
# title/file returns a NEW file id that answers 206. So a cache holding a link
# across a renewal is a cache holding a DEAD link, for up to its 6h TTL.
#
# Two defences, both cheap:
#   1. Every cache key is prefixed with a short fingerprint of the current RD
#      token; on startup the cache is purged when that fingerprint changed, so
#      a renewal invalidates stale links AUTOMATICALLY instead of serving them.
#   2. A failed candidate is blacklisted (`dead=` from the player) and never
#      re-served; the resolve then falls through to the next candidate. Cached
#      links older than _LINK_VERIFY_AFTER are also probed with a 2-byte Range
#      request before being handed out.
# =========================================================================
_CACHE_FP = None

# A cached direct link that is older than this is probed (2-byte Range) before
# it is served. Warm repeat plays inside this window stay instant (0.07s).
_LINK_VERIFY_AFTER = 900  # seconds

_dead_links = {}  # RD file id -> timestamp first reported/probed dead
_DEAD_LINK_TTL = 1800

# ── provenance of the account, not just of the token ──────────────────────
# The user's renewal did NOT change the token text, yet every cached link died:
# RD rotates the direct links with the subscription period. So the fingerprint
# must include ACCOUNT state — `expiration` moves forward on a renewal. The
# /user call is cheap and re-read at most once per _FP_TTL.
_FP_TTL = 600
_FP_RECHECK_SECS = 600
_FP_LOCK = threading.Lock()
_FP_STATE = {"ts": 0.0, "fp": None, "token": None, "exp": None, "id": None,
             "inconclusive": True}


def _token_fingerprint():
    """Short, non-reversible fingerprint of the current RD API key. Only the
    first 10 hex chars of a SHA-256 are exposed (behind the auth gate) — never
    enough to recover the token."""
    raw = (RD_KEY or "").strip()
    if not raw:
        return "nokey"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _cache_fingerprint(force=False):
    """Fingerprint of the RD ACCOUNT/session the cached links were minted
    against. A renewal keeps the token but moves `expiration` forward and
    rotates every direct link, so the account state MUST be part of it —
    otherwise a renewal would silently keep serving 404s for 6h.

    Cached twice for speed: in memory (per process) AND on disk, because every
    resolve runs in a fresh SUBPROCESS — an in-memory-only TTL would mean one
    /user call per resolve. Only the long-lived server (force=True, every
    _FP_RECHECK_SECS) actually calls the API. On any failure it falls back to
    the token-only hash and says so (`inconclusive`), which is what stops a
    transient network blip from wiping a perfectly good cache."""
    global _CACHE_FP
    now = time.time()
    tok = _token_fingerprint()
    if not force:
        with _FP_LOCK:
            if _FP_STATE["fp"] and (now - _FP_STATE["ts"]) < _FP_TTL:
                return _FP_STATE["fp"]
        prev = _read_fp_file()
        try:
            prev_ok = (prev and prev.get("token") == tok
                       and (now - float(prev.get("ts") or 0)) < _FP_TTL)
        except Exception:
            prev_ok = False
        if prev_ok:
            with _FP_LOCK:
                _FP_STATE.update({"ts": float(prev.get("ts") or now),
                                  "fp": prev.get("fp"), "token": prev.get("token"),
                                  "exp": prev.get("exp"), "id": prev.get("id"),
                                  "inconclusive": False})
                _CACHE_FP = prev.get("fp")
            return prev.get("fp")
    fp = tok
    exp = None
    uid = None
    inconclusive = True
    try:
        req = urllib.request.Request(
            f"{RD_API}/user",
            headers={"Authorization": f"Bearer {RD_KEY}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        exp = str(d.get("expiration") or "") or None
        uid = str(d.get("id") or "") or None
        if exp:
            fp = hashlib.sha256(f"{tok}|{exp}|{uid}".encode("utf-8")).hexdigest()[:10]
            inconclusive = False
    except Exception:
        pass
    with _FP_LOCK:
        _FP_STATE.update({"ts": now, "fp": fp, "token": tok, "exp": exp,
                          "id": uid, "inconclusive": inconclusive})
        _CACHE_FP = fp
    return fp


def _cache_key(tmdb, mtype, season, episode, quality, skip_account, lang):
    """Resolve cache key. The account fingerprint is part of the key, so a
    renewal can never key-match an entry written under the previous session —
    belt and braces on top of the startup purge."""
    return (f"{_cache_fingerprint()}|{tmdb}|{mtype}|{season}|{episode}|"
            f"{quality}|{skip_account}|{lang}")


_FP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resolve_cache_fp")


def _read_fp_file():
    """Last recorded provenance, or None when unreadable/legacy (a legacy
    plain-text file is treated as 'no provenance' → one purge, then clean)."""
    try:
        with open(_FP_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("fp") else None
    except Exception:
        return None


def _write_fp_file():
    try:
        with open(_FP_FILE, "w") as f:
            json.dump({"fp": _FP_STATE["fp"], "token": _FP_STATE["token"],
                       "exp": _FP_STATE["exp"], "id": _FP_STATE["id"],
                       "ts": _FP_STATE["ts"] or time.time()}, f)
    except Exception:
        pass


def _cache_purge_entries(fp):
    """Drop every cached resolve that is not bound to the current account
    fingerprint (legacy entries and entries from a previous session)."""
    dropped = 0
    for k in list(_resolve_cache.keys()):
        if not str(k).startswith(str(fp) + "|"):
            _resolve_cache.pop(k, None)
            dropped += 1
    if dropped:
        _write_resolve_cache()
    return dropped


def _cache_purge_stale_provenance(force=False):
    """Purge cached resolves whose links belong to a PREVIOUS RD session and
    record the current provenance. Called at startup and every _FP_RECHECK_SECS
    by the watcher thread, so a renewal invalidates stale links automatically
    instead of serving dead ones for up to 6h. Returns (reason, dropped)."""
    fp = _cache_fingerprint(force=force)
    with _FP_LOCK:
        st = dict(_FP_STATE)
    prev = _read_fp_file()
    reason = None
    if prev is None:
        reason = "no provenance on record (first run / legacy cache)"
    elif prev.get("token") != st.get("token"):
        reason = "RD token changed"
    elif st.get("exp") and prev.get("exp") and st.get("exp") != prev.get("exp"):
        reason = "RD account session rotated (subscription renewed)"
    elif prev.get("fp") != fp and not st.get("inconclusive"):
        reason = "cache fingerprint changed"
    dropped = 0
    if reason:
        dropped = _cache_purge_entries(fp)
        _dead_links.clear()
        print(f"[rd-bridge] cache provenance: {reason} -> purged {dropped} "
              f"entr{'y' if dropped == 1 else 'ies'} (fp={fp})", flush=True)
    _write_fp_file()
    return reason, dropped


def _provenance_watch():
    """Daemon: re-fingerprint the account every _FP_RECHECK_SECS and purge the
    resolve cache when it changed — a renewal is picked up without a restart."""
    while True:
        time.sleep(_FP_RECHECK_SECS)
        try:
            _cache_purge_stale_provenance(force=True)
        except Exception:
            pass


def _link_id(url):
    """Identity of an RD direct link: its /d/<FILEID>/ segment (URL-decoded —
    the player double-encodes). Falls back to the path without the query."""
    u = urllib.parse.unquote(url or "")
    m = re.search(r"/d/([A-Za-z0-9]+)", u)
    if m:
        return m.group(1).upper()
    return u.split("?")[0]


def _mark_dead(url):
    """Remember that this direct link failed. Never fatal: an empty/unknown
    link is simply not remembered."""
    if not url or not _is_rd_url(url):
        return
    lid = _link_id(url)
    if lid:
        _dead_links[lid] = time.time()
        # drop any cache entry currently holding it
        for k in list(_resolve_cache.keys()):
            try:
                cur = (_resolve_cache[k][1] or {}).get("url", "")
            except Exception:
                continue
            if cur and _link_id(cur) == lid:
                _resolve_cache.pop(k, None)


def _is_dead(url):
    if not url:
        return False
    ts = _dead_links.get(_link_id(url))
    if ts and time.time() - ts < _DEAD_LINK_TTL:
        return True
    return False


def _link_live(url, timeout=6):
    """2-byte Range probe against an RD direct link. True = live OR
    inconclusive (a network blip must never nuke a good link)."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-1",
                                                  "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200) in (200, 206)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 410):
            return False
        return True
    except Exception:
        return True


_cache_purge_stale_provenance()


# =========================================================================
# SECURITY LAYER — access control, per-IP rate limiting, outbound allowlist
#
# Threat model: rd.repeaks.xyz is a PUBLIC hostname and every call to
# /api/rd/stream spends the owner's PAID Real-Debrid quota. The embed player
# cannot send a custom header (its cross-origin resolve MUST stay a simple GET
# with no preflight — see EmbedPlayer.tsx), so the shared secret travels as a
# query parameter `t=`.
#
# Two gates, either one admits the request:
#   1. t=<BRIDGE_TOKEN>          — shared secret. It is baked into the public
#                                  client, so it only RAISES THE BAR (stops
#                                  URL harvesters, bots, casual abuse); it is
#                                  NOT a real credential.
#   2. Origin/Referer allowlist  — keeps the already-deployed token-less client
#                                  working. Spoofable with curl, which is
#                                  exactly why the edge (Cloudflare WAF) is the
#                                  real enforcement point — see HARDENING.md.
# Once the client ships the token, set BRIDGE_ALLOW_ORIGIN_COMPAT=0 in .env to
# make the token MANDATORY.
# =========================================================================

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
ALLOW_ORIGIN_COMPAT = os.environ.get("BRIDGE_ALLOW_ORIGIN_COMPAT", "1").strip() != "0"

ALLOWED_ORIGINS = {
    "https://embed.repeaks.xyz",
    "https://www.embed.repeaks.xyz",
    "https://repeaks.xyz",
    "https://www.repeaks.xyz",
    "https://api.repeaks.xyz",
    "https://www.api.repeaks.xyz",
}
# Canonical CORS origin used when the caller is not an allowlisted browser origin.
CORS_ORIGIN = "https://embed.repeaks.xyz"

# Only Real-Debrid's own CDN may ever be fetched or 302-redirected to. This
# closes the open-redirect + SSRF that /api/rd/track?url= used to allow: ANY
# url was forwarded verbatim into a `Location:` header and handed to ffmpeg
# (which happily speaks file:// and plain http:// on the LAN).
_RD_URL_RE = re.compile(r"^https://([a-z0-9-]+\.)*download\.real-debrid\.com/", re.I)


def _is_rd_url(u):
    return bool(u) and bool(_RD_URL_RE.match(u))


def _client_ip(handler):
    """Real client IP behind the Cloudflare tunnel.

    CF-Connecting-IP is stamped by Cloudflare's edge and cannot be forged by
    the caller. X-Forwarded-For is only a fallback for direct/local access,
    where forging it merely lands the caller in a bucket of their choosing.
    """
    for h in ("CF-Connecting-IP", "X-Real-IP"):
        v = handler.headers.get(h)
        if v:
            return v.strip()
    xff = handler.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else "unknown"


def _request_origin(handler):
    o = (handler.headers.get("Origin") or "").strip().lower()
    if o and o != "null":
        return o
    ref = (handler.headers.get("Referer") or "").strip()
    if ref:
        try:
            p = urllib.parse.urlparse(ref)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}".lower()
        except Exception:
            pass
    return ""


def _constant_eq(a, b):
    if not a or not b or len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a.encode(), b.encode()):
        r |= x ^ y
    return r == 0


def _authorized(handler, token):
    if BRIDGE_TOKEN and _constant_eq(token or "", BRIDGE_TOKEN):
        return True
    if ALLOW_ORIGIN_COMPAT and _request_origin(handler) in ALLOWED_ORIGINS:
        return True
    return False


def _audit(msg):
    """Abuse log. NEVER logs the query string (it can carry the token)."""
    try:
        print(f"[sec] {time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)
    except Exception:
        pass


# ── per-IP token bucket ──────────────────────────────────────────────────
_RL_LOCK = threading.Lock()
_RL = {}                      # ip -> [tokens, last_ts]
_RL_SWEEP = [0.0]

RL_PER_MIN = float(os.environ.get("BRIDGE_RL_PER_MIN", "120"))
RL_BURST = float(os.environ.get("BRIDGE_RL_BURST", "40"))
RL_ACTIVE_CAP = int(os.environ.get("BRIDGE_MAX_CONCURRENT", "8"))


def _rate_ok(ip, cost=1.0):
    now = time.time()
    with _RL_LOCK:
        if now - _RL_SWEEP[0] > 300:      # bounded memory (else it's a DoS itself)
            _RL_SWEEP[0] = now
            for k in [k for k, v in _RL.items() if now - v[1] > 900]:
                _RL.pop(k, None)
        st = _RL.get(ip)
        if st is None:
            st = [RL_BURST, now]
            _RL[ip] = st
        st[0] = min(RL_BURST, st[0] + (now - st[1]) * (RL_PER_MIN / 60.0))
        st[1] = now
        if st[0] < cost:
            return False
        st[0] -= cost
        return True


# ── global concurrency cap for expensive work (subprocess / ffmpeg) ──────
_active_lock = threading.Lock()
_active = [0]


def _acquire_slot():
    with _active_lock:
        if _active[0] >= RL_ACTIVE_CAP:
            return False
        _active[0] += 1
        return True


def _release_slot():
    with _active_lock:
        if _active[0] > 0:
            _active[0] -= 1


def _env_int(v, lo, hi, default):
    try:
        n = int(str(v).strip())
    except Exception:
        return default
    return n if lo <= n <= hi else default


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def rd_post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{RD_API}{path}", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {RD_KEY}",
                                          "User-Agent": UA,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if not raw:
            return {}
        return json.loads(raw)


_imdb_cache = {}  # (tmdb, mtype) -> imdb_id


def tmdb_to_imdb(tmdb, mtype):
    """TMDB id → IMDb id via external_ids (cached — 0.4s saved per call)."""
    key = (tmdb, mtype)
    if key in _imdb_cache:
        return _imdb_cache[key]
    try:
        st, body = http_get(f"https://api.themoviedb.org/3/{mtype}/{tmdb}/external_ids?api_key={TMDB_KEY}", timeout=15)
        d = json.loads(body)
        imdb = d.get("imdb_id")
        _imdb_cache[key] = imdb
        return imdb
    except Exception:
        return None


def search_apibay(q, category="0"):
    """TPB/APIBay JSON search — movies, tv, anime, k-dramas all covered."""
    try:
        st, body = http_get(f"https://apibay.org/q.php?q={urllib.parse.quote(q)}&cat={category}", timeout=20)
        d = json.loads(body)
        if not isinstance(d, list):
            return []
        return [t for t in d if t.get("info_hash") and t.get("name")]
    except Exception:
        return []


def search_torrentio(imdb, mtype, season=None, episode=None):
    """Torrentio public API — 100+ indexers, returns infoHash + fileIdx + seeders."""
    try:
        if mtype == "movie":
            url = f"https://torrentio.strem.fun/stream/movie/{imdb}.json"
        else:
            url = f"https://torrentio.strem.fun/stream/series/{imdb}:{season or 1}:{episode or 1}.json"
        st, body = http_get(url, timeout=25)
        d = json.loads(body)
        hits = []
        for s in d.get("streams", []):
            ih = s.get("infoHash")
            if not ih:
                continue
            # parse seeders from the title (👤 N) and quality from name
            title = s.get("title", "") or ""
            import re as _re
            m = _re.search(r"[\U0001F464]\s*(\d+)", title)
            seeders = int(m.group(1)) if m else 0
            bh = s.get("behaviorHints") or {}
            hits.append({"name": title[:90], "info_hash": ih, "file_idx": s.get("fileIdx"),
                         "filename": bh.get("filename", ""), "seeders": seeders,
                         "size_gb": _size_gb(title),
                         "source": "torrentio", "quality": s.get("name", "")[:30]})
        return hits
    except Exception:
        return []


def search_eztv(imdb, season=None, episode=None):
    """EZTV API — TV shows by IMDb id, per season/episode."""
    try:
        url = f"https://eztvx.to/api/get-torrents?imdb_id={imdb}&limit=30"
        if season:
            url += f"&season={season}"
        st, body = http_get(url, timeout=20)
        d = json.loads(body)
        hits = []
        for t in d.get("torrents", []):
            if episode and int(t.get("episode") or 0) != int(episode):
                continue
            hits.append({"name": t.get("title", ""), "info_hash": t.get("hash", ""),
                         "seeders": t.get("seeders", 0), "source": "eztv",
                         "magnet": t.get("magnet_url", "")})
        return hits
    except Exception:
        return []


def rd_instant_available(hashes):
    """Check which hashes are cached on RD (POST /torrents/instantAvailability).
       Returns dict hash → files."""
    try:
        body = urllib.parse.urlencode({"hashes": ",".join(h.upper() for h in hashes)}).encode()
        req = urllib.request.Request(f"{RD_API}/torrents/instantAvailability", data=body, headers={
            "Authorization": f"Bearer {RD_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except Exception:
        return {}


_torrents_cache = {}  # timestamp -> torrents list (RD list is 100+ entries, ~1s)


def rd_get_torrents(ttl=5):
    """GET /torrents with a short cache — the list is fetched many times per
    resolve (account-first + rd_find_existing_torrent per candidate)."""
    now = time.time()
    c = _torrents_cache.get("list")
    if c and now - c[0] < ttl:
        return c[1]
    req = urllib.request.Request(f"{RD_API}/torrents", headers={"Authorization": f"Bearer {RD_KEY}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        ts = json.loads(r.read())
    _torrents_cache["list"] = (now, ts)
    return ts


def rd_find_by_title(q, season=None, episode=None, codec=None):
    """Search the account's existing torrents by title keywords → link.
       If season+episode given, finds the matching SxxExx file inside a
       season pack (selects the right file, not just the first link).
       codec='h264' skips HEVC/AV1 torrents (returns the next matching one)."""
    try:
        ts = rd_get_torrents()
        words = [w.lower() for w in q.split() if (w.isdigit() or len(w) > 2) and w.lower() not in
                 ("and", "the", "for", "with", "from", "that", "this", "not", "are", "was", "but", "you", "all", "she", "his", "her", "its", "has", "had", "have", "who", "which", "what", "when", "where", "why", "how")]
        movie_hits = []  # (codec_score, filename, torrent) for movie mode
        tv_hits = []  # (codec_score, torrent) for tv mode — ranked after
        for t in ts:
            if t.get("status") != "downloaded" or not t.get("links"):
                continue
            fn = (t.get("filename") or "").lower()
            # codec=h264: skip HEVC/AV1 torrents entirely (try the next match)
            if codec == "h264" and _is_hevc_name(t.get("filename", "")):
                continue
            # Title words must ALWAYS match (word boundaries). The episode tag
            # is an additional filter when an episode is requested — but the
            # tag alone (e.g. Loki S02E01 matching "60 Days In" S2E1 request
            # via s02e01) must NEVER pass without a title word match.
            if not any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", fn) for w in words):
                continue
            if season is None and episode is None:
                # Movie mode: the torrent must NOT look like TV content and
                # must match MULTIPLE title words (a single shared word like
                # "days" in "60 Days In" must never match "X-Men: Days of
                # Future Past"). A movie must never resolve to an episode or
                # a season pack.
                matched_words = [w for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", fn)]
                if len(matched_words) < 2 and len(words) >= 2:
                    continue
                if re.search(r"s\d{1,2}e\d{1,2}", fn) or re.search(r"\b\d{1,2}x\d{2}\b", fn):
                    continue
                if re.search(r"\bseason\b|\bs\d{1,2}\b.*(pack|complete|collection)|(pack|complete|collection).*\bs\d{1,2}\b", fn):
                    continue
                movie_hits.append((codec_rank(t.get("filename", "")), t))
                continue
            elif season is not None and episode is not None:
                # TV mode: collect ALL title-matching torrents (packs named
                # "S1-7" AND singles with the SxxExx tag). The best one is
                # chosen after by codec rank + episode availability — a pack
                # with good audio beats a first-found AC-3 single.
                tv_hits.append((codec_rank(t.get("filename", "")), t))
                continue
            # If asking for a specific episode, find the matching file
            if season is not None and episode is not None:
                try:
                    info = rd_get_cached(t["id"])
                    tag = f"s{int(season):02d}e{int(episode):02d}"
                    links = info.get("links") or []
                    for f in info.get("files", []):
                        if tag in f.get("path", "").lower():
                            # RD keeps one link per SELECTED file. If the pack
                            # has fewer links than files (partial selection),
                            # we can't fetch this episode from this torrent —
                            # return None so the caller tries Torrentio instead.
                            if len(links) < len(info.get("files") or []):
                                # verify a link exists for this specific file
                                # (selected files are contiguous from 1)
                                if f["id"] > len(links):
                                    return None
                            idx = f["id"] - 1
                            if 0 <= idx < len(links):
                                for lf in (links[idx],):
                                    try:
                                        ur = rd_post("/unrestrict/link", {"link": lf})
                                        dl = ur.get("download") or ur.get("streamable")
                                        if dl:
                                            # verify the returned URL is the right
                                            # episode (file-id → link order can be
                                            # off on packs)
                                            want = f"s{int(season):02d}e{int(episode):02d}"
                                            if want in dl.lower().replace("-", "").replace("_", "").replace(".", "").replace(" ", ""):
                                                return dl
                                    except Exception:
                                        continue
                                return None
                            break
                except Exception:
                    pass
                # If we asked for a specific episode but this torrent has no
                # matching file/link, move on — NEVER fall back to links[0]
                # (that could be a different episode of a pack).
                continue
            try:
                ur = rd_post("/unrestrict/link", {"link": t["links"][0]})
                dl = ur.get("download") or ur.get("streamable")
                if dl:
                    return dl
                return t["links"][0]
            except Exception:
                return t["links"][0]
        # TV mode: try ranked candidates — best audio first. For each, find
        # the episode's file→link, unrestrict, verify the episode tag, and
        # SKIP silent (AC-3/DTS) files — a pack with AAC beats an AC-3 single.
        if season is not None and episode is not None and tv_hits:
            tv_hits.sort(key=lambda x: x[0])
            tag = f"s{int(season):02d}e{int(episode):02d}"
            for _score, t in tv_hits:
                try:
                    info = rd_get_cached(t["id"])
                    links = info.get("links") or []
                    # Strict tag first (s01e01), then looser episode patterns
                    # (ep 1 / episode 01 / " 01 ") for releases with different
                    # naming (BlueLobster etc.) — only when the strict tag
                    # finds nothing usable.
                    for pattern in (tag, None):
                        # Collect ALL matching files for this pattern, then pick
                        # the best: prefer no A/B/C suffix (full episode), then
                        # the 'A' part (first half) over 'B'. SpongeBob-style
                        # packs tag S01E01A + S01E01B as separate files — the
                        # matcher must not grab E01B for an E1 request.
                        matched = []
                        for f in info.get("files", []):
                            p = f.get("path", "").lower()
                            if pattern is not None:
                                ok = pattern in p
                            else:
                                ep_pat = rf"(?<![\w])e{int(episode):02d}(?![\w])|(?<![\w])ep[\s._-]?{int(episode):02d}(?![\w])|[\s._-]{int(episode):02d}[\s._-]"
                                ok = bool(__import__("re").search(ep_pat, p))
                            if not ok:
                                continue
                            # Loose matching can grab the wrong SEASON's file
                            # (e.g. "Megalobox 2 - 01" when season 1 is wanted).
                            # If the filename declares a season, it must agree.
                            if pattern is None and season is not None:
                                m = re.search(r"\bseason\s*\d+\b|\bs\d{2}\b", p)
                                if m and str(season) not in m.group(0):
                                    continue
                            # codec=h264: skip HEVC/AV1 files inside packs too
                            if codec == "h264" and _is_hevc_name(f.get("path", "")):
                                continue
                            matched.append(f)
                        if not matched:
                            continue
                        # Sort: no-suffix first, then A, then B/C; stable so
                        # same-episode duplicates keep file order
                        def _ab_key(fx):
                            n = fx.get("path", "").lower()
                            m2 = re.search(rf"s{int(season):02d}e{int(episode):02d}([a-z])", n)
                            suf = m2.group(1) if m2 else ""
                            return (0 if not suf else (1 if suf == "a" else 2))
                        matched.sort(key=_ab_key)
                        f = matched[0]
                        idx = f["id"] - 1
                        if 0 <= idx < len(links):
                            try:
                                ur = rd_post("/unrestrict/link", {"link": links[idx]})
                                dl = ur.get("download") or ur.get("streamable")
                                if dl:
                                    want = f"s{int(season):02d}e{int(episode):02d}"
                                    import urllib.parse as _up
                                    dl_dec = _up.unquote(dl).lower()
                                    flat = dl_dec.replace("-", "").replace("_", "").replace(".", "").replace(" ", "")
                                    # Verify: strict s01e01 tag, OR a loose
                                    # episode marker for differently-named
                                    # releases (npz/BlueLobster " - 01 ")
                                    loose_ok = bool(__import__("re").search(
                                        rf"(?<![\w])e{int(episode):02d}(?![\w])|(?<![\w])ep[\s._-]?{int(episode):02d}(?![\w])|[\s._-]{int(episode):02d}[\s._-]",
                                        dl_dec))
                                    if want in flat or loose_ok:
                                        # skip silent files (AC-3/DTS).
                                        # Fast-path on the filename; if the
                                        # name is ambiguous, ffprobe (cached).
                                        safe = audio_codec_is_browser_safe(dl, filename_hint=f.get("path", ""))
                                        if safe is False:
                                            break  # try next torrent
                                        if safe is None:
                                            # ambiguous name — ffprobe once
                                            safe = audio_codec_is_browser_safe(dl)
                                            if safe is False:
                                                break  # silent — next torrent
                                        return dl
                            except Exception:
                                continue
                        # This file matched the pattern but failed
                        # verification (e.g. loose pattern hit "ep 02"
                        # when "ep 01" was requested). Try the NEXT file in
                        # this torrent before giving up on it.
                        continue
                except Exception:
                    continue
            return None
        # Movie mode: return the BEST (audio-safe, H264) account match
        if movie_hits:
            movie_hits.sort(key=lambda x: x[0])
            for _score, best in movie_hits:
                try:
                    ur = rd_post("/unrestrict/link", {"link": best["links"][0]})
                    dl = ur.get("download") or ur.get("streamable")
                    if dl:
                        return dl
                except Exception:
                    continue  # 451 / dead link — try the next account match
            return None
    except Exception:
        pass
    return None


def rd_find_existing(info_hash):
    """Look for an already-added torrent with this hash → return its unrestrictable link."""
    try:
        ts = rd_get_torrents()
        for t in ts:
            if (t.get("hash") or "").lower() == info_hash.lower() and t.get("status") == "downloaded" and t.get("links"):
                return t["links"][0]
    except Exception:
        pass
    return None


def rd_find_existing_torrent(info_hash):
    """Find the torrent id of an already-added hash (any status).
    Returns (id, link_count) — link_count from the fast /torrents list,
    so we can skip big packs with no links for the wanted episode without
    fetching the (slow) full file list."""
    try:
        ts = rd_get_torrents()
        for t in ts:
            if (t.get("hash") or "").lower() == info_hash.lower():
                return t["id"], len(t.get("links") or [])
    except Exception:
        pass
    return None, 0


def rd_add_and_stream(info_hash, file_idx=None, filename=None, season=None, episode=None):
    """Harbor-proven flow: addMagnet → poll → selectFiles/{id} (files=1,2,3)
       → poll downloaded → pickLinkIndex → unrestrict → direct URL.
       Returns None fast for non-cached (Torrentio-style)."""
    # 0. Already in the account? Use the existing link (instant, no re-add).
    #    Re-adding the same hash makes RD re-download the whole pack — slow.
    #    Instead, find the original torrent and pick the matching episode link.
    existing_t = rd_find_existing_torrent(info_hash)
    if existing_t:
        existing_id, existing_links = existing_t
        # Big pack with no/few links: the wanted episode isn't selected in the
        # original torrent. Re-add it — the FRESH instance is in
        # waiting_files_selection where selectFiles WORKS (RD downloads all
        # selected files on their servers).
        if season is not None and episode is not None and existing_links < 10:
            pass  # fall through to re-add below
        else:
            try:
                info = rd_get_cached(existing_id)
                links = info.get("links") or []
                if links:
                    # if we want a specific episode, map file → link
                    if season is not None and episode is not None:
                        tag = f"s{int(season):02d}e{int(episode):02d}"
                        for f in info.get("files") or []:
                            if tag in f.get("path", "").lower():
                                idx = f["id"] - 1
                                if 0 <= idx < len(links):
                                    try:
                                        ur = rd_post("/unrestrict/link", {"link": links[idx]})
                                        dl = ur.get("download") or ur.get("streamable")
                                        # verify the returned URL is the right episode
                                        # (file-id → link order can be off on packs)
                                        if dl:
                                            want = f"s{int(season):02d}e{int(episode):02d}"
                                            if want in dl.lower().replace("-", "").replace("_", "").replace(".", "").replace(" ", ""):
                                                return dl
                                        return None
                                    except Exception:
                                        continue
                                return None  # file not selected/linked — can't play
                        return None  # no matching episode file in this torrent
                    # Movie / no-episode: map via file_idx or filename hint
                    # (a pack contains MANY movies — links[0] is wrong unless
                    # the requested title is the first file).
                    eff_idx2 = file_idx
                    if eff_idx2 is None and filename:
                        for i, f in enumerate(info.get("files") or []):
                            if filename.lower() in f.get("path", "").lower() or f.get("path", "").lower().endswith(filename.lower()):
                                eff_idx2 = i
                                break
                    li2 = pick_link_index(info.get("files"), eff_idx2, len(links))
                    for probe in ([li2] + [i for i in range(len(links)) if i != li2][:5]):
                        if probe < 0 or probe >= len(links):
                            continue
                        try:
                            ur = rd_post("/unrestrict/link", {"link": links[probe]})
                            dl = ur.get("download") or ur.get("streamable")
                            if dl:
                                if filename:
                                    want = filename.lower()[:20].replace("-", "").replace("_", "").replace(".", "").replace(" ", "")
                                    if want in dl.lower().replace("-", "").replace("_", "").replace(".", "").replace(" ", ""):
                                        return dl
                                else:
                                    return dl
                        except Exception:
                            continue
                    return None
            except Exception:
                pass

    # 1. Add magnet — FULL magnet with trackers bypasses RD's 451 filter
    #    (hash-only magnets get flagged as infringing; full magnet+trackers
    #    passes like Torrentio/Stremio do)
    magnet = (f"magnet:?xt=urn:btih:{info_hash}"
              f"&tr=udp://tracker.opentrackr.org:1337/announce"
              f"&tr=udp://open.demonii.com:1337/announce"
              f"&tr=udp://tracker.openbittorrent.com:6969/announce"
              f"&tr=udp://exodus.desync.com:6969/announce")
    if filename:
        magnet += f"&dn={urllib.parse.quote(filename[:80])}"
    try:
        added = rd_post("/torrents/addMagnet", {"magnet": magnet})
    except Exception:
        return None
    tid = added.get("id")
    if not tid:
        return None  # 451 infringing / invalid → next candidate

    info = None
    selected = False
    eff_idx = file_idx
    try:
        for attempt in range(60):  # up to ~2min: cached = instant, non-cached = RD downloads
            try:
                info = rd_get(f"/torrents/info/{tid}")
            except Exception:
                break
            status = info.get("status")
            files = info.get("files") or []
            if status == "magnet_error":
                break
            if status in ("magnet_conversion", "waiting_files_selection") and not selected:
                # For a pack re-add (episode requested but pack has few links),
                # select ALL video files so every episode becomes playable —
                # RD downloads them all on their servers.
                if file_idx is not None or filename or (season is not None and episode is not None):
                    select_all = True
                else:
                    select_all = False
                if select_all:
                    file_ids = [f["id"] for f in files if f.get("path", "").lower().endswith(VIDEO_EXTS)] or [f["id"] for f in files]
                else:
                    # resolve the episode file index from the filename hint or SxxExx
                    if eff_idx is None and filename:
                        for i, f in enumerate(files):
                            if filename.lower() in f.get("path", "").lower() or f.get("path", "").lower().endswith(filename.lower()):
                                eff_idx = i
                                break
                    if eff_idx is None and season is not None and episode is not None:
                        mi = match_episode_file_index([f.get("path", "") for f in files], season, episode)
                        if mi >= 0:
                            eff_idx = mi
                    file_ids = pick_rd_files(files, eff_idx)
                if not file_ids:
                    break
                try:
                    rd_post(f"/torrents/selectFiles/{tid}", {"files": ",".join(str(x) for x in file_ids)})
                except Exception:
                    break
                selected = True
                time.sleep(0.6)
                continue
            if status == "downloaded":
                break
            if status in ("downloading", "queued"):
                # RD is fetching it on THEIR servers (Stremio-style). For huge
                # packs this can take minutes — don't block the whole resolve;
                # give it a few polls then give up (caller tries next hash).
                # Local counter (not global) — concurrent candidates must not
                # share this.
                if attempt >= 10:
                    break
                time.sleep(2)
                continue
            if status in ("error", "virus", "dead"):
                break
            time.sleep(0.6)
    except Exception:
        pass

    if not info or info.get("status") != "downloaded":
        try:
            rd_get(f"/torrents/delete/{tid}")
        except Exception:
            pass
        return None

    links = info.get("links") or []
    if not links:
        return None
    if eff_idx is None and season is not None and episode is not None:
        mi = match_episode_file_index([f.get("path", "") for f in (info.get("files") or [])], season, episode)
        if mi >= 0:
            eff_idx = mi
    link_idx = pick_link_index(info.get("files"), eff_idx, len(links))
    # Verify the chosen link is actually the right FILE: try the mapped link
    # first; if its filename doesn't match the requested title/episode, walk
    # nearby links (file-id order can drift on freshly-added packs).
    import re as _re
    for probe in ([link_idx] + [i for i in range(len(links)) if i != link_idx][:4]):
        if probe < 0 or probe >= len(links):
            continue
        try:
            ur = rd_post("/unrestrict/link", {"link": links[probe]})
            dl = ur.get("download") or ur.get("streamable")
            if dl:
                fnl = dl.lower().replace("-", "").replace("_", "").replace(".", "").replace(" ", "")
                want_pat = None
                if season is not None and episode is not None:
                    want_pat = f"s{int(season):02d}e{int(episode):02d}"
                elif filename:
                    want_pat = filename.lower()[:20].replace("-", "").replace("_", "").replace(".", "").replace(" ", "")
                if want_pat:
                    if want_pat in fnl:
                        return dl
                else:
                    return dl  # no way to verify — trust the mapping
        except Exception:
            continue
    return None


# ── candidate ordering: QUALITY-FIRST, sanity-filtered ────────────────
# HARD RULES for the RD resolver:
#   (1) a CAM/TS/telesync release is never used while an HD (720p+) release
#       exists — _filter_cam_when_hd drops it outright and the order key's
#       leading cam flag demotes any straggler below every non-cam release;
#   (2) candidates are ordered by quality, highest fidelity first: resolution
#       (2160→480) descending, with unmarked names below any explicit height.
# The old POPULARITY-FIRST intent survives INSIDE a fidelity tier (language
# preference, a sanity tier, a sane size band, seeder count, then codec_rank).
# The PROBED gates (audio language, browser-safe audio, Firefox video codec)
# still decide what is actually returned — this only decides what is tried
# first, so the highest-quality SENSIBLE candidate wins instead of first-match.
_SANE_SIZE_MIN_GB = 0.12   # below this it is a sample or a broken release
_SANE_SIZE_MAX_GB = 25.0   # above this it is a remux pack, not an episode


def _size_gb(name):
    """Release size in GB parsed from a release name. Torrentio embeds
    '💾 1.72 GB' in its title; APIBay/EZTV rows carry a plain size field
    (handled by _candidate_size_gb). None when the name says nothing."""
    n = (name or "").upper()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:GB|GIB)\b", n)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:MB|MIB)\b", n)
    if m:
        return float(m.group(1)) / 1024.0
    return None


def _candidate_size_gb(c):
    """Best-effort size for a candidate: an explicit field (bytes or GB) first,
    then the release name."""
    for k in ("size_gb", "size_bytes", "size"):
        v = c.get(k)
        if v is None:
            continue
        try:
            n = float(v)
        except Exception:
            continue
        if n <= 0:
            continue
        if k == "size_gb":
            return n
        # bytes → GB (a value below 1MiB cannot be bytes for video)
        return n / (1024.0 ** 3) if n > 1024 * 1024 else n
    return _size_gb(c.get("name", ""))


def _size_rank(gb):
    """0 = sane for one episode/feature, 1 = unknown, 2 = sample or absurd."""
    if gb is None:
        return 1
    if gb < _SANE_SIZE_MIN_GB or gb > _SANE_SIZE_MAX_GB:
        return 2
    return 0


def _lang_rank(c, lang):
    """lang=dub (anime): an English-ONLY dub release ranks above Dual/Multi —
    dual-audio releases store the Japanese track first, so they always pay the
    slow ffmpeg remux. Unchanged behaviour, moved to module level so the
    ordering helper can share it."""
    n = (c.get("name", "") or "").upper()
    if lang == "dub":
        if any(x in n for x in ("DUAL", "MULTI")):
            return 1  # English present, but not as the first track
        if any(x in n for x in ("DUB", "ENGLISH", "EN.")):
            return 0  # dubbed — likely English-first: try first
        return 2
    return 0


def _candidate_tier(c, codec, want_h):
    """0 = release name looks browser-sensible, higher = more suspicious.
    Name-based only (the real decision is the ffprobe gate at try time)."""
    n = (c.get("name", "") or "").upper()
    tier = 0
    if codec == "h264" and _is_hevc_name(n):
        tier += 2  # Firefox has no HEVC decoder at all
    if any(x in n for x in ("DTS", "TRUEHD", "TRUE-HD", "ATMOS", "AC3", "AC-3",
                            "EAC3", "E-AC-3", "DOLBY", "DD 5.1", "DD5.1", "DDP")):
        tier += 1  # silent in Chrome
    if "HDCAM" in n or "CAMRIP" in n or "TELESYNC" in n or "TS-" in n:
        tier += 3
    if want_h:
        h = (1080 if "1080P" in n else 720 if "720P" in n else 480 if "480P" in n
             else 2160 if ("2160P" in n or "4K" in n) else None)
        if h is not None and h != want_h:
            tier += 1
    return tier


# ── fidelity tiering (HARD RULES) ─────────────────────────────────────
# Two rules, applied to every candidate list before it is ordered or tried:
#   (1) a cam / telesync / telecine release is only ever considered when the
#       candidate set holds NO genuine HD (720p+) alternative — otherwise it
#       is dropped outright (see _filter_cam_when_hd);
#   (2) the surviving candidates are ordered highest-fidelity first, so a
#       2160p beats a 1080p beats a 720p beats an unmarked name beats a cam
#       (see _quality_rank / _candidate_order_key).
_CAM_TOKENS = {
    "CAM", "CAMRIP", "HDCAM", "TS", "HDTS", "TELESYNC", "TELECINE", "TC",
    "HDTC", "NEWSOURCE", "PREDVD", "DVDSCR", "SCREENER", "VHSRIP",
}
# Substrings the previous code already used — kept as a fast path (and to
# catch glued forms like "HDCAMRIP" that do not tokenise to a marker).
_CAM_SUBSTRINGS = ("CAMRIP", "HDCAM", "TELESYNC", "TELECINE", "HDTS", "NEWSOURCE")


def _is_cam_release(name):
    """True when a release name declares a cam / telesync / telecine source —
    the lowest-fidelity tier (a recording made inside a cinema). Tokenised, so
    a "TS"/"TC" inside an unrelated word cannot false-positive; "DTS" (a legit
    audio codec) is explicitly NOT a cam marker."""
    n = (name or "").upper()
    if not n:
        return False
    if any(x in n for x in _CAM_SUBSTRINGS):
        return True
    toks = set(re.split(r"[^A-Z0-9]+", n))
    return bool(toks & _CAM_TOKENS)


def _release_height(name):
    """Explicit resolution height parsed from a release name, else None
    (an unmarked name proves nothing). Handles the usual 480/720/1080/2160
    markers plus 1440p and the 4K/UHD aliases."""
    n = (name or "").upper()
    if "2160P" in n or "4K" in n or "UHD" in n:
        return 2160
    if "1440P" in n or "QHD" in n:
        return 1440
    if "1080P" in n or "FHD" in n:
        return 1080
    if "720P" in n:
        return 720
    if "576P" in n:
        return 576
    if "480P" in n:
        return 480
    return None


def _quality_rank(c):
    """Higher = higher fidelity. Used as the PRIMARY ordering key (rule 2):
    cam/TS/telecine → 0 (always last), an unmarked name → 1 (it cannot prove
    it is HD, so any explicitly-HD release outranks it), otherwise the explicit
    pixel height (480…2160)."""
    name = c.get("name", "")
    if _is_cam_release(name):
        return 0
    h = _release_height(name)
    return 1 if h is None else h


def _filter_cam_when_hd(candidates):
    """HARD RULE 1: drop every cam / telesync / telecine release as soon as the
    set holds at least one genuine HD (720p+) alternative. Returns the possibly
    filtered list, or the input untouched when there is no HD alternative (a cam
    stream still beats no stream) or when filtering would leave it empty."""
    if not candidates:
        return candidates
    hd_alt = [c for c in candidates
              if (_release_height(c.get("name", "")) or 0) >= 720]
    if not hd_alt:
        return candidates  # nothing HD on offer — cams may be used
    kept = [c for c in candidates if not _is_cam_release(c.get("name", ""))]
    return kept or candidates


def _candidate_order_key(c, want_h, lang, codec):
    # Rule 1 (cam never above HD) is also enforced structurally: the cam flag
    # is the FIRST key (1 = cam, so it sorts last ascending), so even a cam
    # that claims "2160p" sorts below a 480p WEB. Rule 2 (highest fidelity
    # first) is the second key; language, browser-safety tier, size sanity,
    # popularity and codec_rank break ties.
    return (1 if _is_cam_release(c.get("name", "")) else 0,
            -_quality_rank(c),
            _lang_rank(c, lang),
            _candidate_tier(c, codec, want_h),
            _size_rank(_candidate_size_gb(c)),
            -int(c.get("seeders") or 0),
            codec_rank(c.get("name", ""), want_h))


def codec_rank(name, want_height=None):
    """Browser-friendly + English-first ranking. Lower = better.
    Penalizes: HEVC/AV1/2160p (unplayable), non-English dubs (RUS/CZ/SK/ES/IT/
    PT/DE/HI), HDCAM (cam). Prefers: H264, 1080p/720p, MP4, English tags.
    want_height (e.g. 720) biases toward that resolution."""
    n = (name or "").upper()
    score = 0
    if any(x in n for x in ("X265", "H265", "HEVC", "AV1", "VP9", "2160P", "4K")):
        score += 100  # unplayable in most browsers
    # Audio codecs: Chrome only decodes AAC/MP3/Opus/Vorbis/FLAC. AC-3,
    # E-AC-3, DTS, TrueHD, Atmos, PCM → video plays with NO sound.
    # (MP4 + AC-3 also silent in Chrome — DolbyD/Dolby Digital = AC-3.)
    if any(x in n for x in ("DTS", "TRUEHD", "TRUE-HD", "ATMOS", "PCM", "AC3", "AC-3", "EAC3", "E-AC-3", "DOLBYD", "DOLBY DIGITAL", "DD 5.1", "DD5.1", "DDP")):
        score += 60  # silent in Chrome/Edge/Firefox
    if "FLAC" in n:
        score += 20  # Chrome MKV/MP4 FLAC works, but less common — mild penalty
    if any(x in n for x in ("AAC", "AAC2.0", "MP3", "OPUS", "VORBIS", "AUDIO")):
        score -= 25  # always plays in browsers
    if "HDCAM" in n or "CAMRIP" in n or "TELESYNC" in n or "TS-" in n:
        score += 200  # cam/telecine quality
    # Non-English audio markers (dubbed/foreign releases)
    if any(x in n for x in ("DUB", "RUS", "CZ", "SK", "ESP", "SPANISH", "LATINO", "LATIN", "SPA.", "ITA", "ITALIAN", "POR", "PORTUGUESE", "GER", "GERMAN", "HIN", "HINDI", "ARAB", "TUR", "TURKISH", "UKR", "POL", "POLISH", "FRE", "FRENCH", "NLD", "DUTCH", "DAN", "NOR", "SWE", "FIN", "HEB", "THA", "VIE", "IND", "MAL", "TAG", "KOR", "CHI", "JAP")):
        score += 50
    if "MULTI" in n:
        score += 10  # multi-audio usually includes English, but not guaranteed
    if "ENGLISH" in n or "ENG." in n or " EN " in n:
        score -= 30
    # Explicit browser-safe audio markers are a STRONG preference — a pack
    # named "AVC1-MP4A" (AAC) must beat AC-3 singles.
    if any(x in n for x in ("MP4A", "AAC", "AAC2.0", "LC-AAC", "MPEG-4 AUDIO")):
        score -= 40
    if "X264" in n or "H264" in n or "AVC" in n:
        score += 0
    # Resolution bias: prefer the requested height when set
    if want_height:
        h = 1080 if "1080P" in n else 720 if "720P" in n else 480 if "480P" in n else None
        if h is None:
            score += 8  # unknown resolution — slight penalty
        else:
            score += abs(h - want_height) // 200  # closer = better
    else:
        if "720P" in n:
            score += 5
        if "1080P" in n:
            score += 3
    if n.endswith(".MP4") or ".MP4 " in n:
        score -= 2  # MP4 container plays more reliably than MKV
    return score


def os_subs(tmdb, mtype, season=None, episode=None):
    """OpenSubtitles search by IMDb id → list of subtitle files.
    Downloads the SRT and converts to VTT (browser-playable via <track>)."""
    try:
        imdb = tmdb_to_imdb(tmdb, "movie" if mtype == "movie" else "tv")
        if not imdb:
            return []
        url = f"https://api.opensubtitles.com/api/v1/subtitles?imdb_id={imdb}&languages=en"
        if season and episode:
            url += f"&season_number={season}&episode_number={episode}"
        req = urllib.request.Request(url, headers={"Api-Key": os.environ.get("OPENSUBTITLES_API_KEY", ""),
                                                   "User-Agent": "repeaks v1"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        out = []
        seen = set()
        for s in (d.get("data") or []):
            att = s.get("attributes", {})
            lang = att.get("language", "en")
            fn = att.get("release_name", "") or att.get("title", "") or f"Subtitle {s.get('id')}"
            fid = str(s.get("id"))
            if lang in seen:
                continue
            seen.add(lang)
            out.append({"label": lang.capitalize(), "file_id": fid, "name": fn[:60], "file": None})
        return out
    except Exception:
        return []


def os_download_vtt(file_id):
    """Fetch the SRT (legacy direct link first — the API download endpoint
    503s from datacenter IPs) → convert to VTT → cache locally."""
    import tempfile
    try:
        srt = None
        # 1) Legacy direct download link (works from server IPs)
        try:
            req0 = urllib.request.Request(
                f"https://dl.opensubtitles.org/en/download/subad/{file_id}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"})
            with urllib.request.urlopen(req0, timeout=30) as r0:
                raw = r0.read()
                if b"WEBVTT" in raw[:20] or b"--> " in raw[:2000] or b"<" not in raw[:2000]:
                    srt = raw.decode("utf-8", "replace")
                else:
                    srt = None  # HTML error page
        except Exception:
            srt = None
        # 2) API download endpoint (falls back if legacy blocked)
        if srt is None:
            body = urllib.parse.urlencode({"file_id": file_id}).encode()
            req = urllib.request.Request("https://api.opensubtitles.com/api/v1/download", data=body, method="POST",
                                         headers={"Api-Key": os.environ.get("OPENSUBTITLES_API_KEY", ""),
                                                  "User-Agent": "repeaks v1",
                                                  "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=20) as r:
                dl = json.loads(r.read())
            srt_url = dl.get("link")
            if srt_url:
                req2 = urllib.request.Request(srt_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req2, timeout=30) as r2:
                    srt = r2.read().decode("utf-8", "replace")
        if not srt:
            return None
        # SRT → VTT conversion
        srt = srt.replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for ln in srt.split("\n"):
            if "-->" in ln:
                ln = ln.replace(",", ".")
            lines.append(ln)
        vtt = "WEBVTT\n\n" + "\n".join(lines).lstrip("\n")
        # cache to disk so the player can fetch it repeatedly
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "os_cache")
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{file_id}.vtt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(vtt)
        return path
    except Exception:
        return None


def url_matches_type(url, mtype, season=None, episode=None):
    """STRICT show/movie separation: a show must never play a movie and
    vice versa. Verifies the resolved URL's filename against the request:
      - type=tv:  URL must contain the SxxExx episode pattern (or at least
                  a season/episode marker). A bare movie filename fails.
      - type=movie: URL must NOT contain an SxxExx pattern (that's an episode).
    Returns True when the URL is type-compatible."""
    if not url:
        return False
    fn = (url.split("/")[-1] or "").lower().replace("%20", " ").replace("-", "").replace("_", "").replace(".", "")
    has_ep = bool(re.search(r"s\d{1,2}e\d{1,2}", fn)) or bool(re.search(r"(?<!\d)\d{1,2}x\d{2}(?!\d)", fn))
    if mtype == "movie":
        return not has_ep
    # tv/anime: need an episode marker
    if has_ep:
        return True
    # some releases name files like "Show.Name.1x03" or "S01E03" already caught;
    # also accept a lone "E03" style or season pack folders with s01/s02
    if re.search(r"\be\d{1,3}\b", fn) or re.search(r"\bs\d{1,2}\b", fn):
        return True
    # loose episode patterns (" - 01 ", " Episode 5 ", " ep.03 ") used by
    # some anime batches (npz/BlueLobster style)
    return bool(re.search(r"(?:e|ep|episode)[\s._-]?\d{1,2}(?!\d)|[\s._-]\d{1,2}[\s._-]", fn))


def resolve_stream(tmdb, mtype, season=None, episode=None, quality=None, skip_account=False, codec=None, lang=None,
                   nocache=False, dead=None):
    """Full flow → direct stream URL, with STRICT show/movie separation:
    a show never plays a movie and vice versa. Any resolved URL that fails
    the type check is rejected. codec='h264' → H.264/AVC only (Firefox
    has NO HEVC/AV1 support, so those would fail to load there).
    lang='dub' (anime) → prefer English-dubbed releases.
    Successful resolves are cached (6h) so repeat plays skip the 20s walk.

    RELIABILITY FAILOVER:
      nocache=True  → never read the cache. A playback failure must not be
                      answered with the same dead link again.
      dead=<url>    → the caller just failed on this direct link: blacklist it
                      (and every cache entry holding it) so the walk falls
                      through to the NEXT candidate automatically.
    Cached links are bound to the current RD token fingerprint, so a debrid
    renewal invalidates them instead of serving 404s for up to 6h."""
    if dead:
        _mark_dead(dead)
    accept = _accept_langs(mtype, lang)
    ck = _cache_key(tmdb, mtype, season, episode, quality, skip_account, lang)
    # codec is deliberately EXCLUDED from the key: it's a browser preference,
    # not a different resolution — sharing the cache means Chrome's resolve
    # (0.1s) serves Firefox instantly instead of Firefox re-walking the
    # account (~19s) for the same file.
    cts, cached = (None, None) if nocache else _resolve_cache_get_entry(ck)
    soft_cached = None
    if cached is not None:
        cfn = cached.get("url", "") or ""
        stale = False
        if _is_dead(cfn):
            stale = True  # the player already failed on this exact link
        elif cfn and cts and (time.time() - cts) > _LINK_VERIFY_AFTER and not _link_live(cfn):
            # RD rotated/expired the direct link (independent of the token) —
            # never hand a dead URL to the player.
            _mark_dead(cfn)
            stale = True
        if stale:
            cached = None
            cts = None
    if cached is not None:
        cfn = cached.get("url", "")
        cname = cfn.split("/")[-1] if "/" in cfn else cfn
        # h264 requested but the cached file's REAL codec is HEVC (probed — a
        # filename check misses every unmarked 2160p x265) → Firefox has no
        # decoder for it. Re-resolve looking for an H.264 release, but keep the
        # cached one as a last resort: a Firefox ask must never end up with
        # LESS than Chrome would have been given.
        if codec == "h264" and cfn and not firefox_video_ok(cfn, cname):
            soft_cached = cached
        elif _cached_audio_ok(cached, cfn, accept):
            return cached
        # else: the cached entry is a foreign-audio release (written before
        # the audio-language check) → fall through and re-resolve.
    result = _resolve_stream_impl(tmdb, mtype, season, episode, quality, skip_account, codec, lang)
    if soft_cached is not None and not (isinstance(result, dict) and result.get("url")):
        # Nothing playable-by-Firefox exists for this title — an HEVC stream
        # beats "no stream" (the player's decode-error retry handles it).
        result = soft_cached
    url = result.get("url") if isinstance(result, dict) else None
    if url and not url_matches_type(url, mtype, season, episode):
        result = {"error": "resolved link is the wrong type (show/movie mismatch)",
                  "imdb": result.get("imdb")}
    if url and isinstance(result, dict):
        _tag_audio(result, accept)
        _resolve_cache_set(ck, result)
    return result


def _tag_audio(result, accept):
    """Attach the audio decision to a resolve result so the player knows
    whether the file plays as-is or needs the track endpoint:
      audio       "fits" | "remux" | "unknown" | "reject"
      needs_remux True when the wanted audio exists but is NOT the file's
                  first track — the only case where the ffmpeg remux (the
                  multi-second/tens-of-seconds first-play stall) is needed.
    """
    url = result.get("url")
    if not url:
        return result
    plan = audio_plan(audio_tracks(url), accept)
    result["audio"] = plan
    result["needs_remux"] = plan == "remux"
    return result


def _cached_audio_ok(cached, url, accept):
    """True when a cached resolve may be served for this audio request.
    Entries written before the audio-language check can point at a foreign
    dub (Spanish/Italian) — verify the file's REAL audio once, remember the
    verdict (so this costs one ffprobe ever), and reject it otherwise."""
    if not url:
        return False
    if cached.get("audio"):
        return cached["audio"] != "reject"
    plan = audio_plan(audio_tracks(url), accept)
    if plan == "reject":
        return False
    cached["audio"] = plan
    cached["needs_remux"] = plan == "remux"
    return True


def _audio_gate(url, accept):
    """True when this file must NOT be served for the requested audio:
    its release name declares a foreign-only dub, or its REAL audio tracks
    contain no accepted language (Spanish/Italian dub for an English ask).
    A file that can't be probed passes — an unknown is never a hard reject."""
    if _foreign_only_name(_url_name(url)):
        return True
    return audio_plan(audio_tracks(url), accept) == "reject"


def _is_hevc_name(name):
    """True when a filename indicates HEVC video (unplayable in Firefox —
    no HEVC support at all). AV1 is EXCLUDED from the list: Firefox/Chrome/
    Edge all decode AV1 natively, so AV1 files are playable."""
    n = (name or "").upper()
    return any(x in n for x in ("HEVC", "X265", "H265", "X.265", "H.265"))


def _resolve_stream_impl(tmdb, mtype, season=None, episode=None, quality=None, skip_account=False, codec=None, lang=None):
    """Full flow → direct stream URL. codec='h264' filters to H.264/AVC only.
    lang='dub' prefers English-dubbed releases for anime."""
    if not RD_KEY:
        return {"error": "REALDEBRID_API_KEY not set"}
    imdb = tmdb_to_imdb(tmdb, "movie" if mtype == "movie" else "tv")
    if not imdb:
        return {"error": "no imdb id"}
    # Which audio languages may be served for this request (English unless the
    # caller asked for something else). Every accepted candidate must pass
    # _audio_gate: a release that only carries a foreign dub is never used.
    accept = _accept_langs(mtype, lang)

    # A Firefox ask (codec=h264) must not be handed an HEVC file — Firefox has
    # NO HEVC decoder, so the viewer sees "no stream" even though a stream was
    # resolved. HEVC files used to be caught by FILENAME only ("x265"/"HEVC"),
    # which misses every unmarked 2160p release and most account files. The
    # real codec is now probed through firefox_video_ok at EVERY point a
    # candidate is accepted below (account walk, Torrentio batches, second
    # pass, APIBay/EZTV tail). Never hard-block though: a probed-HEVC file is
    # remembered by _keep_hevc and served if it is the only thing that exists
    # (the player's decode-error retry/cinesrc fallback handles it) — "no
    # stream" is worse.
    soft_hevc = {"url": None, "title": None}

    def _soft_hevc_result():
        if not soft_hevc["url"]:
            return None
        return {"url": soft_hevc["url"], "source": "realdebrid",
                "title": (soft_hevc["title"] or "")[:80], "imdb": imdb,
                "vcodec": "hevc"}

    def _keep_hevc(url, name):
        """Remember a probed-HEVC file as the last-resort fallback."""
        if not soft_hevc["url"]:
            soft_hevc["url"] = url
            soft_hevc["title"] = name or ""
        return None

    # 0. Fetch Torrentio candidates FIRST (0.1s, parallel-safe) so the slow
    #    account codec probe below overlaps with candidate prep.
    candidates = []
    candidates.extend(search_torrentio(imdb, mtype, season, episode))

    # 1. The user's OWN RD library (Harbor-style, instant) — already-
    #    downloaded content always works, no 451, no waiting. Only check the
    #    PRIMARY titles. skip_account=True bypasses this so Torrentio
    #    candidates with browser-playable audio get picked instead.
    if not skip_account:
        try:
            st, body = http_get(f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}/{tmdb}?api_key={TMDB_KEY}", timeout=15)
            tdata = json.loads(body)
            names = set()
            for k in ("title", "name", "original_title", "original_name"):
                if tdata.get(k):
                    names.add(tdata[k])
            for q in list(names)[:4]:
                if not q:
                    continue
                url = rd_find_by_title(q, season, episode, codec)
                if not url:
                    continue
                # A link the player already failed on (or that a liveness probe
                # found dead) must never be served again — fall through to the
                # next candidate instead.
                if _is_dead(url):
                    continue
                # codec=h264 (Firefox): reject HEVC by the REAL probed codec —
                # the torrent name often does not say it. HEVC is remembered as
                # a last-resort fallback, never a hard "no stream". No name is
                # passed: the URL's own filename is the better hint when the
                # probe cannot tell.
                if codec == "h264" and not firefox_video_ok(url):
                    _keep_hevc(url, q)
                    continue
                # Skip AC-3/DTS account files — they're silent in Chrome.
                # Filename hint avoids the ~3s ffprobe when the name says it.
                safe = audio_codec_is_browser_safe(url, filename_hint=q)
                if safe is False:
                    continue
                # Audio-language gate: the account walk is title-matched only,
                # so it happily returns a foreign dub ("Subtitulado.Esp") when
                # the account holds one. Never serve that for an English ask.
                if _audio_gate(url, accept):
                    continue
                return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
        except Exception:
            pass

    order = []
    tried = 0
    deadline = time.time() + 115
    # Requested resolution bias (shared by the candidate ordering below and the
    # APIBay/EZTV tail sort further down).
    want_h = 1080 if quality == "1080" else 720 if quality == "720" else 480 if quality == "480" else None
    # HARD RULE 1: never walk a CAM/TS/telecine release while an HD (720p+)
    # alternative is on the table. Drop cams here so the h264 pre-filter, the
    # ordering and the try-loop all see the same HD-only set.
    candidates = _filter_cam_when_hd(candidates)
    if candidates:
        # Browser-playable first (H264 > HEVC/AV1), biased to requested quality.
        # codec=h264: filter HEVC/AV1 out — but SOFT: if every candidate is
        # HEVC (common for anime), keep them so the resolve still returns a
        # stream (browsers with hardware decode can play it; the player's
        # decode-error retry handles the rest). Never return "no stream" just
        # because the only releases are HEVC.
        if codec == "h264":
            h264_only = [c for c in candidates if not _is_hevc_name(c.get("name", ""))]
            if h264_only:
                candidates = h264_only
            # else: keep all — HEVC-only title, prefer a stream over nothing
        # lang=dub (anime): boost English-dub releases in the order. Within
        # those, an English-ONLY dub release is ranked above Dual/Multi-audio:
        # its first audio track is the English one, so it plays directly
        # (audio_plan "fits") with no remux at all. Dual-audio releases store
        # the Japanese track first → they still need the ffmpeg remux.
        # POPULARITY-FIRST: among equally sensible candidates the most-seeded
        # one is tried first (then codec_rank breaks ties). See
        # _candidate_order_key.
        order = sorted(candidates,
                       key=lambda c: _candidate_order_key(c, want_h, lang, codec))
        # Stremio-style: try candidates until one streams. Cached = instant;
        # non-cached = RD downloads on their servers (30s-3min).
        # PARALLEL batches (3 at a time): each add is independent, so this
        # cuts the sequential ~1s-per-451 loop to ~1s per batch.
        deadline = time.time() + 115
        tried = 0
        import concurrent.futures as _cf

        def _try_one(c):
            try:
                url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"),
                                        filename=c.get("filename"), season=season, episode=episode)
                if not url:
                    return None
                # Never re-mint a link the player already failed on: RD can
                # hand back a URL for a torrent whose stored link expired, and
                # re-serving it ends playback for no reason.
                if _is_dead(url):
                    return None
                # codec=h264 (Firefox): the RELEASE NAME is only a hint — most
                # unmarked 2160p x265 releases carry no "x265"/"HEVC" token, so
                # this decides on the PROBED video codec. It costs nothing: the
                # audio probe below reads the same header, and it is cached.
                # Probed HEVC is remembered as a last resort, never a hard
                # "no stream".
                if codec == "h264" and not firefox_video_ok(url, c.get("name")):
                    _keep_hevc(url, c.get("name", ""))
                    return None
                if audio_codec_is_browser_safe(url) is False:
                    return None  # silent codec — skip
                # Audio language: reject a foreign dub outright; report how
                # the file has to be played ("fits" = play it as-is).
                if _foreign_only_name(c.get("name", "")):
                    return None
                plan = audio_plan(audio_tracks(url), accept)
                if plan == "reject":
                    return None
                return url, c, plan
            except Exception:
                return None

        BATCH = 3
        # A candidate whose English audio is not its first track needs the
        # blocking remux. Before paying that, look one extra batch further for
        # a release that plays as-is (bounded: 2 batches max, so the resolve
        # never turns into a long walk).
        MAX_BATCHES_FOR_FITS = 2
        fallback = None  # (url, candidate) needing a remux
        for bi, start in enumerate(range(0, len(order), BATCH)):
            if time.time() > deadline:
                break
            batch = order[start:start + BATCH]
            with _cf.ThreadPoolExecutor(max_workers=BATCH) as ex:
                for res in ex.map(_try_one, batch):
                    tried += 1
                    if not res:
                        continue
                    url, c, plan = res
                    if not url:
                        continue
                    if plan != "remux":
                        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
                    if fallback is None:
                        fallback = (url, c)
            if fallback is not None and bi + 1 >= MAX_BATCHES_FOR_FITS:
                break
        if fallback is not None:
            # No as-is release found — play the right audio via the remux
            # (cached after the first time) rather than a foreign dub.
            url, c = fallback
            return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
        # Second pass: try the best-seeded candidates (may include cached HEVC
        # that browsers with hardware decode CAN play)
        if time.time() < deadline:
            byseed = sorted(candidates, key=lambda c: -int(c.get("seeders") or 0))
            for c in byseed:
                if tried >= 14 or time.time() > deadline:
                    break
                if c["info_hash"] in [x["info_hash"] for x in order[:8]]:
                    continue
                tried += 1
                try:
                    url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"),
                                            filename=c.get("filename"), season=season, episode=episode)
                    if url:
                        if _is_dead(url):
                            continue
                        # Same probed-codec gate as the first pass (see _try_one).
                        if codec == "h264" and not firefox_video_ok(url, c.get("name")):
                            _keep_hevc(url, c.get("name", ""))
                            continue
                        safe = audio_codec_is_browser_safe(url)
                        if safe is False:
                            continue
                        if _foreign_only_name(c.get("name", "")):
                            continue
                        if audio_plan(audio_tracks(url), accept) == "reject":
                            continue
                        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
                except Exception:
                    continue
    # All candidates tried and none streamed — report honestly (or serve the
    # remembered HEVC if that is all this title has), skip the slow
    # APIBay/EZTV tail (search_eztv can take 30s+).
    if not candidates or tried > 0:
        return _soft_hevc_result() or {
            "error": "no stream available on real-debrid yet (try again in a few minutes)",
            "imdb": imdb}

    # Remaining sources (APIBay / EZTV)

    if mtype == "movie":
        # APIBay fallback by title + year (filter by imdb id after)
        st, body = http_get(f"https://api.themoviedb.org/3/movie/{tmdb}?api_key={TMDB_KEY}", timeout=15)
        tdata = json.loads(body)
        title = tdata.get("title", "")
        year = (tdata.get("release_date") or "")[:4]
        hits = search_apibay(f"{title} {year}", category="0")
        imdb_l = imdb.lower()
        candidates.extend([h for h in hits if h.get("imdb", "").lower() == imdb_l])
    else:
        # EZTV by IMDb id (per-episode match) + APIBay fallback
        candidates.extend(search_eztv(imdb, season, episode))
        st, body = http_get(f"https://api.themoviedb.org/3/tv/{tmdb}?api_key={TMDB_KEY}", timeout=15)
        tdata = json.loads(body)
        q = f"{tdata.get('name','')} S{int(season or 1):02d}E{int(episode or 1):02d}"
        candidates.extend(search_apibay(q, category="205"))

    # HARD RULE 1 again: the APIBay/EZTV tail can add cams the Torrentio set
    # did not have — re-apply before the final ordering below.
    candidates = _filter_cam_when_hd(candidates)

    if not candidates:
        # Niche content may already be in the user's RD account — match by
        # title, original name, or alternative titles (handles rebrands like
        # "Life on Marbs" → "60 Days In")
        try:
            st, body = http_get(f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}/{tmdb}?api_key={TMDB_KEY}", timeout=15)
            tdata = json.loads(body)
            names = set()
            for k in ("title", "name", "original_title", "original_name"):
                if tdata.get(k):
                    names.add(tdata[k])
            # alternative titles
            try:
                st2, body2 = http_get(f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}/{tmdb}/alternative_titles?api_key={TMDB_KEY}", timeout=15)
                alt = json.loads(body2)
                for a in alt.get("titles", []):
                    names.add(a.get("title", ""))
            except Exception:
                pass
            for q in names:
                if not q:
                    continue
                url = rd_find_by_title(q, season, episode, codec)
                if url:
                    if _is_dead(url):
                        continue
                    # codec=h264 (Firefox): probed-codec gate — the name
                    # pre-filter inside rd_find_by_title cannot see an unmarked
                    # 2160p x265. HEVC is kept soft, never a hard reject.
                    if codec == "h264" and not firefox_video_ok(url):
                        _keep_hevc(url, q)
                        continue
                    if _audio_gate(url, accept):
                        continue
                    return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
        except Exception:
            pass
        return _soft_hevc_result() or {"error": "no torrents found", "imdb": imdb}

    # NOTE: RD's instantAvailability endpoint is currently disabled (error 37),
    # so we try torrents directly — best-seeded first. RD downloads
    # non-cached torrents on their servers; cached ones are instant.
    # Torrentio-style: try EVERY candidate — RD 451-skips silently and
    # cached/unflagged hashes resolve instantly. Also wait longer for RD to
    # fetch non-cached torrents (RD does the downloading, not us).
    # Same popularity-first, sanity-filtered ordering as the main walk.
    order = sorted(candidates, key=lambda c: _candidate_order_key(c, want_h, lang, codec))
    tried = 0
    for c in order:
        if tried >= 40:
            break
        tried += 1
        try:
            url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"), season=season, episode=episode)
            if url:
                if _is_dead(url):
                    continue
                # codec=h264 (Firefox): probed-codec gate on the last candidate
                # source too — prefer an H.264 release, keep probed HEVC soft.
                if codec == "h264" and not firefox_video_ok(url, c.get("name")):
                    _keep_hevc(url, c.get("name", ""))
                    continue
                if _audio_gate(url, accept):
                    continue
                return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
        except Exception:
            continue  # 451 / invalid — next candidate
    return _soft_hevc_result() or {
        "error": "could not resolve via real-debrid (all candidates rejected)", "imdb": imdb}


class Handler(BaseHTTPRequestHandler):
    server_version = "repeaks-bridge"
    sys_version = ""

    def _acao(self):
        """Tight CORS. Only the site's own origins may read responses from a
        browser — not `*`, which let ANY website use the bridge as a free RD
        resolver with the visitor's IP."""
        o = _request_origin(self)
        return o if o in ALLOWED_ORIGINS else CORS_ORIGIN

    def _json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._acao())
        self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _denied(self, why, ip, status=403, extra=None):
        _audit(f"DENY {why} ip={ip} path={urllib.parse.urlparse(self.path).path}")
        return self._json({"error": "unauthorized" if status == 403 else why}, status, extra)

    def _gate(self):
        """Auth + rate limit for every /api/rd/* route. Returns a response-ish
        tuple on rejection, or None to proceed."""
        ip = _client_ip(self)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not _authorized(self, q.get("t", [None])[0]):
            return self._denied("no token / bad origin", ip, 403)
        if not _rate_ok(ip):
            return self._denied("rate limit", ip, 429, {"Retry-After": "15"})
        return None

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            # Deliberately minimal: no key/port/path disclosure. The watchdog
            # only greps for "status": "ok".
            return self._json({"status": "ok", "service": "rd-bridge"})
        if url.path == "/api/rd/stream":
            gate = self._gate()
            if gate:
                return gate
            tmdb = q.get("tmdb", [None])[0]
            mtype = q.get("type", [None])[0]
            if not tmdb or not mtype:
                return self._json({"error": "tmdb + type required"}, 400)
            if not re.fullmatch(r"\d{1,9}", str(tmdb)) or mtype not in ("movie", "tv", "anime"):
                return self._json({"error": "bad tmdb/type"}, 400)
            if not _acquire_slot():
                return self._json({"error": "busy"}, 429, {"Retry-After": "10"})
            try:
                return self._do_resolve(q, tmdb, mtype)
            finally:
                _release_slot()
        if url.path == "/api/rd/search":
            gate = self._gate()
            if gate:
                return gate
            qq = q.get("q", [""])[0]
            if not qq or len(qq) > 120:
                return self._json({"error": "q required"}, 400)
            return self._json(search_apibay(qq, q.get("cat", ["0"])[0]))
        # OpenSubtitles: list subs for a title
        if url.path == "/api/rd/subs":
            gate = self._gate()
            if gate:
                return gate
            tmdb = q.get("tmdb", [None])[0]
            mtype = q.get("type", [None])[0]
            if not tmdb or not mtype:
                return self._json({"error": "tmdb + type required"}, 400)
            if not re.fullmatch(r"\d{1,9}", str(tmdb)):
                return self._json({"error": "bad tmdb"}, 400)
            season = _env_int(q.get("season", [None])[0], 0, 100, None)
            episode = _env_int(q.get("episode", [None])[0], 0, 5000, None)
            subs = os_subs(int(tmdb), mtype, season, episode)
            return self._json({"subs": subs})
        # OpenSubtitles: get the converted VTT for a file_id
        if url.path == "/api/rd/sub":
            gate = self._gate()
            if gate:
                return gate
            fid = q.get("file_id", [None])[0]
            # digits only — file_id lands in a filesystem path AND in an
            # upstream URL, so anything else is traversal / SSRF.
            if not fid or not re.fullmatch(r"\d{1,12}", str(fid)):
                return self._json({"error": "bad file_id"}, 400)
            path = os_download_vtt(fid)
            if not path:
                return self._json({"error": "download failed"}, 404)
            try:
                with open(path, "rb") as f:
                    vtt = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/vtt; charset=utf-8")
                self.send_header("Content-Length", str(len(vtt)))
                self.send_header("Access-Control-Allow-Origin", self._acao())
                self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(vtt)
            except Exception:
                return self._json({"error": "read failed"}, 500)
            return
        if url.path == "/api/rd/track":
            gate = self._gate()
            if gate:
                return gate
            return self.do_GET_track(q)
        self._json({"error": "not found"}, 404)

    def _do_resolve(self, q, tmdb, mtype):
        season = _env_int(q.get("season", [None])[0], 0, 100, None)
        episode = _env_int(q.get("episode", [None])[0], 0, 5000, None)
        quality = q.get("quality", [None])[0]
        skip_account = q.get("skip_account", ["0"])[0] == "1"
        codec = q.get("codec", [None])[0]
        lang = q.get("lang", [None])[0]
        # Playback-failure failover: the player reports the direct link that
        # just died and asks for a fresh walk that must not serve it again.
        nocache = q.get("nocache", ["0"])[0] == "1"
        dead = q.get("dead", [None])[0]
        # Only a plausible RD direct link is accepted (it travels into the
        # subprocess argv and into the dead-link blacklist).
        if dead and (len(dead) > 512 or not _is_rd_url(dead)):
            dead = None
        # Run the resolve in a SUBPROCESS with a hard timeout — a hung
        # RD call can never deadlock the server thread pool this way.
        import subprocess as _sp
        import sys as _sys
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolve_cli.py")
        args = [_sys.executable, script, str(tmdb), mtype]
        if season is not None:
            args.append(str(season))
        if episode is not None:
            args.append(str(episode))
        if quality:
            args.append(str(quality)[:16])
        if codec:
            args.append(f"--codec={str(codec)[:16]}")
        if lang:
            args.append(f"--lang={str(lang)[:16]}")
        if skip_account:
            args.append("--skip-account")
        if nocache:
            args.append("--nocache")
        if dead:
            args.append(f"--dead={dead}")
        try:
            proc = _sp.run(args, capture_output=True, text=True, timeout=150)
            out = proc.stdout.strip()
            if out:
                result = json.loads(out.splitlines()[-1])
                if isinstance(result, dict):
                    # Token fingerprint: lets the client invalidate its own
                    # cached URL after a renewal (a changed `v` = new session =
                    # every previously cached link is dead).
                    result["v"] = _cache_fingerprint()
                return self._json(result, 200 if result.get("url") else 404)
            return self._json({"error": "resolve failed"}, 404)
        except _sp.TimeoutExpired:
            return self._json({"error": "resolve timeout (55s)"}, 504)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # GET /api/rd/track?url=<rd-url>&lang=eng — stream the RD file remuxed
    # with ONLY the requested audio track (dual-audio anime: pick eng over jpn).
    # Uses -c copy (no re-encode) so it's fast and CPU-light. The player points
    # <video> at this endpoint when lang=dub is requested on a multi-track file.
    # The remux is cached to disk (keyed by RD file hash): first play remuxes,
    # later plays serve the cached file with full Range support (instant start,
    # seekable) — this is what makes dub playback fast in Firefox.
    def do_GET_track(self, q):
        import subprocess as _sp
        import urllib.parse as _up
        raw_url = q.get("url", [None])[0]
        lang = (q.get("lang", ["eng"])[0] or "eng").lower()
        if lang in ("dub", "en", "english"):
            lang = "eng"  # normalise (also keeps the _eng.mp4 cache name)
        if not raw_url:
            return self._json({"error": "url required"}, 400)
        # SSRF / open-redirect guard: `url` used to be echoed into a 302
        # Location AND handed straight to ffmpeg, so it could point anywhere
        # (file://, 127.0.0.1, the LAN). Only RD's own CDN is ever legitimate.
        if not _is_rd_url(raw_url):
            _audit(f"DENY track non-rd url ip={_client_ip(self)}")
            return self._json({"error": "url must be a real-debrid download URL"}, 400)
        # `lang` is interpolated into a cache FILENAME and an ffmpeg -map
        # argument, so it must be a bare language tag (was: anything at all).
        if not re.fullmatch(r"[a-z]{2,3}", lang):
            return self._json({"error": "bad lang"}, 400)
        # Derive a stable cache key from the RD file ID in the URL
        # (https://{server}.download.real-debrid.com/d/{ID}/{filename}) — the
        # URL may be percent-encoded, so decode before matching.
        import urllib.parse as _up
        dec_url = _up.unquote(raw_url)
        m = re.search(r"/d/([A-Z0-9]+)/", dec_url, re.I)
        file_id = m.group(1).upper() if m else str(hash(raw_url) & 0xFFFFFFFF)
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{file_id}_{lang}.mp4")
        # ── FAST PATH: does this file even NEED a remux? ──────────────────
        # A remux only exists to pick the wanted audio track. If the file's
        # FIRST audio track already IS that language (or the streams are
        # untagged, in which case ffmpeg would just fall back to the same
        # first track), the remux would produce byte-identical audio while
        # making the viewer wait for a full multi-GB RD download + copy.
        # Redirect to the original file instead: playback starts immediately
        # and nothing is written to track_cache.
        tracks = audio_tracks(raw_url)
        plan = audio_plan(tracks, (lang,))
        if plan in ("fits", "unknown"):
            self.send_response(302)
            self.send_header("Location", raw_url)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", self._acao())
            self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if plan == "reject":
            # The file carries no <lang> track at all (e.g. a Spanish/Italian
            # dub for an English request). Refuse — never remux some other
            # language and label it English.
            langs = ",".join(_norm_lang(t.get("lang")) for t in (tracks or []))
            return self._json({"error": f"no {lang} audio track in this file (has: {langs})"}, 409)
        # plan == "remux": the wanted track exists but is not the first one.
        # Serve from cache when present (full Range support → fast Firefox)
        if os.path.exists(cache_path):
            size = os.path.getsize(cache_path)
            rng = self.headers.get("Range", "")
            if rng:
                try:
                    rng = rng.replace("bytes=", "").split("-")
                    start = int(rng[0]) if rng[0] else 0
                    end = int(rng[1]) if len(rng) > 1 and rng[1] else size - 1
                    end = min(end, size - 1)
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Access-Control-Allow-Origin", self._acao())
                    self.send_header("Vary", "Origin")
                    self.end_headers()
                    with open(cache_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", self._acao())
            self.send_header("Vary", "Origin")
            self.end_headers()
            with open(cache_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        # Not cached — remux to a file FIRST (faststart: moov atom at front),
        # then serve the complete file with Content-Length. This makes the
        # video load like a normal file (progressively buffered, seekable),
        # NOT a live stream. First play waits for the remux; later plays hit
        # the cache instantly with full Range support.
        tmp_path = cache_path + ".tmp"
        with _remux_lock(file_id):
            # Another request may have finished the remux while we waited.
            if os.path.exists(cache_path):
                pass
            elif _remux_recently_failed(file_id):
                return self._json({"error": f"no {lang} audio track — remux failed"}, 502)
            else:
                # ffmpeg pulls the whole multi-GB file and burns 600s of CPU.
                # Cap how many can run at once so a burst of anonymous calls
                # cannot pin every core on the Pi (cache hits above never take
                # a slot, so normal playback is unaffected).
                if not _acquire_slot():
                    return self._json({"error": "busy"}, 429, {"Retry-After": "15"})
                try:
                    cmd = ["ffmpeg", "-v", "error", "-y", "-i", raw_url,
                           "-map", "0:v:0", "-map", f"0:a:m:language:{lang}",
                           "-c", "copy", "-strict", "-2", "-movflags", "faststart", "-f", "mp4", tmp_path]
                    proc = _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                    proc.wait(timeout=600)
                    if proc.returncode != 0 or not os.path.exists(tmp_path):
                        # The wanted track could not be extracted. The old code
                        # fell back to audio track 0 — which is how a Spanish/
                        # Italian dub ended up playing as "English". Fail
                        # instead: the player re-resolves (English or nothing).
                        _remux_mark_failed(file_id)
                        try:
                            if os.path.exists(tmp_path):
                                os.unlink(tmp_path)
                        except Exception:
                            pass
                        return self._json(
                            {"error": f"no {lang} audio track — refusing foreign audio fallback"}, 502)
                    os.rename(tmp_path, cache_path)
                except Exception:
                    _remux_mark_failed(file_id)
                    try:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                    except Exception:
                        pass
                    return self._json({"error": "remux failed"}, 500)
                finally:
                    _release_slot()
        # Serve the freshly remuxed file with FULL Range support (the browser
        # needs to seek even on the first play). Reuse the same serving logic
        # as a cache hit by handling the Range header here too.
        size = os.path.getsize(cache_path)
        rng = self.headers.get("Range", "")
        if rng:
            try:
                rng = rng.replace("bytes=", "").split("-")
                start = int(rng[0]) if rng[0] else 0
                end = int(rng[1]) if len(rng) > 1 and rng[1] else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", self._acao())
                self.send_header("Vary", "Origin")
                self.end_headers()
                with open(cache_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(cache_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        return

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"RD-bridge on :{PORT} (key={'set' if RD_KEY else 'MISSING'})")
    # Purge cached links from a previous RD session (a renewal rotates them all)
    # and keep watching: a later renewal must not be able to serve 404s for 6h.
    # force=True = one /user call at boot so a renewal that happened while the
    # bridge was down is caught immediately (subprocesses reuse the disk copy).
    _cache_purge_stale_provenance(force=True)
    threading.Thread(target=_provenance_watch, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
