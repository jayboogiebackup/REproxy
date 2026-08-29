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
import json
import os
import re
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rd_flow import match_episode_file_index, pick_rd_files, pick_link_index, VIDEO_EXTS  # noqa: E402

_info_cache = {}  # torrent_id -> (timestamp, info) — big packs are slow to fetch
_codec_cache = {}  # url -> (timestamp, codec_name) — ffprobe is ~3s, cache it


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
            _codec_cache[url.split("/d/")[-1].split("/")[0] if "/d/" in url else url] = (time.time(), False)
            return False
        if any(x in n for x in ("AAC", "AAC2.0", "MP3", "OPUS", "VORBIS", "FLAC")):
            _codec_cache[url.split("/d/")[-1].split("/")[0] if "/d/" in url else url] = (time.time(), True)
            return True
    import subprocess as _sp
    try:
        # cache key = the RD file id (stable across CDN host rotations)
        key = url
        for frag in ("/d/", "/stream/"):
            if frag in url:
                key = url.split(frag)[1].split("/")[0]
                break
        now = time.time()
        c = _codec_cache.get(key)
        if c and now - c[0] < ttl:
            return c[1]
        proc = _sp.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0", url],
                       capture_output=True, text=True, timeout=25)
        codec = (proc.stdout or "").strip().split(",")[0].strip().lower()
        safe = codec in ("aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le",
                         "pcm_s24le", "pcm_f32le", "pcm_u8", "truehd") if codec else None
        # TrueHD actually decodes in Chrome sometimes (FLAC-compatible core);
        # mark AC-3 family explicitly unsafe
        if codec in ("ac3", "eac3", "dts", "dts-hd", "mlp"):
            safe = False
        _codec_cache[key] = (now, safe)
        return safe
    except Exception:
        return None  # can't probe — don't block playback


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


def rd_find_by_title(q, season=None, episode=None):
    """Search the account's existing torrents by title keywords → link.
       If season+episode given, finds the matching SxxExx file inside a
       season pack (selects the right file, not just the first link)."""
    try:
        ts = rd_get_torrents()
        words = [w.lower() for w in q.split() if (w.isdigit() or len(w) > 2) and w.lower() not in
                 ("and", "the", "for", "with", "from", "that", "this", "not", "are", "was", "but", "you", "all", "she", "his", "her", "its", "has", "had", "have", "who", "which", "what", "when", "where", "why", "how")]
        movie_hits = []  # (codec_score, filename, torrent) for movie mode
        for t in ts:
            if t.get("status") != "downloaded" or not t.get("links"):
                continue
            fn = (t.get("filename") or "").lower()
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
                tag = f"s{int(season):02d}e{int(episode):02d}"
                if tag not in fn:
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
    return bool(re.search(r"\be\d{1,3}\b", fn)) or bool(re.search(r"\bs\d{1,2}\b", fn))


def resolve_stream(tmdb, mtype, season=None, episode=None, quality=None, skip_account=False):
    """Full flow → direct stream URL, with STRICT show/movie separation:
    a show never plays a movie and vice versa. Any resolved URL that fails
    the type check is rejected."""
    result = _resolve_stream_impl(tmdb, mtype, season, episode, quality, skip_account)
    url = result.get("url") if isinstance(result, dict) else None
    if url and not url_matches_type(url, mtype, season, episode):
        return {"error": "resolved link is the wrong type (show/movie mismatch)",
                "imdb": result.get("imdb")}
    return result


