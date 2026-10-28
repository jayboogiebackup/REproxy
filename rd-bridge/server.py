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
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
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


# ── candidate audio-fit verdicts (persisted) ──────────────────────────
# MEASURED (2026-09-27): Mushoku Tensei S1E1 (tmdb=94664) resolved to
# "[BlackRabbit] ... [Bluray-1080p][Opus 2.0][AV1]" whose FIRST audio track is
# Japanese, so an English request needed the ffmpeg remux: TTFB 22.2s and NO
# first byte in 150s → the player reports "no stream available". A release
# whose accepted audio already IS its first track plays instantly (no remux,
# no multi-GB copy on the Pi). So the resolver prefers releases that FIT, and
# remembers per torrent what the probe decided so a later resolve of the same
# title goes straight to a fitting release:
#   "fits"/"unknown" → plays as-is        (never pay a remux for it)
#   "remux"          → right audio, not first (only ever a fallback)
#   "reject"         → no accepted audio at all (never try it again)
_cand_verdict = {}   # "info_hash|accept" -> (timestamp, plan)
_CAND_VERDICT_TTL = 21600


def _verdict_key(info_hash, accept):
    return f"{(info_hash or '').lower()}|{'+'.join(accept or ())}"


def _cand_verdict_get(c, accept):
    e = _cand_verdict.get(_verdict_key(c.get("info_hash"), accept))
    if e and time.time() - e[0] < _CAND_VERDICT_TTL:
        return e[1]
    return None


def _cand_verdict_save():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cand_verdict_cache.json")
        with open(p, "w") as f:
            json.dump({k: (v[0], v[1]) for k, v in _cand_verdict.items()}, f)
    except Exception:
        pass


def _cand_verdict_set(c, accept, plan):
    try:
        _cand_verdict[_verdict_key(c.get("info_hash"), accept)] = (time.time(), plan)
        _cand_verdict_save()
    except Exception:
        pass


def _cand_verdict_load():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cand_verdict_cache.json")
        with open(p) as f:
            for k, (ts, v) in json.load(f).items():
                _cand_verdict[k] = (ts, v)
    except Exception:
        pass


_cand_verdict_load()


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
    # `duration` arrived after this cache shipped: an entry written before it
    # has no duration field and would make the wrong-content gate blind — one
    # re-probe per file fixes that (and only once).
    if c and now - c[0] < ttl and "duration" in (c[1] or {}):
        return c[1]
    info = {"tracks": None, "safe": None, "vcodec": None, "duration": None}  # type: dict
    import subprocess as _sp
    try:
        # NO -select_streams: one read returns every stream (video + audio) and
        # the container duration, so the video codec AND the real runtime are
        # known without a second ffprobe.
        proc = _sp.run(["ffprobe", "-v", "error",
                        "-show_entries",
                        "stream=codec_type,codec_name,duration:stream_tags=language:format=duration",
                        "-of", "json", url],
                       capture_output=True, text=True, timeout=25)
        d = json.loads(proc.stdout or "{}")
        # REAL runtime of the file. This is the ONLY check that can see through
        # a mislabeled release: a torrent whose file is NAMED "Show.S08E07…" but
        # whose content is a two-hour movie (MEASURED 2026-10-11 — the exact
        # "Rick and Morty s8e7 plays a movie" report: the served .mkv was named
        # S08E07 and ran 1h59m45s). Container duration first, then the longest
        # stream duration for containers that omit it.
        try:
            _fd = (d.get("format") or {}).get("duration")
            if _fd is not None and float(_fd) > 0:
                info["duration"] = float(_fd)
        except Exception:
            pass
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
        if info["duration"] is None:
            # Fallback for containers without a format duration: the longest
            # stream duration is the runtime (a stream-level duration survives
            # in WebM/some MP4s where the format tag is absent).
            _sd = 0.0
            for st in (d.get("streams") or []):
                try:
                    _sd = max(_sd, float(st.get("duration") or 0))
                except Exception:
                    continue
            if _sd > 0:
                info["duration"] = _sd
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


# ── CONTENT IDENTITY GATE: never serve a file that is not the request ───
# MEASURED (2026-10-11, owner report "Rick and Morty s8e7 it plays a movie"):
# the release the resolver picked was a FAKE ThePirateBay upload named
# "Rick and Morty S08E07 1080p WEB H264-SuccessfulCrab" whose single .mkv was
# 1h59m45s / 1.5 GB — a two-hour MOVIE wearing an episode's filename (the
# torrent's file list also carried "Thepiratebay new Version!!.txt" and
# "Downloaded from thepirateheaven.org.txt"). EVERY gate the resolver had was
# NAME-based, so it passed cleanly and the viewer got a movie for an episode.
#
# The gates below are the only ones that can see through a lie:
#   * the file's REAL duration (ffprobe — free: it rides the same header read
#     as the audio tracks) vs TMDB's runtime for that episode/film. A 22-minute
#     episode that is actually 120 minutes long is not that episode.
#   * an exact SxxExx / 1x02 NUMBER parse: a file spelling a different episode
#     than the one asked for is the wrong episode.
#   * the requested title's own words: a file whose title words share nothing
#     with the request is a different title (an episode-title-only filename is
#     exempt — it matches the episode's own TMDB name).
#   * the torrent's file list: advertisement/installer files bundled next to a
#     video betray a spam repack, not a scene release.
# Every one of them fails OPEN: a value we cannot measure is never a reject,
# because blocking playback on an unknown is worse than trying it.
_RUNTIME_TTL = 86400
_tmdb_meta_cache = {}   # "tmdb|tv|s|e" -> (ts, {"runtime": int|None, "title": str})


def _tmdb_meta(tmdb, mtype, season=None, episode=None):
    """TMDB runtime (minutes) + episode title for this request, cached 24h.
    Best-effort: each field is None/"" when TMDB has no data (never blocks)."""
    key = f"{tmdb}|{mtype}|{season}|{episode}"
    c = _tmdb_meta_cache.get(key)
    if c and time.time() - c[0] < _RUNTIME_TTL:
        return c[1]
    meta = {"runtime": None, "title": ""}
    try:
        if mtype == "movie":
            st, body = http_get(f"https://api.themoviedb.org/3/movie/{tmdb}?api_key={TMDB_KEY}", timeout=15)
            d = json.loads(body)
            meta["title"] = d.get("title") or ""
            if d.get("runtime"):
                meta["runtime"] = int(d["runtime"])
        else:
            st, body = http_get(
                f"https://api.themoviedb.org/3/tv/{tmdb}/season/{int(season or 1)}"
                f"/episode/{int(episode or 1)}?api_key={TMDB_KEY}", timeout=15)
            d = json.loads(body)
            meta["title"] = d.get("name") or ""
            if d.get("runtime"):
                meta["runtime"] = int(d["runtime"])
            if meta["runtime"] is None:
                st, body = http_get(f"https://api.themoviedb.org/3/tv/{tmdb}?api_key={TMDB_KEY}", timeout=15)
                ert = (json.loads(body).get("episode_run_time") or [])
                if ert and ert[0]:
                    meta["runtime"] = int(ert[0])
    except Exception:
        pass
    _tmdb_meta_cache[key] = (time.time(), meta)
    return meta


def _tmdb_titles(tmdb, mtype):
    """Every title TMDB holds for the request (localised + original), cached
    24h. Empty list when unknown — the title check then simply does not run."""
    key = f"titles|{tmdb}|{mtype}"
    c = _tmdb_meta_cache.get(key)
    if c and time.time() - c[0] < _RUNTIME_TTL:
        return c[1]
    names = []
    try:
        st, body = http_get(
            f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}"
            f"/{tmdb}?api_key={TMDB_KEY}", timeout=15)
        d = json.loads(body)
        for k in ("title", "name", "original_title", "original_name"):
            if d.get(k):
                names.append(d[k])
    except Exception:
        pass
    _tmdb_meta_cache[key] = (time.time(), names)
    return names


# Duration tolerances. A file may legitimately be a double episode (2x) or a
# feature-length special, so "too long" needs real slack; "too short" only
# catches trailers/samples/partials. Both are env-tunable.
_DURATION_TOO_LONG_RATIO = float(os.environ.get("BRIDGE_DUR_TOO_LONG_RATIO", "2.6"))
_DURATION_TOO_LONG_SLACK_MIN = float(os.environ.get("BRIDGE_DUR_TOO_LONG_SLACK", "12"))
_DURATION_TOO_SHORT_RATIO = float(os.environ.get("BRIDGE_DUR_TOO_SHORT_RATIO", "0.35"))
_DURATION_MIN_RUNTIME = float(os.environ.get("BRIDGE_DUR_MIN_RUNTIME", "8"))


def file_duration(url):
    """The file's REAL runtime in seconds (probed once, cached 6h), or None."""
    info = _probe_audio(url)
    if not isinstance(info, dict):
        return None
    if "duration" not in info:
        # An entry from before the duration probe existed — re-probe once.
        try:
            _audio_probe_cache.pop(_probe_key(url), None)
        except Exception:
            pass
        info = _probe_audio(url)
    d = info.get("duration") if isinstance(info, dict) else None
    try:
        return float(d) if d and float(d) > 0 else None
    except Exception:
        return None


def duration_mismatch(url, tmdb, mtype, season=None, episode=None):
    """Reason string when the file's REAL duration cannot be the requested
    episode/film, else None (unknown duration or unknown runtime → None).

    MEASURED: a 23-minute episode (TMDB, Rick and Morty S08E07) served as a
    7185s = 120-minute file → "> 23*2.6 + 12 = 72 min" → rejected. The same
    rule catches the inverse: a 22-minute file for a 97-minute film request.
    """
    dur = file_duration(url)
    if not dur:
        return None
    exp = _tmdb_meta(tmdb, mtype, season, episode).get("runtime")
    if not exp or exp < _DURATION_MIN_RUNTIME:
        return None
    act_min = dur / 60.0
    kind = "film" if mtype == "movie" else "episode"
    if act_min > exp * _DURATION_TOO_LONG_RATIO + _DURATION_TOO_LONG_SLACK_MIN:
        return f"file runs {act_min:.0f} min but this {kind} is ~{exp} min"
    if act_min < exp * _DURATION_TOO_SHORT_RATIO:
        return f"file runs only {act_min:.0f} min but this {kind} is ~{exp} min"
    return None


_EP_PAIR_RES = (
    re.compile(r"(?<![a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})(?!\d)", re.I),
    re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)"),
)


def _ep_pairs(name):
    """Every (season, episode) pair a filename spells (S01E02 / s1e2 /
    S01.E02 / 1x02). Empty set when the name spells none."""
    low = (name or "").lower()
    out = set()
    for rx in _EP_PAIR_RES:
        for m in rx.finditer(low):
            out.add((int(m.group(1)), int(m.group(2))))
    return out


_TITLE_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "not", "are", "was",
    "but", "you", "all", "she", "his", "her", "its", "has", "had", "have",
    "who", "which", "what", "when", "where", "why", "how", "season", "series",
    "movie", "film", "part", "vol", "volume", "episode", "chapter", "captulo",
}
# Release/encoding vocab that says nothing about WHICH title a file is.
_RELEASE_TAG_TOKENS = {
    "1080p", "720p", "480p", "2160p", "1440p", "576p", "4k", "uhd", "hdr",
    "sdr", "dv", "web", "webdl", "webrip", "webhd", "bluray", "blu", "bdrip",
    "brrip", "dvdrip", "dvd", "hdtv", "remux", "repack", "proper", "internal",
    "h264", "h265", "x264", "x265", "hevc", "avc", "av1", "xvid", "divx",
    "aac", "aac2", "ac3", "eac3", "ddp", "dd", "dts", "truehd", "atmos",
    "opus", "flac", "mp3", "6ch", "2ch", "10bit", "8bit", "hq", "hd",
    "multi", "multiaudio", "dual", "dubbed", "dub", "subs", "sub", "subbed",
    "eng", "english", "jpn", "japanese", "ita", "spa", "fre", "ger", "rus",
    "complete", "extended", "uncut", "uncensored", "upscale", "ai", "amzn",
    "nf", "dsnp", "max", "hmax", "atvp", "tv", "mkv", "mp4", "avi", "mov",
    "sbs", "3d", "full", "group", "www", "org", "com", "net", "info",
}


def _title_tokens(s):
    """Significant title words in a release/file name: ≥3 chars, not a release
    tag, not a stop word, not a number, not an SxxExx/Exx tag."""
    out = set()
    for w in re.split(r"[^a-z0-9]+", (s or "").lower()):
        if len(w) < 3 or w in _TITLE_STOPWORDS or w in _RELEASE_TAG_TOKENS:
            continue
        if re.fullmatch(r"\d+", w):
            continue
        if re.fullmatch(r"s\d{1,2}(e\d{1,3})?", w) or re.fullmatch(r"e\d{1,3}", w):
            continue
        out.add(w)
    return out


def identity_mismatch(url, tmdb, mtype, season=None, episode=None, names=None):
    """Reason string when the resolved FILE demonstrably is not the requested
    title/season/episode, else None.

      * episode numbers: if the filename spells a SxxExx/1x02 marker and the
        request wants a different (season, episode) → wrong episode. For a
        movie request, ANY episode marker means it is not a film.
      * title: if the filename carries its own title words and none of them is
        a word of the requested title → different title. A filename that is
        only the EPISODE's own title (no show name) is exempt, because it
        matches the episode's TMDB name.

    Unknown/ambiguous names always pass (fail-open)."""
    fn = urllib.parse.unquote(url or "").split("/")[-1]
    low = fn.lower()
    pairs = _ep_pairs(low)
    want = (int(season), int(episode)) if (season and episode) else None
    if mtype == "movie":
        if pairs:
            ptxt = ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(pairs))
            return f"file is a TV episode ({ptxt}), not a film"
    elif want and pairs and want not in pairs:
        ptxt = ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(pairs))
        return f"file is {ptxt}, not S{want[0]:02d}E{want[1]:02d}"
    if names is None:
        names = _tmdb_titles(tmdb, mtype)
    want_toks = set()
    for n in (names or ()):
        want_toks |= _title_tokens(n)
    file_toks = _title_tokens(low)
    if want_toks and len(file_toks) >= 2 and not (file_toks & want_toks):
        ep_title = _tmdb_meta(tmdb, mtype, season, episode).get("title") or ""
        if not (file_toks & _title_tokens(ep_title)):
            return ("file is titled '" + " ".join(sorted(file_toks))[:60]
                    + "' — no word of the requested title")
    return None


def content_gate(url, tmdb, mtype, season=None, episode=None):
    """The single content-identity gate every acceptance path must clear.
    Returns a reason string when the file must NOT be served (wrong medium /
    wrong episode / wrong title / impossible runtime), else None. Never raises
    and never blocks on an unknown — a probe/TMDB failure returns None."""
    try:
        return (duration_mismatch(url, tmdb, mtype, season, episode)
                or identity_mismatch(url, tmdb, mtype, season, episode))
    except Exception:
        return None


# Spam-bundle markers. A scene/P2P release never ships an advertisement .txt,
# but a REPACK wearing a release's name does — MEASURED on the exact fake
# served for "Rick and Morty S08E07" (files "Thepiratebay new Version!!.txt"
# and "Downloaded from thepirateheaven.org.txt" beside the .mkv). Only
# unambiguous ad/installer markers count: a domain in the TORRENT NAME is a
# normal release-site tag and must never be a reason to drop a source.
_FAKE_FILE_RE = re.compile(
    r"(new version!!|downloaded from|thepirateheaven|download more|"
    r"visit our (?:site|website)|free download)\.|\.(?:url|lnk|exe|scr|bat|cmd|vbs|jar)$",
    re.I)


def fake_upload(files):
    """True when a torrent's FILE LIST betrays a spam/repack upload: an
    advertisement or installer file bundled next to the video. `files` is an
    RD info file list ([{"path": ...}]) or a list of names."""
    for f in (files or ()):
        p = f.get("path") if isinstance(f, dict) else f
        if p and _FAKE_FILE_RE.search(str(p)):
            return True
    return False


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
    "ESPANOL", "SUBTITULADO", "SUBTITULADA",
    # ── added 2026-10-11 (seen in the live resolve cache / Torrentio sets) ──
    # MEASURED misses that let foreign dubs through: "Lanterns.S01E01-E02.
    # 1080p.Ru.Ultradox" (RU) and "Breaking Bad (Integrale) VOSTFR + FRENCH
    # 720p" (French audio) were both served to an English request.
    #   RU   – Russian (the 2-letter form; "RUS" was already here)
    #   VFF/VFQ/VF/TRUEFRENCH – French-audio release tags (VOSTFR is NOT one:
    #          it means French SUBS over the original audio — still excluded)
    #   GERDUB – German dub (the glued form; "GER"/"DEU" were already here)
    #   SPANIS – common sloppy tag for a Spanish dub ("SPA" was already here)
    #   ITADUB/SPADUB/FRDUB/RUDUB/DUBLADO – glued dub tags
    # Whole-token matching only (see _foreign_only_name): a title word that
    # merely CONTAINS one of these ("Anita"→ITA, "Rush"→RU, "Italiano") is a
    # different token and is never matched. Word forms that are common English
    # title words (SPANISH/ITALIAN/FRENCH/GERMAN) stay OUT on purpose — a prior
    # false positive came from loose matching ("The Italian Job").
    "RU", "VFF", "VFQ", "VF", "TRUEFRENCH", "GERDUB", "SPANIS",
    "ITADUB", "SPADUB", "FRDUB", "RUDUB", "DUBLADO",
    # NOTE: "DUBBED" is deliberately NOT a foreign-audio token here. On its own
    # it is an ENGLISH declaration — "Mushoku.Tensei.S01E01.DUBBED.XviD-AFG" is
    # a genuine English dub release, and holding "DUBBED" as a foreign marker
    # hard-rejected exactly the releases a dub request needs (MEASURED on
    # tt13293588: that torrent was the only explicitly-DUBBED candidate and
    # _foreign_only_name buried it at rank 3 = never tried, while _ENG_DECL_
    # TOKENS already counts "DUBBED" as an English signal). It only announces
    # FOREIGN audio when the name also spells the language out — see
    # _FOREIGN_LANG_WORDS / _foreign_only_name ("...1080p.SPANISH.DUBBED").
    # NOTE: "VOSTFR" is deliberately NOT here. VOSTFR (version originale
    # sous-titrée en français) describes FRENCH SUBTITLES over the ORIGINAL
    # audio — usually English. Treating it as a foreign-audio marker rejected
    # valid English-audio releases outright. Subtitling never changes audio.
}
ENGLISH_NAME_TOKENS = {"ENG", "ENGLISH", "EN", "MULTI", "DUAL", "MULTIAUDIO",
                       "DUALAUDIO", "ENGDUB", "VOSTENG"}
# Spelled-out language words that turn a bare "DUBBED" into a FOREIGN-audio
# marker ("1080p.SPANISH.DUBBED", "LATINO.DUBBED"). Kept OUT of
# FOREIGN_NAME_TOKENS on purpose: those words are common inside English titles
# ("The Italian Job"), so they are only ever consulted next to "DUBBED", where
# the release is unambiguously announcing WHICH dub it carries.
_FOREIGN_LANG_WORDS = {"SPANISH", "CASTELLANO", "LATINO", "ESPANOL", "ITALIAN",
                       "PORTUGUESE", "PORTUGUES", "FRENCH", "GERMAN", "RUSSIAN",
                       "VFF", "VFQ", "TRUEFRENCH"}


def _foreign_only_name(name):
    """True when a release name declares foreign audio and gives no sign of
    English/multi audio. Cheap pre-filter — the real decision is the probe."""
    if not name:
        return False
    toks = set(re.split(r"[^A-Za-z0-9]+", name.upper()))
    foreign = bool(toks & FOREIGN_NAME_TOKENS)
    if not foreign and "DUBBED" in toks:
        # a bare "DUBBED" is English; only a *qualified* dub ("SPANISH.DUBBED")
        # is foreign (see _FOREIGN_LANG_WORDS)
        foreign = bool(toks & _FOREIGN_LANG_WORDS)
    if not foreign:
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


# ── streaming remux jobs (BEHIND A FLAG, OFF BY DEFAULT) ─────────────────
# The default track path is the BLOCKING remux: buffer the whole file to disk
# with `-movflags faststart`, then serve it with Content-Length + full Range.
# It is correct and seekable, but it serves ZERO bytes until the entire file
# has been copied (MEASURED 2026-10-11: +58s for a 611 MB file at 4.39 MB/s;
# a 6.8 GB episode extrapolates to ~24-26 min, and the player aborts at 120s).
#
# The streaming variant below removes that wait: ffmpeg emits a fragmented MP4
# (frag_keyframe+empty_moov, no Content-Length) and the request streams the
# GROWING temp file to the client as bytes appear (measured ~3.4s to the first
# 2 MB). A background job owns ffmpeg, so it survives a client disconnect and
# still fills the disk cache; the next play is served from the cache with full
# Range/seek support.
#
# WHY IT IS OFF BY DEFAULT: an earlier ship of exactly this code (commit
# de847df) was reverted (00ff8e4, "loads then fails") because the client sent
# the track request through the CF relay → Vercel REproxy chain, which buffers
# and strips Accept-Ranges and destroyed the live stream. The client is now
# DIRECT-FIRST (browser → https://rd.repeaks.xyz/api/rd/track), which is the
# missing half. Even so: per verification-and-testing.md §5, this path is only
# ever switched on after a REAL end-to-end play (first frame AND a seek) has
# been observed. Until `BRIDGE_STREAMING_REMUX=1` is set in the environment,
# `do_GET_track` keeps using the blocking remux — a working slow path beats a
# faster one that serves nothing.
#
# NOTE: read at CALL time, not as a module constant. The .env loader lives far
# below (near RD_KEY), so a module-level `os.environ.get` here would always see
# the default and `BRIDGE_STREAMING_REMUX=1` in .env would silently do nothing.
STREAMING_REMUX_ENV = "BRIDGE_STREAMING_REMUX"


def streaming_remux_enabled():
    """True when the flag-gated streaming remux is switched on."""
    return os.environ.get(STREAMING_REMUX_ENV, "0").strip() == "1"

_REMUX_JOBS = {}              # file_id -> {"tmp","cache","done","ok","size","proc"}
_REMUX_JOBS_GUARD = threading.Lock()
_REMUX_JOB_STALE = 3 * 3600   # sweep .tmp orphaned by a process killed mid-remux
_REMUX_STALL_BAIL = 90        # no new bytes for this long ⇒ end the response


def _remux_job_sweep(cache_dir):
    """Delete .tmp files orphaned by a killed process (service restart mid-remux)."""
    now = time.time()
    try:
        for fn in os.listdir(cache_dir):
            if fn.endswith(".tmp"):
                p = os.path.join(cache_dir, fn)
                try:
                    if now - os.path.getmtime(p) > _REMUX_JOB_STALE:
                        os.unlink(p)
                except Exception:
                    pass
    except Exception:
        pass


def _remux_job_start_or_attach(file_id, tmp_path, cache_path, raw_url, lang,
                               cache_dir):
    """Start the background remux job for this RD file id, or attach to the one
    already running. Returns (job, status): "started", "attached" (the job is
    already filling the SAME .tmp — never start a second multi-GB ffmpeg) or
    "busy" (global ffmpeg slot cap reached → the caller answers 429)."""
    with _REMUX_JOBS_GUARD:
        job = _REMUX_JOBS.get(file_id)
        if job is not None and not job["done"].is_set():
            return job, "attached"
        if not _acquire_slot():
            return None, "busy"
        job = {"tmp": tmp_path, "cache": cache_path, "done": threading.Event(),
               "ok": False, "pos": 0, "proc": None}
        _REMUX_JOBS[file_id] = job
    _remux_job_sweep(cache_dir)
    threading.Thread(target=_remux_job_run, args=(file_id, job, raw_url, lang),
                     daemon=True).start()
    return job, "started"


def _remux_job_run(file_id, job, raw_url, lang):
    """The background remux. Owns ffmpeg + the .tmp file for its whole life, so
    a client disconnect cannot abort it — the cache still fills. Writes the
    fragmented MP4 to .tmp, then `os.replace`s it to the real cache name on a
    clean exit; a failure marks the file id so the next request fails fast
    instead of re-running a doomed remux."""
    import subprocess as _sp
    proc = None
    out = None
    ok = False
    try:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", raw_url,
               "-map", "0:v:0", "-map", f"0:a:m:language:{lang}",
               "-c", "copy", "-strict", "-2",
               "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.DEVNULL)
        job["proc"] = proc
        out = open(job["tmp"], "wb")
        po = proc.stdout
        if po is None:  # stdout=PIPE, so this cannot happen — typing guard
            raise RuntimeError("ffmpeg stdout unavailable")
        while True:
            chunk = po.read(65536)
            if not chunk:
                break
            try:
                out.write(chunk)
                out.flush()
            except Exception:
                pass
            # `pos` is the reader-visible high-water mark; the bytes are already
            # flushed to the .tmp, so a streaming reader can serve them now.
            job["pos"] += len(chunk)
        rc = proc.wait(timeout=120)
        ok = (rc == 0 and job["pos"] > 0)
    except Exception:
        ok = False
    finally:
        try:
            if out:
                out.close()
        except Exception:
            pass
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        _release_slot()
        if ok:
            try:
                os.replace(job["tmp"], job["cache"])
            except Exception:
                ok = False
        if not ok:
            _remux_mark_failed(file_id)
            try:
                if os.path.exists(job["tmp"]):
                    os.unlink(job["tmp"])
            except Exception:
                pass
        job["ok"] = ok
        job["done"].set()
        with _REMUX_JOBS_GUARD:
            if _REMUX_JOBS.get(file_id) is job:
                del _REMUX_JOBS[file_id]


def _serve_streaming_remux(handler, job, cache_path, tmp_path, file_id):
    """Stream the job's growing file to the client (the streaming-remux branch).

    Traps this handles (each cost a session to learn):
      * `os.replace(tmp, cache)` MOVES the file out from under a lagging client.
        So read from `cache_path` if it exists, else `tmp_path` — and only end
        the response when the job is done AND pos has reached the FINAL size.
        Ending on "the tmp is gone" truncates a client that was behind.
      * A live stream has no known total size, so a 206 is impossible: honour
        the Range OFFSET if the job has already written past it, but answer 200
        with `Accept-Ranges: none` + `Connection: close` so the browser does not
        treat it as seekable (seeks work on the NEXT play, from the cache).
      * NEVER kill ffmpeg on client abort — the job must outlive the request.
        A broken pipe only ends this response.
      * A no-growth stall bail so a wedged RD read cannot hold the socket open.
    """
    start_at = 0
    _rm = re.match(r"\s*bytes=(\d+)-\s*$", handler.headers.get("Range", "") or "")
    if _rm:
        start_at = int(_rm.group(1))
    handler.send_response(200)
    handler.send_header("Content-Type", "video/mp4")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Accept-Ranges", "none")
    handler.send_header("Connection", "close")
    handler.send_header("Access-Control-Allow-Origin", handler._acao())
    handler.send_header("Vary", "Origin")
    handler.end_headers()
    # BaseHTTPRequestHandler here is HTTP/1.0, so a body with no Content-Length
    # is framed by connection close — that is what makes the live stream legal.
    handler.close_connection = True
    pos = start_at
    stalled_since = time.time()
    client_gone = False
    try:
        while True:
            src = cache_path if os.path.exists(cache_path) else (
                tmp_path if os.path.exists(tmp_path) else None)
            fsz = pos
            if src is not None:
                try:
                    fsz = os.path.getsize(src)
                except OSError:
                    fsz = pos
            if src is not None and fsz > pos:
                try:
                    with open(src, "rb") as f:
                        f.seek(pos)
                        data = f.read(min(1 << 20, fsz - pos))
                except OSError:
                    data = b""
                if data:
                    pos += len(data)
                    try:
                        handler.wfile.write(data)
                        handler.wfile.flush()
                    except Exception:
                        # Client went away (BrokenPipe/Reset): end THIS response,
                        # leave the background job filling the cache.
                        client_gone = True
                        break
                    stalled_since = time.time()
                    continue
            if job["done"].is_set():
                if src == cache_path:
                    try:
                        total = os.path.getsize(cache_path)
                    except OSError:
                        total = pos
                    if pos >= total:
                        break  # complete file fully delivered
                    continue
                break  # job failed / .tmp gone — end the stream honestly
            if time.time() - stalled_since > _REMUX_STALL_BAIL:
                _audit(f"track streaming remux stalled file_id={file_id}")
                break
            time.sleep(0.15)
    finally:
        if client_gone:
            _audit(f"track client left — streaming remux keeps filling "
                   f"cache file_id={file_id}")


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
# CANDIDATE SOURCES (configurable)
#
# Torrentio config segment — joined by "|" and inserted into the URL path
# before /stream/... `qualityfilter` drops cams/screeners/3D AT THE INDEXER,
# so they never even enter the candidate list (belt-and-braces on top of the
# in-process CAM rule, _filter_cam_when_hd).
#   NOTE: `language=english` is a VERIFIED NO-OP in Torrentio ("No need to
#   filter english since it's hard to predict which entries are english").
#   Do NOT rely on it — the real language gate is _audio_gate/_foreign_only_name.
# =========================================================================
TORRENTIO_BASE = os.environ.get("TORRENTIO_BASE", "https://torrentio.strem.fun").strip().rstrip("/")
TORRENTIO_CONFIG = os.environ.get(
    "TORRENTIO_CONFIG", "qualityfilter=cam,scr,threed|sort=qualitysize"
).strip().strip("/")