def _resolve_stream_impl(tmdb, mtype, season=None, episode=None, quality=None, skip_account=False):
    """Full flow → direct stream URL."""
    if not RD_KEY:
        return {"error": "REALDEBRID_API_KEY not set"}
    imdb = tmdb_to_imdb(tmdb, "movie" if mtype == "movie" else "tv")
    if not imdb:
        return {"error": "no imdb id"}

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
                url = rd_find_by_title(q, season, episode)
                if not url:
                    continue
                # Skip AC-3/DTS account files — they're silent in Chrome.
                # Filename hint avoids the ~3s ffprobe when the name says it.
                safe = audio_codec_is_browser_safe(url, filename_hint=q)
                if safe is False:
                    continue
                return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
        except Exception:
            pass

    order = []
    tried = 0
    deadline = time.time() + 115
    if candidates:
        # Browser-playable first (H264 > HEVC/AV1), biased to requested quality
        want_h = 1080 if quality == "1080" else 720 if quality == "720" else 480 if quality == "480" else None
        order = sorted(candidates,
                       key=lambda c: (codec_rank(c.get("name", ""), want_h), -int(c.get("seeders") or 0)))
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
                if url and audio_codec_is_browser_safe(url) is False:
                    return None  # silent codec — skip
                return url, c
            except Exception:
                return None

        BATCH = 3
        for start in range(0, len(order), BATCH):
            if time.time() > deadline:
                break
            batch = order[start:start + BATCH]
            with _cf.ThreadPoolExecutor(max_workers=BATCH) as ex:
                for res in ex.map(_try_one, batch):
                    tried += 1
                    if not res:
                        continue
                    url, c = res
                    if url:
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
                        safe = audio_codec_is_browser_safe(url)
                        if safe is False:
                            continue
                        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
                except Exception:
                    continue
    # All candidates tried and none streamed — report honestly, skip the slow
    # APIBay/EZTV tail (search_eztv can take 30s+).
    if not candidates or tried > 0:
        return {"error": "no stream available on real-debrid yet (try again in a few minutes)", "imdb": imdb}

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
                url = rd_find_by_title(q)
                if url:
                    return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
        except Exception:
            pass
        return {"error": "no torrents found", "imdb": imdb}

    # NOTE: RD's instantAvailability endpoint is currently disabled (error 37),
    # so we try torrents directly — best-seeded first. RD downloads
    # non-cached torrents on their servers; cached ones are instant.
    # Torrentio-style: try EVERY candidate — RD 451-skips silently and
    # cached/unflagged hashes resolve instantly. Also wait longer for RD to
    # fetch non-cached torrents (RD does the downloading, not us).
    order = sorted(candidates, key=lambda c: -int(c.get("seeders") or 0))
    tried = 0
    for c in order:
        if tried >= 40:
            break
        tried += 1
        try:
            url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"), season=season, episode=episode)
            if url:
                return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
        except Exception:
            continue  # 451 / invalid — next candidate
    return {"error": "could not resolve via real-debrid (all candidates rejected)", "imdb": imdb}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self._json({"status": "ok", "service": "rd-bridge", "rd_key": bool(RD_KEY), "port": PORT})
        if url.path == "/api/rd/stream":
            tmdb = q.get("tmdb", [None])[0]
            mtype = q.get("type", [None])[0]
            if not tmdb or not mtype:
                return self._json({"error": "tmdb + type required"}, 400)
            season = q.get("season", [None])[0]
            episode = q.get("episode", [None])[0]
            quality = q.get("quality", [None])[0]
            skip_account = q.get("skip_account", ["0"])[0] == "1"
            # Run the resolve in a SUBPROCESS with a hard timeout — a hung
            # RD call can never deadlock the server thread pool this way.
            import subprocess as _sp
            import sys as _sys
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolve_cli.py")
            args = [_sys.executable, script, tmdb, mtype]
            if season:
                args.append(season)
            if episode:
                args.append(episode)
            if quality:
                args.append(quality)
            if skip_account:
                args.append("--skip-account")
            try:
                proc = _sp.run(args, capture_output=True, text=True, timeout=150)
                out = proc.stdout.strip()
                if out:
                    result = json.loads(out.splitlines()[-1])
                    return self._json(result, 200 if result.get("url") else 404)
                return self._json({"error": "resolve failed"}, 404)
            except _sp.TimeoutExpired:
                return self._json({"error": "resolve timeout (55s)"}, 504)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if url.path == "/api/rd/search":
            qq = q.get("q", [""])[0]
            if not qq:
                return self._json({"error": "q required"}, 400)
            return self._json(search_apibay(qq, q.get("cat", ["0"])[0]))
        # OpenSubtitles: list subs for a title
        if url.path == "/api/rd/subs":
            tmdb = q.get("tmdb", [None])[0]
            mtype = q.get("type", [None])[0]
            if not tmdb or not mtype:
                return self._json({"error": "tmdb + type required"}, 400)
            season = q.get("season", [None])[0]
            episode = q.get("episode", [None])[0]
            subs = os_subs(int(tmdb), mtype,
                           int(season) if season else None,
                           int(episode) if episode else None)
            return self._json({"subs": subs})
        # OpenSubtitles: get the converted VTT for a file_id
        if url.path == "/api/rd/sub":
            fid = q.get("file_id", [None])[0]
            if not fid:
                return self._json({"error": "file_id required"}, 400)
            path = os_download_vtt(fid)
            if not path:
                return self._json({"error": "download failed"}, 404)
            try:
                with open(path, "rb") as f:
                    vtt = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/vtt; charset=utf-8")
                self.send_header("Content-Length", str(len(vtt)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(vtt)
            except Exception:
                return self._json({"error": "read failed"}, 500)
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"RD-bridge on :{PORT} (key={'set' if RD_KEY else 'MISSING'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