# MediaFusion — keyless public Stremio addon. A BARE /stream/... call returns
# {"streams":[]}: a CONFIG URL generated from its /configure page (the config
# is an encrypted token embedded in the path) is REQUIRED. Set
# MEDIAFUSION_CONFIG_URL to e.g.
#   https://mediafusion.elfhosted.com/<encrypted-token>/manifest.json
# Leave it EMPTY to skip the source (logged once, never fatal).
MEDIAFUSION_CONFIG_URL = os.environ.get("MEDIAFUSION_CONFIG_URL", "").strip()

# Torznab slot (Jackett/Prowlarr on the Pi). INERT unless BOTH are set.
TORZNAB_URL = os.environ.get("TORZNAB_URL", "").strip().rstrip("/")
TORZNAB_API_KEY = os.environ.get("TORZNAB_API_KEY", "").strip()
TORZNAB_PATH = os.environ.get(
    "TORZNAB_PATH", "/api/v2.0/indexers/all/results/torznab/api"
).strip()

# Per-resolve candidate counts by source. Diagnostic only: attached to the
# resolve result as `_sources` and surfaced solely when the caller asks
# debug=1 (stripped from every normal response). Never affects the walk.
_SRC_COUNTS = {}


def _src_count(name, n):
    _SRC_COUNTS[name] = _SRC_COUNTS.get(name, 0) + int(n or 0)


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


# The import-time purge is for the resolve SUBPROCESS (resolve_cli.py imports
# this module) and for boot: a link from a previous RD session must be dropped
# before it can be served. A TEST or TOOL that imports this module with a
# placeholder key must never inherit that side effect — with an unverifiable
# token the fingerprint is inconclusive, the "token changed" branch fires and
# the live resolve cache is wiped. RD_SKIP_CACHE_PURGE=1 opts out (set by
# _test_stream_rules.py before it imports this module).
if not os.environ.get("RD_SKIP_CACHE_PURGE"):
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


def torrentio_rd_config():
    """The Torrentio URL segment for the STREMIO setup: our own debrid key.

    `realdebrid=<key>` is what the Torrentio addon writes into its config string
    when you install it in Stremio with a Real-Debrid account. It changes the
    /stream/ response completely (MEASURED 2026-10-12, Fight Club tt0137523):

        plain Torrentio       98 streams,   0 with `url`   -> we must addMagnet
                                                             and poll RD (15-50s)
        Torrentio + realdebrid 184 streams, 184 with `url` -> each url 302s
                                                             STRAIGHT to the
                                                             debrid file

    That single hop is the whole reason Stremio feels instant: the addon has
    already done the debrid dance. Returns None when no RD key is configured.
    """
    if not RD_KEY:
        return None
    return f"{TORRENTIO_BASE}/realdebrid={RD_KEY}|{TORRENTIO_CONFIG}" if TORRENTIO_CONFIG \
        else f"{TORRENTIO_BASE}/realdebrid={RD_KEY}"


def search_torrentio_rd(imdb, mtype, season=None, episode=None):
    """Torrentio streams that ALREADY resolve to a debrid file (the Stremio RD
    setup). Each hit carries `direct_url` — the addon's resolve endpoint, which
    answers a 302 to real-debrid.com — plus `rd_cached` when the addon tagged it
    [RD+] (cached in THIS account, i.e. playable with no waiting at all).

    Shaped like the MediaFusion "streaming provider" hits on purpose: the
    resolver's direct-url pass follows the link once, runs the SAME codec /
    audio / content gates on the final file and can then serve it immediately.
    No infoHash is involved, so nothing is added to the account here.
    """
    base = torrentio_rd_config()
    if not base or not imdb:
        return []
    try:
        if mtype == "movie":
            url = f"{base}/stream/movie/{imdb}.json"
        else:
            url = f"{base}/stream/series/{imdb}:{season or 1}:{episode or 1}.json"
        st, body = http_get(url, headers={"User-Agent": UA}, timeout=25)
        if st != 200 or not body:
            return []
        streams = (json.loads(body).get("streams") or [])
        hits = []
        for s in streams:
            u = (s.get("url") or "").strip()
            if not u.lower().startswith("http"):
                continue  # an infoHash row — plain Torrentio already covers it
            bh = s.get("behaviorHints") or {}
            fname = (bh.get("filename") or "").strip()
            label = (s.get("title") or "").strip()
            desc = (s.get("description") or "").strip()
            name = fname or label or desc
            if not name:
                continue
            seeders = 0
            m = re.search(r"\U0001F464\s*(\d+)", desc) or re.search(r"\u25B6\ufe0f?\s*(\d+)", desc)
            if m:
                seeders = int(m.group(1))
            size_gb = None
            vs = bh.get("videoSize")
            if isinstance(vs, (int, float)) and vs > 0:
                size_gb = round(vs / (1024.0 ** 3), 2)
            if size_gb is None:
                size_gb = _size_gb(name) or _size_gb(desc)
            hits.append({
                "name": name[:90],
                "direct_url": u,
                "rd_cached": "[RD+]" in (s.get("name") or ""),
                "filename": fname,
                "seeders": seeders,
                "size_gb": size_gb,
                "source": "torrentio-rd",
                "quality": (s.get("name") or "")[:30],
            })
        return hits
    except Exception:
        return []


def _follow_redirect(url, timeout=12):
    """Resolve the addon's 302 to the final debrid file url, without downloading.

    Torrentio answers `HEAD` with the Location, so this is one cheap hop
    (MEASURED 2026-10-12: ~200-400ms to `*.download.real-debrid.com`). Returns ""
    when the link is uncached/expired or not a media file, which is exactly the
    cheap rejection the direct pass wants before any ffprobe runs.
    """
    cur = url
    for _ in range(4):
        try:
            req = urllib.request.Request(cur, headers={"User-Agent": UA}, method="HEAD")
            opener = urllib.request.build_opener(_NoRedirect())
            resp = opener.open(req, timeout=timeout)
            final = resp.geturl()
            return final if final.lower().startswith("http") else ""
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if not loc:
                return ""
            cur = urllib.parse.urljoin(cur, loc)
            cur_host = urllib.parse.urlparse(cur).netloc.lower()
            # Once we are on the debrid host the 302 chain is finished: this is
            # the file URL the player will stream.
            if "real-debrid.com" in cur_host:
                return cur
        except Exception:
            return ""
    return cur if cur.lower().startswith("http") else ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the first 302 so the Location can be read (and counted)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def search_torrentio(imdb, mtype, season=None, episode=None):
    """Torrentio public API — 100+ indexers, returns infoHash + fileIdx + seeders.
       The configured segment (default qualityfilter=cam,scr,threed|sort=
       qualitysize) is inserted before /stream/... so cams/screeners/3D are
       dropped AT THE INDEXER and never reach the candidate list."""
    try:
        cfg = f"{TORRENTIO_CONFIG}/" if TORRENTIO_CONFIG else ""
        if mtype == "movie":
            url = f"{TORRENTIO_BASE}/{cfg}stream/movie/{imdb}.json"
        else:
            url = f"{TORRENTIO_BASE}/{cfg}stream/series/{imdb}:{season or 1}:{episode or 1}.json"
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


# ── MediaFusion (public Stremio addon) ────────────────────────────────────
_MEDIAFUSION_WARNED = [False]


def _mediafusion_stream_base():
    """Derive the /stream base from the configured MediaFusion URL. Accepts
    either .../<encrypted-token>/manifest.json (the addon's /configure output)
    or a bare .../<encrypted-token> path. None when unconfigured."""
    u = MEDIAFUSION_CONFIG_URL
    if not u:
        return None
    u = u.split("?")[0].rstrip("/")
    if u.endswith("/manifest.json"):
        u = u[: -len("/manifest.json")]
    return u.rstrip("/") or None


def _mediafusion_sid(sid):
    """Normalise a Stremio meta id into the form MediaFusion accepts.

    MediaFusion is a Stremio addon, so it is addressed by an IMDb id
    ("tt0137523") or by an explicitly TMDB-prefixed id ("tmdb:550"). A BARE
    numeric TMDB id is NOT a valid Stremio id — the addon answers it with
    {"streams":[]}, which is why this source returned 0 candidates while it
    was being queried with the resolver's internal TMDB number.

    "tt…" and "tmdb:…" pass through untouched; 550 / "550" → "tmdb:550".
    Returns None when there is nothing usable to query with."""
    s = str(sid or "").strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("tt") or low.startswith("tmdb:"):
        return s
    if s.isdigit():
        return f"tmdb:{s}"
    return s


def _mediafusion_resolve_url(url, timeout=15):
    """Follow the addon's FIRST redirect (its debrid proxy URL → the provider's
    direct file URL) and return that, or None when it is not a usable file.

    MediaFusion configured with a debrid "streaming provider" answers with
    .../streaming_provider/<token>/<hash>, a URL that 302s to the provider's
    download link. Its failure modes (uncached torrent, expired debrid
    account) 302 to an elfhosted slate.m3u8 "Connection expired"/"Couldn't
    start" page instead — caught here on the FIRST hop, so a dead entry costs
    one cheap request instead of an ffprobe. The resolved URL carries the real
    filename, which the show/movie type check and the codec/audio gates need
    (a bare /streaming_provider/<hash> URL has no filename at all)."""
    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-1"})
        final = ""
        try:
            with opener.open(req, timeout=timeout) as r:
                final = r.geturl() or ""
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                return None
            final = e.headers.get("Location") or ""
        if not final or final == url:
            return None
        low = final.lower()
        if "slate.m3u8" in low or "slate.elfhosted.com" in low:
            return None  # provider error page, not a file
        if not re.search(r"\.(mkv|mp4|m4v|avi|webm|mov|ts|mpg|mpeg)(\?|$|#)", low):
            return None  # not a direct media file
        return final
    except Exception:
        return None


def search_mediafusion(sid, mtype, season=None, episode=None):
    """MediaFusion public Stremio addon.

    `sid` is whatever id the caller holds — an IMDb id ("tt…"), a "tmdb:…" id,
    or a bare TMDB number. It is normalised by _mediafusion_sid() BEFORE the
    request, because the addon silently returns {"streams":[]} for a bare
    numeric id.

    TWO stream shapes are supported, because which one you get depends on the
    addon config in MEDIAFUSION_CONFIG_URL:
      * RAW TORRENT (infoHash + fileIdx, or a magnet in `url`) — the exact
        shape Torrentio uses, so info_hash + file_idx is reused by the same
        Real-Debrid path;
      * STREAMING PROVIDER: the addon was configured with a debrid account, so
        it answers with a proxy `url` that redirects to the provider's file.
        There is no infoHash to add to our own RD account — the URL *is* the
        stream — so it is carried as `direct_url` and followed at try time
        (see the MediaFusion direct pass in the resolver).

    A BARE /stream/... call returns {"streams":[]} — the addon needs a config
    embedded in the path (from its /configure page). When MEDIAFUSION_CONFIG_URL
    is unset the source is skipped with ONE clear log line, never an error."""
    base = _mediafusion_stream_base()
    if not base:
        if not _MEDIAFUSION_WARNED[0]:
            _MEDIAFUSION_WARNED[0] = True
            print("[src] mediafusion: MEDIAFUSION_CONFIG_URL not set — source disabled",
                  flush=True)
        return []
    sid = _mediafusion_sid(sid)
    if not sid:
        return []
    try:
        if mtype == "movie":
            kind, meta = "movie", sid
        else:
            kind, meta = "series", f"{sid}:{season or 1}:{episode or 1}"
        st, body = http_get(f"{base}/stream/{kind}/{meta}.json", timeout=25)
        d = json.loads(body)
        streams = d.get("streams") or []
        if not streams:
            # A bare/expired token answers an empty list — not an error.
            print("[src] mediafusion: 0 streams (config token missing/expired?)", flush=True)
            return []
        hits = []
        for s in streams:
            bh = s.get("behaviorHints") or {}
            fname = bh.get("filename", "") or ""
            title = s.get("title", "") or ""
            desc = s.get("description", "") or ""
            name = fname or title or desc
            url = (s.get("url") or "").strip()
            ih = s.get("infoHash") or ""
            direct = ""
            if not ih and url.lower().startswith("magnet:"):
                mm = re.search(r"btih:([0-9a-fA-F]{32,40})", url)
                ih = mm.group(1) if mm else ""
            if not ih:
                # No hash to hand to Real-Debrid. An https url is the addon's
                # own provider proxy (playable); anything else is unusable.
                if url.lower().startswith("http"):
                    direct = url
                else:
                    continue
            # Seeders live in the description: "👤 N" (some rows use ▶️ N).
            m = (re.search(r"\U0001F464\s*(\d+)", desc)
                 or re.search(r"\u25B6\ufe0f\s*(\d+)", desc))
            seeders = int(m.group(1)) if m else 0
            size_gb = None
            vs = bh.get("videoSize")
            if isinstance(vs, (int, float)) and vs > 0:
                size_gb = round(vs / (1024.0 ** 3), 2)
            if size_gb is None:
                size_gb = _size_gb(name) or _size_gb(desc)
            hit = {"name": name[:90], "info_hash": ih, "file_idx": s.get("fileIdx"),
                   "filename": fname, "seeders": seeders, "size_gb": size_gb,
                   "source": "mediafusion",
                   "quality": (s.get("name", "") or "")[:30]}
            if direct:
                hit["direct_url"] = direct
            hits.append(hit)
        return hits
    except Exception:
        return []


# ── Torznab (Jackett/Prowlarr on the Pi) ──────────────────────────────────
def _torznab_enabled():
    return bool(TORZNAB_URL and TORZNAB_API_KEY)


def _magnet_hash(magnet):
    m = re.search(r"btih:([0-9a-fA-F]{32,40})", magnet or "")
    return m.group(1) if m else ""


def search_torznab(query, imdb=None, mtype=None, season=None, episode=None):
    """Standard Torznab XML search (Jackett/Prowlarr). ENV-driven and INERT:
    when TORZNAB_URL or TORZNAB_API_KEY is unset this returns [] immediately
    (no I/O, no logging, no delay) so an uninstalled Jackett cannot break a
    resolve. Uses t=movie/tvsearch with imdbid when an IMDb id is known, else
    a plain t=search&q=. Parses title, size, seeders, infohash/magnet."""
    if not _torznab_enabled():
        return []
    try:
        params = {"apikey": TORZNAB_API_KEY, "limit": "50"}
        if imdb and mtype == "movie":
            params["t"] = "movie"
            params["imdbid"] = imdb.lstrip("t")
        elif imdb and mtype in ("tv", "anime"):
            params["t"] = "tvsearch"
            params["imdbid"] = imdb.lstrip("t")
            if season is not None:
                params["season"] = season
            if episode is not None:
                params["ep"] = episode
        else:
            params["t"] = "search"
        if query:
            params["q"] = query
        url = f"{TORZNAB_URL}{TORZNAB_PATH}?" + urllib.parse.urlencode(params)
        st, body = http_get(url, timeout=25)
        root = ET.fromstring(body)
        hits = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            ih = ""
            seeders = 0
            magnet = ""
            for el in item.iter():
                tag = el.tag.split("}")[-1]  # strip any torznab namespace
                if tag == "attr":
                    nm = (el.get("name") or "").lower()
                    val = el.get("value") or ""
                    if nm == "infohash" and val:
                        ih = val
                    elif nm in ("seeders", "seed") and val.isdigit():
                        seeders = int(val)
                    elif nm == "magneturl" and val:
                        magnet = val
                elif tag in ("link", "enclosure") and not magnet:
                    magnet = (el.get("url") or el.text or "").strip()
            if not ih:
                ih = _magnet_hash(magnet)
            if not ih:
                g = (item.findtext("guid") or "").strip()
                if re.fullmatch(r"[0-9a-fA-F]{40}", g):
                    ih = g
            if not ih:
                continue  # RD needs a hash — cannot use this row
            size = (item.findtext("size") or "").strip()
            size_gb = int(size) / (1024.0 ** 3) if size.isdigit() else None
            hits.append({"name": title[:90], "info_hash": ih, "seeders": seeders,
                         "size_gb": size_gb, "source": "torznab",
                         "magnet": magnet, "quality": title[:30]})
        return hits
    except Exception:
        return []


# ── Concurrent source fan-out ─────────────────────────────────────────────
# Torrentio + MediaFusion + Torznab used to be fetched ONE AFTER ANOTHER, so the
# resolve paid the SUM of their latencies. MEASURED (The Office S01E01,
# 2026-10-11): Torrentio 0.37s, Torznab 0.00s, MediaFusion 15.20s (elfhosted
# cold) → 15.58s serial before the walk even started. Fetched together the wall
# clock is the SLOWEST source, not the sum.
#
# `BRIDGE_SOURCE_TIMEOUT` caps EACH source (default 12s): a hung indexer can no
# longer hold the whole resolve hostage — its late result is simply dropped and
# the sources that answered are still used. Set it higher to be more patient,
# 0 (or negative) to disable the cap.
_SOURCE_TIMEOUT = float(os.environ.get("BRIDGE_SOURCE_TIMEOUT", "12"))

# The player-feedback preview (see preview_candidates) deliberately uses a SHORT
# budget: Torrentio answers in ~0.4s, so a 3s window already yields a real
# candidate list to show while the viewer waits, without making the preview
# itself a stall. Feedback only — it never touches the resolve path.
_PREVIEW_TIMEOUT = float(os.environ.get("BRIDGE_PREVIEW_TIMEOUT", "3"))


def _fetch_sources(imdb, tmdb, mtype, season=None, episode=None, budget=None):
    """Fetch every configured candidate source CONCURRENTLY.

    Returns (torrentio_hits, mediafusion_hits, torznab_hits) in that fixed
    order (so the candidate ordering downstream is identical to the old serial
    code). A source that errors or overruns the per-source cap contributes an
    empty list — never a fabricated one — exactly like the old `except: []`.
    `budget` overrides the per-source cap in seconds (None → _SOURCE_TIMEOUT).
    """
    import concurrent.futures as _cf

    cap = _SOURCE_TIMEOUT if budget is None else budget

    def _torrentio():
        return search_torrentio(imdb, mtype, season, episode)

    def _mediafusion():
        # MediaFusion is addressed by an IMDb id ("tt…") or a "tmdb:<n>" id — a
        # BARE TMDB number silently answers {"streams":[]} (see _mediafusion_sid).
        return search_mediafusion(imdb or f"tmdb:{tmdb}", mtype, season, episode)

    def _torznab():
        return search_torznab(None, imdb=imdb, mtype=mtype, season=season, episode=episode)

    out = {"torrentio": [], "mediafusion": [], "torznab": []}
    ex = _cf.ThreadPoolExecutor(max_workers=3)
    try:
        futs = {ex.submit(_torrentio): "torrentio",
                ex.submit(_mediafusion): "mediafusion",
                ex.submit(_torznab): "torznab"}
        try:
            for fut in _cf.as_completed(futs, timeout=(cap if cap and cap > 0 else None)):
                name = futs[fut]
                try:
                    out[name] = fut.result() or []
                except Exception:
                    out[name] = []
        except _cf.TimeoutError:
            # At least one source overran the cap. Harvest whatever else already
            # finished; abandon the stragglers (their own http timeout still
            # bounds them). Never block here — that would defeat the cap.
            for fut, name in futs.items():
                if fut.done() and not fut.cancelled():
                    try:
                        out[name] = fut.result() or []
                    except Exception:
                        out[name] = []
    finally:
        # wait=False: a straggler source must NOT stall the resolve. It keeps
        # running in its worker thread until its own http timeout fires.
        ex.shutdown(wait=False)
    return out["torrentio"], out["mediafusion"], out["torznab"]


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
                    if fake_upload(info.get("files")):
                        continue  # spam/repack bundle — see _FAKE_FILE_RE
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
                    if fake_upload(info.get("files")):
                        continue  # spam/repack bundle — see _FAKE_FILE_RE
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


# ── RD provider-block signal (HTTP 451 "Unavailable For Legal Reasons") ──
# MEASURED 2026-10-11 (The Odyssey, tt33764258): EVERY fresh candidate was
# refused by RD with HTTP 451 in ~0.5s, so the walk spent its whole budget
# failing and then reported the generic "no stream available … try again in a
# few minutes" — which tells the viewer to retry something that can never
# succeed. These counters let the resolver tell "the provider blocks this
# title" apart from "nobody is seeding it (yet)".
_rd_block_stats = {"adds": 0, "n451": 0}


def _rd_block_snapshot():
    return (_rd_block_stats["adds"], _rd_block_stats["n451"])


def _is_451(exc):
    """True when an exception is an HTTP 451 from RD (infringing content the
    account is not allowed to serve)."""
    return isinstance(exc, urllib.error.HTTPError) and getattr(exc, "code", None) == 451


def _rd_add_and_stream_error(imdb, adds0, n451_0):
    """The error dict the resolver reports when the walk found nothing.

    Distinctly surfaces a provider-wide block: if EVERY fresh magnet add this
    walk attempted came back 451, the honest message is "the provider blocks
    all of these", not an invitation to retry in a few minutes."""
    adds = _rd_block_stats["adds"] - adds0
    n451 = _rd_block_stats["n451"] - n451_0
    if n451 and adds and n451 >= adds:
        return {"error": "all candidates are blocked by the provider "
                         "(real-debrid: HTTP 451 Unavailable For Legal Reasons)",
                "imdb": imdb, "blocked": True}
    return None


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
                if fake_upload(info.get("files")):
                    return None  # spam/repack bundle — see _FAKE_FILE_RE
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
    _rd_block_stats["adds"] += 1
    try:
        added = rd_post("/torrents/addMagnet", {"magnet": magnet})
    except Exception as e:
        if _is_451(e):
            # MEASURED: RD refuses the whole title this way (all candidates,
            # ~0.5s each). Recorded so the resolver can say so instead of the
            # generic "try again in a few minutes".
            _rd_block_stats["n451"] += 1
        return None
    tid = added.get("id")
    if not tid:
        return None  # 451 infringing / invalid → next candidate

    info = None
    selected = False
    eff_idx = file_idx
    uncached_polls = 0  # DEDICATED cache-miss budget (see below)
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
                # RD is fetching it on THEIR servers (Stremio-style). A torrent
                # RD ALREADY holds reports `downloaded` right after selectFiles,
                # so `downloading`/`queued` means it is NOT cached and the wait
                # is unbounded — give up fast and let the caller try the next
                # hash. Dedicated counter, NOT `attempt`: `attempt` also counts
                # the magnet-conversion / file-selection polls, so reusing it
                # (the old `if attempt >= 10: time.sleep(2)`) burned ~20s per
                # uncached candidate — 5 batches was the whole 115s resolve
                # deadline, i.e. the reported "no stream" timeouts. Budget:
                # ~3 polls × 1.2s ≈ 3.6s per uncached candidate.
                uncached_polls += 1
                if uncached_polls >= 3:
                    break
                time.sleep(1.2)
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
    # A spam/repack bundle (an advertisement or installer file sitting next to
    # the video) is not the release whose name it wears — MEASURED: the fake
    # served for "Rick and Morty S08E07" shipped "Thepiratebay new
    # Version!!.txt" + "Downloaded from thepirateheaven.org.txt". Never serve
    # it, and drop it from the account so it cannot be picked up again.
    if fake_upload(info.get("files")):
        try:
            rd_get(f"/torrents/delete/{tid}")
        except Exception:
            pass
        _audit(f"REJECT fake upload bundle (spam files) tid={tid}")
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
    """Where a release sits for the REQUESTED language.

    lang=dub (anime): an English-ONLY dub release ranks above Dual/Multi —
    dual-audio releases store the Japanese track first, so they always pay the
    slow ffmpeg remux.

    SUB (the default): the mirror image, and it used to be missing entirely —
    every candidate scored 0, so a dub-only release could lead a SUB request.
    MEASURED 2026-10-11 on Frieren S1E1 (anime sub, 111 candidates): the ranked
    head was "[Yameii] … [English Dub] [CR WEB-DL 1080p]", i.e. a viewer who
    asked for subtitles was handed an English dub — no Japanese track at all.
    A release that ANNOUNCES a dub carries the dub instead of the original
    audio, so it now sits behind every release that does not. Dual/Multi stay
    unpenalised: those still carry the original track, which is what a sub
    viewer wants."""
    n = (c.get("name", "") or "").upper()
    if lang == "dub":
        if any(x in n for x in ("DUAL", "MULTI")):
            return 1  # English present, but not as the first track
        if any(x in n for x in ("DUB", "ENGLISH", "EN.")):
            return 0  # dubbed — likely English-first: try first
        return 2
    # SUB / language-agnostic request: keep dub-only releases out of the lead.
    if re.search(r"\b(DUB|DUBBED|ENGDUB|ENGLISH\s+DUB)\b", n):
        return 1
    return 0


# Name markers that announce the FIRST audio track is NOT the accepted one.
# "DUAL"/"MULTI" only count as an AUDIO signal together with an AUDIO token,
# so a "Multi.Subs"/"MULTi." release (multi-language SUBTITLES, audio usually
# English) is not misread as dual-audio.
_MULTI_AUDIO_TOKENS = {"DUAL", "MULTI"}
_ENG_DECL_TOKENS = {"ENG", "ENGLISH", "ENGDUB", "DUBBED"}
# A BARE "ENG"/"EN" token is the weakest English signal — it appears as one of
# many language tags on multi-language releases. It only proves English-first
# when nothing in the name declares a foreign language. MEASURED 2026-10-11:
# "Obsession.(2025).WEB-DL.HDR.DV8.2160p.3xUkr.Eng" scored as
# PROVEN-English-first (rank 0) although "3xUkr" is a THREE-Ukrainian-track
# release — the bare "Eng" tag was matched and the Ukr majority ignored.
# Spelled-out declarations (ENGLISH/ENGDUB/DUBBED) stay strong on their own.
_STRONG_ENG_TOKENS = {"ENGLISH", "ENGDUB", "DUBBED"}
_JPN_DECL_TOKENS = {"JPN", "JAPANESE", "JAP"}
_NEVER_RANK = 3  # carries no accepted audio: never served, so never tried


def _audio_first_rank(c, accept):
    """Where a candidate sits on "is the accepted audio the FIRST track?":
      0 = PROVEN as-is (a probe verdict of "fits", or the name declares the
          accepted language) — the accepted audio IS track 1: no remux needed
      1 = unknown/untagged (the name proves nothing, or the probe saw only
          untagged streams) — plays as-is, but the language is UNCONFIRMED
      2 = the accepted audio is NOT first → the slow play-time ffmpeg remux
      3 = carries no accepted audio at all → never worth a try

    NOTE (2026-10-11): the probe verdict "unknown" (untagged/und streams) used
    to score 0, i.e. it tied with a PROVEN "fits". That let an untagged
    Spanish/Italian dub outrank a release with a real English first track
    inside the same resolution tier — one of the ways "movies are only
    Spanish" happened. Untagged is still ACCEPTABLE (rank 1, far above a
    remux or a reject) and still fails open, it is simply not allowed to beat
    a candidate whose English audio we actually proved.

    The PROBE verdict (persisted per torrent, see _cand_verdict) always wins: it
    is a real ffprobe of the file, while a name is only a hint. The hint itself
    is deliberately conservative — it never overrides a proven verdict and an
    unmarked name stays in the neutral tier — because the probe and the try
    loop still decide what is actually returned.

    Rank 0 ("the accepted audio is PROVEN first") therefore requires either a
    real probed verdict of "fits" (an ffprobe that saw the accepted language as
    stream[0]) or a name that declares it unambiguously. Untagged/"und" audio
    is NEVER rank 0 — untagged is exactly what the probe reports when it could
    not confirm the first track, so it stays rank 1 (acceptable, unconfirmed)
    and never counts as proven. A bare ENG token next to a declared foreign
    language is likewise not proof (see _STRONG_ENG_TOKENS).
    """
    accept = tuple(accept or ())
    if not accept:
        return 1
    v = _cand_verdict_get(c, accept)
    if v == "reject":
        return _NEVER_RANK
    if v == "fits":
        return 0  # PROVEN: the accepted language is the file's first track
    if v == "unknown":
        return 1  # probed but untagged — acceptable, unconfirmed (see NOTE)
    if v == "remux":
        return 2
    name = c.get("name", "") or ""
    if "eng" not in accept:
        return 1
    if _foreign_only_name(name):
        return _NEVER_RANK  # a declared foreign-only release can never be served
    if "jpn" in accept:
        # Japanese is accepted as well, so a Japanese-first (dual/multi) release
        # already fits — an anime request demotes nothing for being JPN-first.
        return 0
    toks = set(re.split(r"[^A-Za-z0-9]+", name.upper()))
    # Multi-language releases glue a TRACK COUNT onto the language tag
    # ("3xUkr" = three Ukrainian tracks, "2xEng"): whole-token matching sees
    # "3XUKR", which is in no token set, so the foreign majority was invisible
    # (MEASURED: "...2160p.3xUkr.Eng" → 0). Expand the "Nx<LANG>" forms first.
    xtoks = set(toks)
    for t in toks:
        m = re.match(r"^\d+X?(.+)$", t)
        if m:
            xtoks.add(m.group(1))
    multi_audio = bool(toks & _MULTI_AUDIO_TOKENS) and "AUDIO" in toks
    eng_decl = bool(xtoks & _ENG_DECL_TOKENS)
    # A bare ENG/EN next to a declared FOREIGN language does NOT prove
    # English-first — the foreign tracks are the majority (measured:
    # "...2160p.3xUkr.Eng" = three Ukrainian tracks + one English tag). Only a
    # spelled-out English declaration (ENGLISH/ENGDUB/DUBBED) survives a
    # competing foreign language token.
    if (eng_decl and not (xtoks & _STRONG_ENG_TOKENS)
            and bool(xtoks & (FOREIGN_NAME_TOKENS | _FOREIGN_LANG_WORDS))):
        return 1  # acceptable, but the language is UNCONFIRMED — not "proven"
    if eng_decl and not multi_audio:
        return 0  # "English Dub" / "[ENG]" — English is very likely track 1
    if multi_audio or (toks & _JPN_DECL_TOKENS):
        # Dual/Multi-Audio stores the original Japanese track first (that is
        # why it needs the remux); a proven "fits" verdict still outranks this.
        return 1 if eng_decl else 2
    return 1


# A "native-compatible" SURROUND release: a browser-SAFE codec (AAC/Opus/FLAC/
# Vorbis — the ones a <video> element can actually decode, see
# SAFE_AUDIO_CODECS) declared together with a surround channel tag. Applied
# only as a WITHIN-TIER tiebreak, and only as a PROMOTION (rank 0/1, never a
# demotion), so it can never push a candidate below one it already beat.
# AC-3/E-AC-3/DTS/TrueHD are deliberately NOT counted as "native" here even
# though releases label them 5.1: in THIS bridge they are browser-SILENT
# (audio_codec_is_browser_safe → False), so promoting them would only spend a
# wasted add/probe on a file the try loop immediately rejects. Within the same
# resolution the ranking therefore ends up preferring e.g. "AAC 5.1" / "Opus
# 5.1" over a plain stereo name, which is the "bad sound" fix.
_SAFE_SURROUND_RE = re.compile(
    r"(?:AAC|OPUS|FLAC|VORBIS)[\s._-]?(?:5\.1|7\.1|6CH|51CH|6CH\.)"
    r"|(?:5\.1|7\.1|6CH)[\s._-]?(?:AAC|OPUS|FLAC|VORBIS)", re.I)


def _audio_native_rank(c, accept):
    """0 = the release declares a browser-safe SURROUND track (AAC/Opus/FLAC
    + 5.1/6CH) — best "sound" that still decodes natively, no remux;
    1 = everything else (plain stereo, an unmarked name, or a surround tag on
    a browser-unsafe codec). Never demotes, so it is safe as a tiebreak."""
    name = c.get("name", "") or ""
    if not name:
        return 1
    return 0 if _SAFE_SURROUND_RE.search(name) else 1


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
#       (see _quality_rank / _candidate_order_key). A FIREFOX ask
#       (codec="h264") is the one exception: it has no HEVC decoder and 4K
#       stutters, so 1080p is its top pick for smooth playback (see
#       _quality_rank_browser).
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
    """Higher = higher fidelity. Chrome's PRIMARY ordering key (rule 2):
    cam/TS/telecine → 0 (always last), an unmarked name → 1 (it cannot prove
    it is HD, so any explicitly-HD release outranks it), otherwise the explicit
    pixel height (480…2160). Firefox (codec="h264") does NOT use this — see
    _quality_rank_browser, which is speed-first."""
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


# ── browser-aware fidelity preference ────────────────────────────────
# Chrome (codec != "h264") keeps STRICT highest-quality-first: 2160 > 1440 >
# 1080 > 720 > 480 > unmarked. Firefox (codec == "h264") is SPEED-first: it
# has no HEVC decoder and 4K playback stutters on the streaming hardware, so
# 1080p is the top pick. Everything else sorts by distance from that sweet
# spot, which puts the heavy 2160p last among explicit heights (a light 480p
# plays smoother than a 4K stream) — cam stays the floor for both browsers.
_FIREFOX_SWEET_SPOT = 1080
# Larger than any possible |height - 1080| (max 1080 for 2160p), so an
# explicit height always outranks the unmarked-name tier (1) and a cam (0).
_FIREFOX_RANK_BASE = 2000


def _quality_rank_browser(c, codec=None, want_h=None):
    """Fidelity rank used as the PRIMARY ordering key (rule 2), chosen by the
    requesting browser:

      * Chrome / unknown (codec is not "h264") → strict highest-quality-first,
        identical to _quality_rank: explicit pixel height (480…2160) wins,
        an unmarked name sits at 1 (above cam), a cam release at 0.
      * Firefox (codec == "h264") → speed-first, 1080p on top: rank decreases
        with distance from _FIREFOX_SWEET_SPOT, and a height ABOVE the sweet
        spot is penalised one extra point so the lighter release wins the tie
        (720p beats the equally-distant but heavier 1440p). Ladder:
        1080 > 720 > 1440 > 576 > 480 > 2160, with the unmarked/cam floor
        unchanged. An EXPLICIT quality request (want_h, from ?quality=1080/720/
        480) becomes the sweet spot instead of 1080, so the user's own pick is
        never outranked by the browser default.

    The cam floor is preserved in both branches so a cam can never outrank a
    genuine release no matter which browser asks (see rule 1).

    NOTE (2026-10-11): this rank is now only the FIDELITY component of the
    primary key — `_primary_sanity` runs ahead of it so a nominal 4K cannot
    outrank a browser-playable 1080p on resolution alone (see below)."""
    name = c.get("name", "")
    if _is_cam_release(name):
        return 0
    h = _release_height(name)
    # MEASURED 2026-10-11 (v1.6): the sweet-spot ladder is no longer Firefox-
    # only. Chrome used to get strict highest-quality-first, and the measured
    # result was a 2160p x265 10-bit WEBRip (Culpa Mia, 5.25 GB) leading every
    # playable 1080p for a plain movie click: the browser then has to software-
    # decode HEVC/SDR-downconvert and pull 5 GB through the relay before it can
    # show a picture, which is exactly the slow start / bad playback being
    # reported. 1080p is the practical ceiling for in-browser streaming;
    # 2160p stays reachable as an EXPLICIT ask (want_h=2160, the player's 4K
    # option) and still wins when no 1080p release exists at all.
    if h is None:
        return 1  # unmarked: cannot prove a height — still above cam only
    target = want_h or _FIREFOX_SWEET_SPOT
    overshoot = 1 if h > target else 0
    return _FIREFOX_RANK_BASE - abs(h - target) - overshoot


# ── plausibility in the PRIMARY key (not only the tail) ───────────────
# MEASURED 2026-10-11: for a plain browser ask a 4K HEVC/x265 release used to
# outrank a 1080p H.264 one, because resolution was the primary key and every
# sanity check (size, tier, codec) lived in the TAIL — where it can never
# demote a nominally-higher resolution. That is the measured cause of the
# wrong-aspect / implausible-size picks (Micro4K upscales: a "2160p" name on a
# ~2 GB file) and of heavy 4K HEVC streams a browser cannot keep up with.
# These checks now run in the PRIMARY key, ahead of the fidelity tiebreak, so
# resolution can only win among candidates that are plausible at all.
_RES_MIN_GB = {2160: 3.5, 1440: 1.8, 1080: 0.7, 720: 0.25, 576: 0.15, 480: 0.08}


def _res_size_sanity(h, gb):
    """0 = the size is plausible for the claimed height; 2 = implausibly small
    for that height (a Micro4K-style upscale / a mislabelled release);
    1 = not enough information to judge (never a demotion on its own)."""
    if h is None or gb is None:
        return 1
    mn = _RES_MIN_GB.get(h)
    if mn is None:
        return 1
    return 2 if gb < mn else 0


def _primary_sanity(c, codec, want_h):
    """Plausibility tuple for the PRIMARY ordering key (lower = better):
        (browser-codec sanity, size sanity, resolution/size sanity)
    * browser-codec sanity: for a Chrome/unknown ask a 4K (or >1080p)
      HEVC/x265 release is demoted below every H.264/AV1 release — the
      measured wrong-aspect / implausible-size class. A Firefox ask
      (codec="h264") never reaches here with HEVC: it is filtered out earlier
      (see the h264_only prefilter in the walk).
    * size sanity: _size_rank (sample / absurd size).
    * resolution/size sanity: _res_size_sanity (upscale detection).
    The cam floor still runs BEFORE this (it is the first key element), so a
    cam can never be promoted by any of these checks."""
    n = (c.get("name", "") or "").upper()
    h = _release_height(n)
    gb = _candidate_size_gb(c)
    # ── video-codec capability of the ask (measured 2026-10-11) ────────
    # A browser ask that cannot decode HEVC/AV1/VP9 must never let such a
    # release LEAD the candidate order. This demotion used to fire only for
    # HEVC names ABOVE 1080p AND only for a non-h264 ask — and _is_hevc_name
    # missed the "H 265" form entirely. MEASURED consequence: the top-14
    # candidates for MobLand S02E01 were all 2160p HEVC, and The Office S01E01's
    # #1 candidate was an x265 HEVC release, so the player was handed a stream
    # its decoder cannot render (Chromium has no hvc1 → black screen → fallback
    # handoff). Now it fires at EVERY height and for BOTH browser asks:
    #   * the default (Chromium / unknown) ask has no HEVC decoder at all;
    #   * codec="h264" declared H.264-only support (no HEVC/AV1/VP9).
    # AV1/VP9 are demoted only for the h264-only ask — Firefox/Chrome/Edge all
    # decode AV1 natively, so the default ask keeps them. This is a DEMOTION,
    # never a filter: an HEVC-only title still resolves.
    limited = _is_hevc_name(n) or (codec == "h264" and ("AV1" in n or "VP9" in n))
    codec_bad = 1 if limited else 0
    return (codec_bad, _size_rank(gb), _res_size_sanity(h, gb))



# ── COLLECTION PACKS (v1.6, MEASURED 2026-10-28) ──────────────────────────
# For an EPISODE request, a season/collection torrent is a trap: RD accepts the
# magnet (201) and then sits at status=downloading, progress=0, because it must
# fetch the WHOLE collection before it can serve one file. The resolve burns its
# deadline there and the player reports "no stream" — which is exactly what
# happened to Frieren S1E1, where the top candidates were all packs:
#   [ToonsHub] Frieren S01 E01-E14 2160p   · [SubsPlease] 01-28 Batch
#   [Erai-raws] 01 ~ 28 [BATCH]            · [wham] ... S01 (BD 1080p)
# 84 candidates came back and the walk still ended on a remux. A single-episode
# release (Sousou no Frieren - 01) resolves from cache in seconds.
_BATCH_RANGE_RE = re.compile(r"(?:s\d{1,2})?\s*e?p?\s*(\d{1,3})\s*[-~\u2013]\s*(?:s\d{1,2})?\s*e?p?\s*(\d{1,3})", re.I)
_BATCH_WORD_RE = re.compile(r"\b(batch|complete|collection|season\s*pack|all\s*episodes)\b", re.I)


def _is_collection_release(name, mtype, season=None, episode=None):
    """True when this release is a multi-episode collection, not one episode."""
    if mtype == "movie" or not name:
        return False
    n = name.lower()
    if _BATCH_WORD_RE.search(n):
        return True
    m = _BATCH_RANGE_RE.search(n)
    if m:
        try:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi > lo:          # "E01-E14", "01-28", "01 ~ 28"
                return True
        except Exception:
            pass
    return False


def _batch_penalty(c, mtype, season=None, episode=None):
    """Sort prefix: 0 for a single-episode release, 1 to walk it only after
    every one-episode candidate has been tried."""
    return 1 if _is_collection_release(c.get("name", ""), mtype, season, episode) else 0


def _candidate_order_key(c, want_h, lang, codec, accept=None):
    # Rule 1 (cam never above HD) is enforced structurally: the cam flag is the
    # FIRST key (1 = cam, so it sorts last ascending), so even a cam that
    # claims "2160p" sorts below a 480p WEB.
    # Rule 2 (the owner's rule) is QUALITY FIRST — highest fidelity wins among
    # candidates that can actually be served for THIS request ("always pick the
    # highest quality stream first then go down the list"). A FIREFOX ask is the
    # one deliberate exception (1080p-first for smooth playback); see
    # _quality_rank_browser.
    # Rule 3 (audio playability, restored 2026-10-11) bounds rule 2: a candidate
    # that cannot hand the player the requested audio AS-IS is never allowed to
    # outrank one that can. See the two branches below.
    #
    # HISTORY (do not re-break): 7971d86 demoted the audio-fit rank
    # (_audio_first_rank) to a within-quality TIEBREAK. The MEASURED consequence
    # for an ANIME DUB ask (accept=("eng",)) — its own test pinned the rule and
    # failed: "fit: 720p that plays beats 1080p that needs a remux" — was that
    # the whole candidate ladder is ordered by resolution first, and the dub
    # walk then leads with high-resolution releases whose FIRST track is
    # Japanese (fansubs / dual-audio). MEASURED on Mushoku Tensei S1E1
    # (tt13293588, 50 candidates): the only release that carries English FIRST
    # ("[Yameii-Unofficial] … [English Dub] [FUNi WEB-DL]") sorted at position
    # 36/50, far past the point the walk gives up (MAX_FITS_BATCHES=5 → ≤15
    # candidates once a remux fallback is in hand). The resolve therefore spent
    # 69s and returned the 1080p dual-audio REMUX; the player routes a dub to
    # /api/rd/track, which must pull the entire multi-GB file before it can
    # serve a byte (MEASURED: 22.2s TTFB, nothing in 150s, client timeout 120s)
    # → the viewer sees "no stream available". A SUB ask is immune (accept=("jpn",
    # "eng") → every candidate is as-is → 6s).
    accept = tuple(accept or ("eng",))
    cam = 1 if _is_cam_release(c.get("name", "")) else 0
    afr = _audio_first_rank(c, accept)
    quality = -_quality_rank_browser(c, codec, want_h)
    native = _audio_native_rank(c, accept)
    # Plausibility is a PRIMARY key element now, not a tail tiebreak: a
    # nominally-higher resolution that is implausible (4K HEVC for a plain
    # browser ask, a "2160p" Micro4K-style upscale, an absurd/small size)
    # must lose to a plausible lower-resolution release. See _primary_sanity.
    plaus = _primary_sanity(c, codec, want_h)
    # DETERMINISM (measured 2026-10-11): the candidate set carried 182
    # candidates but only 170 distinct keys — 11 groups tied on everything,
    # and their order was decided by SOURCE (fetch arrival) order, so the same
    # request could return a different release run to run. `info_hash` is a
    # stable, unique identifier, so it is the FINAL tiebreak: identical keys
    # can no longer exist, and the picked release is reproducible. (Seeders
    # still lead the popularity axis and can vary live — that is the intended
    # popularity-first behaviour; this only removes the arbitrary tie.)
    tail = (_lang_rank(c, lang),
            _candidate_tier(c, codec, want_h),
            _size_rank(_candidate_size_gb(c)),
            -int(c.get("seeders") or 0),
            codec_rank(c.get("name", ""), want_h),
            (c.get("info_hash") or "").lower())
    if (lang or "").lower() == "dub":
        # ANIME DUB: the requested audio is a NON-DEFAULT alternate track, so
        # "which release can hand the player English first, without the remux?"
        # is the dominant axis, ahead of resolution — an untagged 1080p on a dub
        # ask is almost always Japanese audio that either fails the probe or
        # needs the stall-prone remux, while the one release that PROVES
        # English-first plays instantly. Ladder: 0 (proven/declared English
        # first) → 1 (untagged, unconfirmed) → 2 (needs the remux) → 3 (carries
        # no accepted audio). Quality still decides INSIDE each class, so the
        # owner's rule holds among candidates that can serve the dub at all.
        return (cam, afr) + plaus + (quality, native) + tail
    # MOVIE / TV / ANIME SUB: the requested language is the medium's default
    # (or JPN is accepted too), so quality stays primary — but a candidate that
    # cannot play as-is (needs the blocking remux, rank 2, or carries no
    # accepted audio at all, rank 3) is gated BELOW every candidate that can.
    # That is a correctness bound on rule 2, never a quality preference: it
    # merely stops an unusable-for-this-request release from leading the walk.
    return (cam, 0 if afr <= 1 else 1, plaus, quality, afr, native) + tail


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
    # A file that SPELLS a different episode than the one requested is the
    # wrong episode — the episode NUMBERS must agree, not merely exist.
    if season and episode:
        pairs = _ep_pairs(urllib.parse.unquote((url.split("/")[-1] or "")))
        if pairs and (int(season), int(episode)) not in pairs:
            return False
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
                   nocache=False, dead=None, rid=None):
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
        elif cfn and content_gate(cfn, tmdb, mtype, season, episode):
            # A cached link that is not the requested CONTENT — a mislabeled
            # release cached before the identity gate existed (the "S08E07"
            # file that was really a two-hour movie) — must never be served
            # again. Fall through and re-walk instead.
            _audit("REJECT cached wrong content: " + _url_name(cfn)[:60])
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
            # A cached entry whose audio has to be REMUXED is the one result we
            # never trust blindly — the remux is exactly what times out (see
            # _audio_first_rank). Walk ONCE for a release that plays as-is and
            # keep this cached link as the fallback. `fits_checked` records that
            # that walk already happened, so a title whose only releases need a
            # remux pays for the search once, not on every single play.
            if cached.get("needs_remux") and not cached.get("fits_checked"):
                soft_cached = cached
            else:
                return cached
        # else: the cached entry is a foreign-audio release (written before
        # the audio-language check) → fall through and re-resolve.
    result = _resolve_stream_impl(tmdb, mtype, season, episode, quality, skip_account, codec, lang, rid=rid)
    if soft_cached is not None and not (isinstance(result, dict) and result.get("url")):
        # Nothing playable exists for this title — a cached HEVC / remux stream
        # beats "no stream" (the player's decode-error retry handles it).
        result = soft_cached
    url = result.get("url") if isinstance(result, dict) else None
    if url:
        # FINAL IDENTITY BOUNDARY: what leaves this function must be what was
        # asked for. Type (show vs movie) first, then CONTENT — duration,
        # episode numbers, title — so that no path (account library, Torrentio,
        # MediaFusion, Torznab, APIBay/EZTV tail, or a soft/cached fallback) can
        # hand the player a file that is not the request.
        bad = None
        if not url_matches_type(url, mtype, season, episode):
            bad = "wrong type (show/movie mismatch)"
        if not bad:
            bad = content_gate(url, tmdb, mtype, season, episode)
        if bad:
            _audit(f"REJECT at boundary: {bad} :: {_url_name(url)[:60]}")
            result = {"error": f"resolved link does not match the request ({bad})",
                      "imdb": (result or {}).get("imdb")}
    # Cache ONLY a real, verified URL: `url` above may name a link the identity
    # boundary just rejected, and caching that error would make the title
    # unplayable for 6h (every play would re-read the cached error).
    if isinstance(result, dict) and result.get("url"):
        _tag_audio(result, accept)
        _tag_cam(result)
        if soft_cached is not None and result.get("needs_remux"):
            # The walk just searched for a release that plays as-is and found
            # none: remember that, so the next play serves this remux link
            # straight from cache instead of re-walking the account.
            result["fits_checked"] = True
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


def _tag_cam(result):
    """Attach the CAM verdict to a resolve result so the player can WARN the
    viewer: `cam: True` marks a stream whose selected release is a cam /
    telesync / telecine — a recording made inside a cinema, the lowest
    fidelity tier there is (blurry picture, audience noise).

    This can only ever be set when no HD alternative existed: _filter_cam_when_hd
    drops every cam release the moment the candidate set holds one genuine
    720p+ release, so a cam that survives to selection means it is genuinely
    the only thing this title has right now. The stream is still returned and
    still plays — the flag is honesty for the viewer, never a block.

    Verdict source: the release name when the resolver knows it (Torrentio /
    MediaFusion / APIBay / EZTV candidates all carry it), otherwise the real
    filename inside the RD URL — the account-library path returns a title-only
    match, so the stored file's own name is the only truthful signal there.
    """
    if not result.get("url"):
        return result
    if _is_cam_release(result.get("title") or "") or _is_cam_release(_url_name(result["url"])):
        result["cam"] = True
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
    Edge all decode AV1 natively, so AV1 files are playable.

    Separator-INSENSITIVE (measured 2026-10-11): releases spell the codec
    "H265", "H.265", "H 265", "H-265", "H_265", "X265" and "HEVC". Matching on
    the glued token alone MISSED every spaced/punctuated form — and those are
    extremely common on 2160p releases ("...DoVi HDR H 265-playWEB"). HEVC then
    went undetected: the "h264" prefilter kept it, _primary_sanity did not
    demote it, and it led the candidate order to a browser that cannot decode
    it. Strip every non-alphanumeric character and upper-case before matching so
    all separator variants collapse onto the same glued token."""
    n = re.sub(r"[^A-Za-z0-9]", "", name or "").upper()
    return any(x in n for x in ("HEVC", "X265", "H265"))


def _resolve_stream_impl(tmdb, mtype, season=None, episode=None, quality=None, skip_account=False, codec=None, lang=None, rid=None):
    """Wrapper around the real walk. Resets the per-source diagnostic counter,
    then attaches the resulting `_sources` counts to the result dict. Purely
    informational — the walk and its behaviour are unchanged."""
    _SRC_COUNTS.clear()
    result = _resolve_stream_impl_inner(tmdb, mtype, season, episode, quality,
                                        skip_account, codec, lang, rid=rid)
    if isinstance(result, dict):
        result["_sources"] = dict(_SRC_COUNTS)
    return result


def _resolve_stream_impl_inner(tmdb, mtype, season=None, episode=None, quality=None, skip_account=False, codec=None, lang=None, rid=None):
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
    # Snapshot the RD provider-block counters BEFORE any magnet add, so the
    # failure paths can tell "the provider blocks every candidate (HTTP 451)"
    # apart from "nothing resolved (uncached / no HD / all rejected)".
    _block0 = _rd_block_snapshot()
    _progress_start(rid)

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

    # Requested resolution bias (shared by the account walk, the Torrentio-RD
    # pass, the candidate ordering and the APIBay/EZTV tail sort).
    # 2160 is an EXPLICIT 4K ask (the player's 4K option). Without it the ladder
    # targets 1080 — see _quality_rank_browser.
    want_h = (2160 if quality == "2160" else 1080 if quality == "1080"
              else 720 if quality == "720" else 480 if quality == "480" else None)

    # 0. THE ACCOUNT LIBRARY WALK RUNS FIRST. ORDERING IS A LATENCY
    #    DECISION, NOT A PREFERENCE (MEASURED 2026-10-11): the account walk
    #    costs ~1-5s and frequently HITS, while the indexer fan-out below can
    #    cost 15s+ on its own (MediaFusion cold = 15.20s). Running the
    #    indexers first made every library hit pay for them too — the visible
    #    "stuck on loading repokapone for shows" stall. MEASURED for
    #    The Office S01E01: account-hit resolve 4.3s vs 18.4s when the
    #    account is skipped. The fan-out is now paid ONLY when the account
    #    has nothing usable, which is exactly where it is needed.
    # 0. The user's OWN RD library (Harbor-style, instant) — already-
    #    downloaded content always works, no 451, no waiting. Only check the
    #    PRIMARY titles. skip_account=True bypasses this so Torrentio
    #    candidates with browser-playable audio get picked instead.
    #
    #    BUT: an account file that needs the play-time REMUX is not "instant"
    #    either — that remux has to pull the whole multi-GB file before it can
    #    serve a single byte (MEASURED: 22.2s to first byte and nothing in
    #    150s → the player reports "no stream available"). So such a file is
    #    kept as a FALLBACK and the candidate walk below still gets to look for
    #    a release that plays as-is (see _audio_first_rank).
    account_remux = None
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
                # CONTENT IDENTITY: the account walk is title-matched only — it
                # never checked WHICH episode/film the stored file is, so a
                # mislabeled movie (or another episode) sitting in the library
                # passed. Checked before the remux bookkeeping so a wrong file
                # is never even held as a fallback.
                bad = content_gate(url, tmdb, mtype, season, episode)
                if bad:
                    _audit(f"REJECT account wrong content: {bad} :: {_url_name(url)[:60]}")
                    continue
                if audio_plan(audio_tracks(url), accept) == "remux" and account_remux is None:
                    account_remux = (url, {"name": f"{q} (from account)"})
                    continue  # keep looking for a copy that needs no remux
                # RESOLUTION CEILING (v1.6, MEASURED): the account walk used to
                # return whatever the library happened to hold, whatever its
                # resolution. For Culpa Mia the library holds ONE copy —
                # "…[2160p] [4K] [WEB] [5.1] [YTS.MX]" — so a plain movie click
                # was served a 5.25 GB x265 10-bit file while the indexers right
                # below had a 1080p WEB-DL of the same film: software HEVC
                # decode plus a multi-GB pull through the relay before the first
                # frame. A library file ABOVE the requested/default ceiling is
                # now treated exactly like a remux-needing one — held as the
                # fallback, so the candidate walk gets to find a playable 1080p
                # first, and the 4K copy is still used when nothing else exists.
                _ah = _release_height(_url_name(url))
                _atarget = (2160 if quality == "2160" else 1080 if quality == "1080"
                            else 720 if quality == "720" else 480 if quality == "480"
                            else _FIREFOX_SWEET_SPOT)
                if _ah and _ah > _atarget and account_remux is None:
                    account_remux = (url, {"name": f"{q} (from account)"})
                    continue  # keep looking for a copy at/below the ceiling
                return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
        except Exception:
            pass
    # 0b. TORRENTIO-RD — the Stremio setup, and now the fastest path we have.
    #     The addon is queried WITH our debrid key, so every stream it returns
    #     already carries a url that 302s straight to the debrid file: no
    #     addMagnet, no selectFiles, no polling loop. MEASURED 2026-10-12 on
    #     Fight Club: plain Torrentio 98 streams / 0 urls vs Torrentio-RD 184
    #     streams / 184 urls, and the whole hop to `download.real-debrid.com`
    #     costs ~0.2-0.4s.
    #     It runs AFTER the account walk (a library hit is still the cheapest
    #     possible answer) and BEFORE the indexer fan-out, because a hit here
    #     skips that 15s+ cold fan-out entirely. [RD+] rows are cached in THIS
    #     account and are tried first; each candidate still goes through the
    #     same cam / codec / audio-language / content-identity gates, so a
    #     foreign-only release can never win just for being cached.
    if torrentio_rd_config():
        try:
            rd_hits = search_torrentio_rd(imdb, mtype, season, episode)
        except Exception:
            rd_hits = []
        if rd_hits:
            _src_count("torrentio-rd", len(rd_hits))
            _progress_set(rid, phase="torrentio-rd", total=len(rd_hits))
            rd_remux = None
            rd_deadline = time.time() + 12
            rd_order = sorted(
                rd_hits,
                key=lambda c: (0 if c.get("rd_cached") else 1,
                               _candidate_order_key(c, want_h, lang, codec, accept)),
            )
            for c in rd_order[:6]:
                if time.time() > rd_deadline:
                    break
                u = _follow_redirect(c.get("direct_url", ""))
                if not u:
                    continue  # uncached / expired link: rejected before any probe
                if _is_dead(u):
                    continue
                if _is_cam_release(c.get("name", "")):
                    continue
                if codec == "h264" and not firefox_video_ok(u, c.get("name")):
                    _keep_hevc(u, c.get("name", ""))
                    continue
                if audio_codec_is_browser_safe(u, filename_hint=c.get("filename") or c.get("name")) is False:
                    _cand_verdict_set(c, accept, "reject")
                    continue
                if _foreign_only_name(c.get("name", "")):
                    _cand_verdict_set(c, accept, "reject")
                    continue
                plan = audio_plan(audio_tracks(u), accept)
                _cand_verdict_set(c, accept, plan)
                if plan == "reject":
                    continue
                bad = content_gate(u, tmdb, mtype, season, episode)
                if bad:
                    _audit(f"REJECT torrentio-rd wrong content: {bad} :: {_url_name(u)[:60]}")
                    _cand_verdict_set(c, accept, "reject")
                    continue
                if plan == "remux":
                    if rd_remux is None:
                        rd_remux = (u, c)
                    continue
                _progress_tried(rid, c.get("name", ""), "playing", "torrentio-rd cached")
                _audit(f"torrentio-rd hit cached={bool(c.get('rd_cached'))} {_url_name(u)[:60]}")
                return {"url": u, "source": "torrentio-rd",
                        "title": c.get("name", "")[:80], "imdb": imdb}
            if rd_remux is not None and account_remux is None:
                # Better than nothing, but still only a fallback: the indexer
                # walk below may find a copy that plays as-is.
                account_remux = rd_remux

    # 1. Indexer candidates — fetched CONCURRENTLY (see _fetch_sources), in
    #    front of the same cam/quality/audio gates: Torrentio (100+ indexers),
    #    MediaFusion (Stremio addon: infoHash+fileIdx OR a provider
    #    direct_url) and Torznab (Jackett on the Pi, INERT unless TORZNAB_*
    #    are set). Concurrent because serially the resolve paid the SUM of
    #    their latencies (MEASURED: MediaFusion 15.20s cold + Torrentio
    #    0.37s = 15.58s) whereas together it pays only the slowest; and
    #    _fetch_sources caps each source so one hung indexer can no longer
    #    hold the whole resolve hostage.
    candidates = []
    tor_hits, mf_hits, tz_hits = _fetch_sources(imdb, tmdb, mtype, season, episode)
    _src_count("torrentio", len(tor_hits))
    candidates.extend(tor_hits)
    _src_count("mediafusion", len(mf_hits))
    # Two shapes come back, and only ONE can go through the RD candidate
    # walk: torrent hits (info_hash → RD add, exactly like Torrentio) and
    # "streaming provider" hits, which are already-resolved proxy URLs with
    # no hash to add. The latter are followed + probed in the direct pass
    # below.
    mf_direct = [h for h in mf_hits if h.get("direct_url")]
    candidates.extend(h for h in mf_hits if not h.get("direct_url"))
    _src_count("torznab", len(tz_hits))
    candidates.extend(tz_hits)

    order = []
    tried = 0
    deadline = time.time() + 115
    # HARD RULE 1: never walk a CAM/TS/telecine release while an HD (720p+)
    # alternative is on the table. Drop cams here so the h264 pre-filter, the
    # ordering and the try-loop all see the same HD-only set.
    candidates = _filter_cam_when_hd(candidates)

    # 1b. MediaFusion "streaming provider" streams. The addon is configured with
    #     a debrid account, so instead of a torrent hash it returns a proxy URL
    #     that already points at the file — there is no RD add to make, the URL
    #     IS the stream. It is followed (see _mediafusion_resolve_url) and then
    #     probed with the SAME codec/audio gates as the account walk, ordered by
    #     the same popularity/quality key as the RD candidates and BOUNDED, so a
    #     run of uncached/expired entries cannot stall a resolve: a dead one is
    #     rejected on the first cheap hop, before any ffprobe. A release needing
    #     the play-time remux is only ever remembered, never selected over one
    #     that plays as-is (mf_remux, used as a last-resort fallback below).
    mf_remux = None
    if mf_direct:
        mf_deadline = time.time() + 20
        mf_order = sorted(mf_direct,
                          key=lambda c: _candidate_order_key(c, want_h, lang, codec, accept))
        for c in mf_order[:5]:
            if time.time() > mf_deadline:
                break
            u = _mediafusion_resolve_url(c["direct_url"])
            if not u:
                continue  # uncached / expired account / not a media file
            if _is_dead(u):
                continue
            if codec == "h264" and not firefox_video_ok(u, c.get("name")):
                _keep_hevc(u, c.get("name", ""))
                continue
            if audio_codec_is_browser_safe(u) is False:
                _cand_verdict_set(c, accept, "reject")
                continue
            if _foreign_only_name(c.get("name", "")):
                _cand_verdict_set(c, accept, "reject")
                continue
            plan = audio_plan(audio_tracks(u), accept)
            _cand_verdict_set(c, accept, plan)
            if plan == "reject":
                continue
            # CONTENT IDENTITY: a MediaFusion "streaming provider" URL is the
            # file itself and used to be returned with NO episode/title check
            # at all — one more way the wrong content could reach the player.
            bad = content_gate(u, tmdb, mtype, season, episode)
            if bad:
                _audit(f"REJECT mediafusion wrong content: {bad} :: {_url_name(u)[:60]}")
                _cand_verdict_set(c, accept, "reject")
                continue
            if plan == "remux":
                if mf_remux is None:
                    mf_remux = (u, c)
                continue
            return {"url": u, "source": "mediafusion",
                    "title": c.get("name", "")[:80], "imdb": imdb}

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
                       key=lambda c: (_batch_penalty(c, mtype, season, episode),
                                      _candidate_order_key(c, want_h, lang, codec, accept)))
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
                    _cand_verdict_set(c, accept, "reject")
                    return None  # silent codec — skip
                # Audio language: reject a foreign dub outright; report how
                # the file has to be played ("fits" = play it as-is) and REMEMBER
                # the verdict so a later resolve of this title can go straight
                # to a release that needs no remux (see _audio_first_rank).
                if _foreign_only_name(c.get("name", "")):
                    _cand_verdict_set(c, accept, "reject")
                    return None
                plan = audio_plan(audio_tracks(url), accept)
                _cand_verdict_set(c, accept, plan)
                if plan == "reject":
                    return None
                # CONTENT IDENTITY: a name-matching release can still BE
                # something else (the fake "Rick and Morty S08E07 …" that was a
                # two-hour movie). Reject on measured evidence — runtime,
                # episode numbers, title — before this candidate is ever
                # selected. See the CONTENT IDENTITY GATE section.
                bad = content_gate(url, tmdb, mtype, season, episode)
                if bad:
                    _audit(f"REJECT candidate wrong content: {bad} :: {_url_name(url)[:60]}")
                    _cand_verdict_set(c, accept, "reject")
                    return None
                return url, c, plan
            except Exception:
                return None

        BATCH = 3
        # A candidate whose accepted audio is not its FIRST track needs the
        # blocking play-time remux — and that remux is what times out
        # (MEASURED: 22.2s to first byte, nothing in 150s). So a remux result is
        # never *selected* while an untried candidate might still play as-is: it
        # is only ever the FALLBACK. The extra search is bounded twice over —
        # by the resolve deadline above and by MAX_FITS_BATCHES once a fallback
        # exists — so the walk can never turn into a long stall, and the
        # fallback is always returned if no fitting release turns up.
        MAX_FITS_BATCHES = 5       # ≤15 candidates spent looking for a "fits"
        FITS_SEARCH_SECONDS = 40   # …and never longer than this
        # Pre-seeded with the account's own copy when that copy needs the remux:
        # it is still better than "no stream", and having it in hand is exactly
        # what makes the extra search below cheap and bounded.
        fallback = account_remux  # (url, candidate) needing a remux
        fits_deadline = time.time() + FITS_SEARCH_SECONDS
        for bi, start in enumerate(range(0, len(order), BATCH)):
            if time.time() > deadline:
                break
            # No fallback yet → keep walking (returning nothing is worse).
            # Fallback in hand → give the remaining candidates a bounded look.
            if fallback is not None and (bi + 1 > MAX_FITS_BATCHES
                                         or time.time() > fits_deadline):
                break
            batch = order[start:start + BATCH]
            # Progress: the batch currently in flight is what "trying" means —
            # the walk is a small fan-out, not a serial scan, so the screen shows
            # the group being attempted and then each verdict as it lands.
            _progress_set(rid, phase="trying",
                          trying=[(c.get("name") or "")[:70] for c in batch])
            with _cf.ThreadPoolExecutor(max_workers=BATCH) as ex:
                results = list(ex.map(_try_one, batch))
            for c, res in zip(batch, results):
                    tried += 1
                    if not res:
                        _progress_tried(rid, c.get("name", ""), "no-stream")
                        continue
                    url, c, plan = res
                    if not url:
                        _progress_tried(rid, c.get("name", ""), "no-stream")
                        continue
                    _progress_tried(rid, c.get("name", ""),
                                    "remux" if plan == "remux" else "playing")
                    if plan != "remux":
                        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
                    if fallback is None:
                        fallback = (url, c)
        if fallback is not None:
            # No as-is release found — play the right audio via the remux
            # (cached after the first time) rather than a foreign dub.
            url, c = fallback
            return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
        # Second pass: try the best-seeded candidates (may include cached HEVC
        # that browsers with hardware decode CAN play). Same playability rule as
        # the main walk: a release that plays as-is wins, a remux is kept only
        # as the fallback for when nothing fits.
        if time.time() < deadline:
            byseed = sorted(candidates, key=lambda c: -int(c.get("seeders") or 0))
            remux_fb = None
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
                            _cand_verdict_set(c, accept, "reject")
                            continue
                        if _foreign_only_name(c.get("name", "")):
                            _cand_verdict_set(c, accept, "reject")
                            continue
                        plan = audio_plan(audio_tracks(url), accept)
                        _cand_verdict_set(c, accept, plan)
                        if plan == "reject":
                            continue
                        # Same content-identity gate as the main walk.
                        bad = content_gate(url, tmdb, mtype, season, episode)
                        if bad:
                            _audit(f"REJECT candidate wrong content: {bad} :: {_url_name(url)[:60]}")
                            _cand_verdict_set(c, accept, "reject")
                            continue
                        if plan == "remux":
                            if remux_fb is None:
                                remux_fb = (url, c)
                            continue
                        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
                except Exception:
                    continue
            if remux_fb is not None:
                url, c = remux_fb
                return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
    # All candidates tried and none streamed — report honestly (or serve the
    # account's own copy / the remembered HEVC if that is all this title has),
    # skip the slow APIBay/EZTV tail (search_eztv can take 30s+).
    if not candidates or tried > 0:
        if account_remux is not None:
            url, c = account_remux
            return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
        # A MediaFusion stream whose accepted audio needs the play-time remux:
        # still better than "no stream" (the player's remux/decode-error path
        # handles it), exactly like account_remux above.
        if mf_remux is not None:
            url, c = mf_remux
            return {"url": url, "source": "mediafusion", "title": c.get("name", "")[:80], "imdb": imdb}
        # Provider-wide block (HTTP 451) is reported DISTINCTLY — "try again in
        # a few minutes" is actively misleading when RD refuses every candidate.
        return (_rd_add_and_stream_error(imdb, *_block0)
                or _soft_hevc_result()
                or {"error": "no stream available on real-debrid yet (try again in a few minutes)",
                    "imdb": imdb})

    # Remaining sources (APIBay / EZTV)

    if mtype == "movie":
        # APIBay fallback by title + year (filter by imdb id after)
        st, body = http_get(f"https://api.themoviedb.org/3/movie/{tmdb}?api_key={TMDB_KEY}", timeout=15)
        tdata = json.loads(body)
        title = tdata.get("title", "")
        year = (tdata.get("release_date") or "")[:4]
        hits = search_apibay(f"{title} {year}", category="0")
        imdb_l = imdb.lower()
        imdb_matches = [h for h in hits if h.get("imdb", "").lower() == imdb_l]
        _src_count("apibay", len(imdb_matches))
        candidates.extend(imdb_matches)
    else:
        # EZTV by IMDb id (per-episode match) + APIBay fallback
        ez_hits = search_eztv(imdb, season, episode)
        _src_count("eztv", len(ez_hits))
        candidates.extend(ez_hits)
        st, body = http_get(f"https://api.themoviedb.org/3/tv/{tmdb}?api_key={TMDB_KEY}", timeout=15)
        tdata = json.loads(body)
        q = f"{tdata.get('name','')} S{int(season or 1):02d}E{int(episode or 1):02d}"
        ab_hits = search_apibay(q, category="205")
        _src_count("apibay", len(ab_hits))
        candidates.extend(ab_hits)

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
                    # Same content-identity gate (this niche-title account walk
                    # is title-matched only, like the main one).
                    bad = content_gate(url, tmdb, mtype, season, episode)
                    if bad:
                        _audit(f"REJECT account wrong content: {bad} :: {_url_name(url)[:60]}")
                        continue
                    return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
        except Exception:
            pass
        return (_soft_hevc_result()
                or ({"url": account_remux[0], "source": "realdebrid",
                     "title": account_remux[1].get("name", "")[:80], "imdb": imdb}
                    if account_remux else None)
                or {"error": "no torrents found", "imdb": imdb})

    # NOTE: RD's instantAvailability endpoint is currently disabled (error 37),
    # so we try torrents directly — best-seeded first. RD downloads
    # non-cached torrents on their servers; cached ones are instant.
    # Torrentio-style: try EVERY candidate — RD 451-skips silently and
    # cached/unflagged hashes resolve instantly. Also wait longer for RD to
    # fetch non-cached torrents (RD does the downloading, not us).
    # Same popularity-first, sanity-filtered ordering as the main walk.
    order = sorted(candidates,
                   key=lambda c: (_batch_penalty(c, mtype, season, episode),
                                  _candidate_order_key(c, want_h, lang, codec, accept)))
    tried = 0
    remux_fb = None
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
                if _foreign_only_name(c.get("name", "")):
                    _cand_verdict_set(c, accept, "reject")
                    continue
                plan = audio_plan(audio_tracks(url), accept)
                _cand_verdict_set(c, accept, plan)
                if plan == "reject":
                    continue
                # Same content-identity gate as every other acceptance path.
                bad = content_gate(url, tmdb, mtype, season, episode)
                if bad:
                    _audit(f"REJECT candidate wrong content: {bad} :: {_url_name(url)[:60]}")
                    _cand_verdict_set(c, accept, "reject")
                    continue
                # A release that plays as-is wins; a remux (right audio, not
                # first) is only the fallback — see _audio_first_rank.
                if plan == "remux":
                    if remux_fb is None:
                        remux_fb = (url, c)
                    continue
                return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
        except Exception:
            continue  # 451 / invalid — next candidate
    if remux_fb is not None:
        url, c = remux_fb
        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
    if account_remux is not None:
        url, c = account_remux
        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
    return (_rd_add_and_stream_error(imdb, *_block0)
            or _soft_hevc_result()
            or {"error": "could not resolve via real-debrid (all candidates rejected)", "imdb": imdb})


# ── Player feedback: candidate preview ─────────────────────────────────────
# The resolve above can take several seconds (account walk) or tens of seconds
# (indexer fan-out + candidate walk) on a cold title, and the player used to
# show nothing but "Loading repokapone…" for that whole time. These two helpers
# let the player render WHAT IS BEING TRIED as soon as the first results arrive.
def _account_library_matches(tmdb, mtype, season=None, episode=None):
    """Fast, unrestrict-free account scan: filenames ALREADY in the RD library
    that match this title by word boundaries. FEEDBACK ONLY — it deliberately
    skips the per-file probes, gates and unrestrict that the resolve's own
    account walk does, so it must never be treated as a resolve decision."""
    try:
        st, body = http_get(
            f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}/{tmdb}?api_key={TMDB_KEY}",
            timeout=10)
        tdata = json.loads(body)
    except Exception:
        return []
    words = set()
    for k in ("title", "name", "original_title", "original_name"):
        v = tdata.get(k)
        if v:
            for w in str(v).lower().split():
                if len(w) > 2:
                    words.add(w)
    if not words:
        return []
    hits = []
    try:
        for t in rd_get_torrents():
            if t.get("status") != "downloaded" or not t.get("links"):
                continue
            fn = t.get("filename") or ""
            low = fn.lower()
            if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) for w in words):
                hits.append(fn[:90])
    except Exception:
        pass
    return hits


# ── resolve PROGRESS (what the player's torrent screen shows) ─────────────
# The player sends a `rid` (one per load) and polls /api/rd/progress, so the
# loading screen can show the REAL walk — which releases are in flight and what
# happened to the ones already tried — instead of an invented animation.
#
# FILE-BACKED, not in-memory: /api/rd/stream runs the resolve in a SUBPROCESS
# (resolve_cli.py, so a hung RD call can never deadlock the server), which means
# the process holding the request is not the one doing the walking. A tiny JSON
# record per rid under the system temp dir is visible to both, costs one small
# write per candidate, and needs no IPC.
PROGRESS_DIR = os.path.join(tempfile.gettempdir(), "repeaks-progress")
_PROGRESS_TTL_S = 120


def _progress_path(rid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", rid or "")[:64]
    return os.path.join(PROGRESS_DIR, f"{safe}.json") if safe else None


def _progress_write(rid, rec):
    path = _progress_path(rid)
    if not path:
        return
    try:
        os.makedirs(PROGRESS_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)   # atomic: a poller never sees a half-written file
    except Exception:
        pass


def _progress_read(rid):
    path = _progress_path(rid)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        if time.time() - rec.get("ts", 0) > _PROGRESS_TTL_S:
            try:
                os.remove(path)
            except Exception:
                pass
            return None
        return rec
    except Exception:
        return None


def _progress_start(rid):
    if not rid:
        return
    _progress_write(rid, {"ts": time.time(), "started": time.time(), "phase": "sources",
                          "trying": [], "tried": [], "total": 0, "done": False,
                          "source": None})


def _progress_set(rid, **fields):
    if not rid:
        return
    rec = _progress_read(rid) or {"ts": time.time(), "tried": [], "trying": []}
    rec.update(fields)
    rec["ts"] = time.time()
    _progress_write(rid, rec)


def _progress_tried(rid, name, verdict, detail=""):
    """One finished attempt, with the honest outcome: playing / remux /
    no-stream. The screen shows these in order under the batch being tried."""
    if not rid:
        return
    rec = _progress_read(rid) or {"ts": time.time(), "tried": [], "trying": []}
    tried = rec.get("tried") or []
    tried.append({"name": (name or "")[:88], "verdict": verdict, "detail": (detail or "")[:60]})
    rec["tried"] = tried[-24:]      # the screen only ever shows a window
    rec["trying"] = []
    rec["ts"] = time.time()
    _progress_write(rid, rec)


def progress_get(rid):
    return _progress_read(rid) or {}


def preview_candidates(tmdb, mtype, season=None, episode=None, quality=None, lang=None,
                       codec=None, budget=None):
    """Read-only candidate preview for PLAYER FEEDBACK.

    Runs the SAME source fan-out the resolve uses (concurrently, with a SHORT
    budget so the list arrives while the viewer is still waiting) plus the light
    account-library scan, and reports what the resolve is about to walk. It NEVER
    adds a torrent, never calls unrestrict and never writes the resolve cache —
    it can only report, so a slow indexer cannot damage the resolve path.
    """
    try:
        imdb = tmdb_to_imdb(tmdb, "movie" if mtype == "movie" else "tv")
        tor, mf, tz = _fetch_sources(imdb, tmdb, mtype, season, episode,
                                     budget=(_PREVIEW_TIMEOUT if budget is None else budget))
        accept = _accept_langs(mtype, lang)
        # 2160 is an EXPLICIT 4K ask (the player's 4K option). Without it the
        # ladder targets 1080 — see _quality_rank_browser.
        want_h = (2160 if quality == "2160" else 1080 if quality == "1080"
                  else 720 if quality == "720" else 480 if quality == "480" else None)
        merged = _filter_cam_when_hd(list(tor) + list(mf) + list(tz))
        try:
            merged = sorted(merged, key=lambda c: _candidate_order_key(c, want_h, lang, codec, accept))
        except Exception:
            pass
        return {
            "imdb": imdb,
            "counts": {"torrentio": len(tor), "mediafusion": len(mf), "torznab": len(tz)},
            "account": _account_library_matches(tmdb, mtype, season, episode)[:8],
            "candidates": [
                {"name": (c.get("name") or "")[:90], "source": c.get("source", ""),
                 "seeders": int(c.get("seeders") or 0), "size_gb": c.get("size_gb")}
                for c in merged[:30]
            ],
            "total": len(merged),
        }
    except Exception as e:
        return {"error": str(e)}


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
        # ── external sources (CastleTV / Vixsrc) ──────────────────────────
        # Reverse-engineered from yapgrid's player. Spends NO Real-Debrid quota.
        # CastleTV's hscow CDN plays DIRECT in the browser (no Referer required),
        # so it costs this bridge no bandwidth and adds no relay hop. Its major
        # titles are frequently Hindi-first, so ext_sources sniffs the TS head
        # and refuses picks whose FIRST audio track is not English rather than
        # silently serving the wrong language.
        if url.path == "/api/rd/ext":
            gate = self._gate()
            if gate:
                return gate
            tmdb = q.get("tmdb", [None])[0]
            mtype = q.get("type", [None])[0]
            source = (q.get("source", ["castle"])[0] or "castle").lower()
            if not tmdb or not mtype:
                return self._json({"error": "tmdb + type required"}, 400)
            if not re.fullmatch(r"\d{1,9}", str(tmdb)) or mtype not in ("movie", "tv", "anime"):
                return self._json({"error": "bad tmdb/type"}, 400)
            if source not in ("castle", "vixsrc"):
                return self._json({"error": "bad source"}, 400)
            try:
                season = int(q.get("season", [0])[0] or 0) or None
                episode = int(q.get("episode", [0])[0] or 0) or None
            except (TypeError, ValueError):
                return self._json({"error": "bad season/episode"}, 400)
            if not _acquire_slot():
                return self._json({"error": "busy"}, 429, {"Retry-After": "10"})
            try:
                import ext_sources
                info = None
                if source == "castle":
                    kind = "movie" if mtype == "movie" else "tv"
                    st, body = http_get(
                        f"https://api.themoviedb.org/3/{kind}/{tmdb}"
                        f"?api_key={TMDB_KEY}&language=en-US", timeout=15)
                    d = json.loads(body) if st == 200 and body else {}
                    date = d.get("release_date") or d.get("first_air_date") or ""
                    info = {
                        "title": d.get("title") or d.get("name"),
                        "originalTitle": d.get("original_title") or d.get("original_name"),
                        "year": int(date[:4]) if date[:4].isdigit() else None,
                        "originalLanguage": d.get("original_language"),
                    }
                res = ext_sources.resolve(int(tmdb), mtype, season, episode,
                                          source=source, info=info)
                return self._json(res, 200 if not res.get("error") else 404)
            except Exception as e:
                _audit(f"EXT {source} tmdb={tmdb} failed: {e}")
                return self._json({"error": f"{source} resolve failed"}, 502)
            finally:
                _release_slot()
        # Read-only candidate PREVIEW for PLAYER FEEDBACK. The resolve above can
        # take seconds (account walk) or tens of seconds (indexer fan-out) on a
        # cold title, during which the player used to show nothing but "Loading
        # repokapone…". This route runs the SAME source fan-out with a SHORT
        # budget (see preview_candidates) so the client can render WHAT IS BEING
        # TRIED as soon as the first results arrive. It is strictly read-only —
        # no torrent add, no unrestrict, no resolve-cache write — so a slow or
        # hung indexer on this path can never damage the resolve.
        if url.path == "/api/rd/progress":
            # Polled by the player's torrent screen while a resolve is running.
            # Read-only, token-gated like the rest, and returns an empty record
            # for an unknown/expired rid rather than an error.
            if self._gate():
                return
            ridq = (q.get("rid", [None])[0] or "")[:64]
            return self._json({"rid": ridq, **progress_get(ridq)}, extra={
                "Cache-Control": "no-store"})
        if url.path == "/api/rd/candidates":
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
                return self._json(preview_candidates(
                    int(tmdb), mtype,
                    _env_int(q.get("season", [None])[0], 0, 100, None),
                    _env_int(q.get("episode", [None])[0], 0, 5000, None),
                    quality=q.get("quality", [None])[0],
                    lang=q.get("lang", [None])[0],
                    codec=q.get("codec", [None])[0]))
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
        rid = (q.get("rid", [None])[0] or "")[:64] or None
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
        if rid:
            args.append(f"--rid={rid}")
        debug = q.get("debug", ["0"])[0] == "1"
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
                    # Per-source candidate counts are exposed ONLY when the
                    # caller explicitly asks debug=1 (diagnostic; stripped from
                    # every normal response so the payload stays identical).
                    if not debug:
                        result.pop("_sources", None)
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
    #
    # KNOWN COST of the DEFAULT path (measured 2026-10-11): the blocking remux
    # writes the WHOLE file (`-movflags faststart`) before serving a single
    # byte. MEASURED: first byte at +58s for a 611 MB file; ~4.7 MB/s
    # RD CDN → Pi, so a 6.8 GB episode extrapolates to ~26 min (the client
    # aborts at 120s → the viewer sees "no stream available").
    # The streaming fragmented-MP4 variant (`-movflags frag_keyframe+empty_moov
    # -f mp4 pipe:1`, first bytes in ~3.4s) is now implemented and gated behind
    # `BRIDGE_STREAMING_REMUX` (STREAMING_REMUX above), default OFF. It was
    # shipped once (de847df) and reverted (00ff8e4, "loads then fails") because
    # the client routed the track request through the CF relay → Vercel chain,
    # which buffers/strips Accept-Ranges; the client is now direct-first, which
    # removes that cause. It is still ONLY enabled after a real end-to-end play
    # (first frame AND seek) is observed — a working blocking remux beats a
    # faster one that serves nothing. Callers should prefer the as-is path
    # (`plan in ("fits","unknown")` → 302) so this branch is only reached for
    # dual-audio files that genuinely need it.
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
        # ── STREAMING REMUX — FLAG-GATED, OFF BY DEFAULT (see STREAMING_REMUX)
        # Same ffmpeg copy, but the client is served the bytes AS THEY APPEAR
        # (fragmented MP4, no Content-Length) instead of waiting for the whole
        # multi-GB file. A background job owns ffmpeg so the disk cache still
        # completes even if the viewer leaves. Only enabled after real playback
        # evidence (first frame + seek) — the blocking path below is the default.
        if streaming_remux_enabled():
            if os.path.exists(cache_path):
                pass  # a job finished while we were checking — fall through to the cache serve below
            else:
                if _remux_recently_failed(file_id):
                    return self._json({"error": f"no {lang} audio track — remux failed"}, 502)
                job, status = _remux_job_start_or_attach(
                    file_id, tmp_path, cache_path, raw_url, lang, cache_dir)
                if status == "busy":
                    return self._json({"error": "busy"}, 429, {"Retry-After": "15"})
                if job is None:  # cannot happen (busy handled above) — typing guard
                    return self._json({"error": "remux unavailable"}, 500)
                return _serve_streaming_remux(self, job, cache_path, tmp_path, file_id)
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
