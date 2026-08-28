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

from rd_flow import match_episode_file_index, pick_rd_files, pick_link_index  # noqa: E402

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


def rd_get(path):
    req = urllib.request.Request(f"{RD_API}{path}", headers={"Authorization": f"Bearer {RD_KEY}"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


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


def tmdb_to_imdb(tmdb, mtype):
    """TMDB id → IMDb id via external_ids."""
    try:
        st, body = http_get(f"https://api.themoviedb.org/3/{mtype}/{tmdb}/external_ids?api_key={TMDB_KEY}", timeout=15)
        d = json.loads(body)
        return d.get("imdb_id")
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


def rd_find_by_title(q, season=None, episode=None):
    """Search the account's existing torrents by title keywords → link.
       If season+episode given, finds the matching SxxExx file inside a
       season pack (selects the right file, not just the first link)."""
    try:
        req = urllib.request.Request(f"{RD_API}/torrents", headers={"Authorization": f"Bearer {RD_KEY}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            ts = json.loads(r.read())
        words = [w.lower() for w in q.split() if len(w) > 2]
        for t in ts:
            if t.get("status") != "downloaded" or not t.get("links"):
                continue
            fn = (t.get("filename") or "").lower()
            # match if ANY distinctive word hits (handles title aliases)
            if not any(w in fn for w in words):
                continue
            # If asking for a specific episode, find the matching file
            if season is not None and episode is not None:
                try:
                    info = rd_get(f"/torrents/info/{t['id']}")
                    tag = f"s{int(season):02d}e{int(episode):02d}"
                    for f in info.get("files", []):
                        if tag in f.get("path", "").lower():
                            # RD keeps one link per selected file, ordered by
                            # file id — pick the link for this file directly
                            # (re-selecting on a completed torrent 404s).
                            links = info.get("links") or []
                            idx = f["id"] - 1
                            if 0 <= idx < len(links):
                                for lf in (links[idx],):
                                    try:
                                        ur = rd_post("/unrestrict/link", {"link": lf})
                                        dl = ur.get("download") or ur.get("streamable")
                                        if dl:
                                            return dl
                                    except Exception:
                                        continue
                                return links[idx]
                            break
                except Exception:
                    pass
            try:
                ur = rd_post("/unrestrict/link", {"link": t["links"][0]})
                return ur.get("download") or ur.get("streamable")
            except Exception:
                return t["links"][0]
    except Exception:
        pass
    return None


def rd_find_existing(info_hash):
    """Look for an already-added torrent with this hash → return its unrestrictable link."""
    try:
        req = urllib.request.Request(f"{RD_API}/torrents", headers={"Authorization": f"Bearer {RD_KEY}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            ts = json.loads(r.read())
        for t in ts:
            if (t.get("hash") or "").lower() == info_hash.lower() and t.get("status") == "downloaded" and t.get("links"):
                return t["links"][0]
    except Exception:
        pass
    return None


def rd_add_and_stream(info_hash, file_idx=None, filename=None, season=None, episode=None):
    """Harbor-proven flow: addMagnet → poll → selectFiles/{id} (files=1,2,3)
       → poll downloaded → pickLinkIndex → unrestrict → direct URL.
       Returns None fast for non-cached (Torrentio-style)."""
    # 0. Already in the account? Use the existing link (instant, no re-add).
    existing = rd_find_existing(info_hash)
    if existing and not filename and season is None:
        try:
            ur = rd_post("/unrestrict/link", {"link": existing})
            return ur.get("download") or ur.get("streamable")
        except Exception:
            return existing

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
                # RD is fetching it on THEIR servers (Stremio-style). Keep
                # polling — popular torrents finish in 30s-3min, then stream.
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
    try:
        ur = rd_post("/unrestrict/link", {"link": links[link_idx]})
        return ur.get("download") or ur.get("streamable")
    except Exception:
        return None


def codec_rank(name):
    """Browser-friendly + English-first ranking. Lower = better.
    Penalizes: HEVC/AV1/2160p (unplayable), non-English dubs (RUS/CZ/SK/ES/IT/
    PT/DE/HI), HDCAM (cam). Prefers: H264, 1080p/720p, MP4, English tags."""
    n = (name or "").upper()
    score = 0
    if any(x in n for x in ("X265", "H265", "HEVC", "AV1", "VP9", "2160P", "4K")):
        score += 100  # unplayable in most browsers
    if "HDCAM" in n or "CAMRIP" in n or "TELESYNC" in n or "TS-" in n:
        score += 200  # cam/telecine quality
    # Non-English audio markers (dubbed/foreign releases)
    if any(x in n for x in ("DUB", "RUS", "CZ", "SK", "ESP", "ITA", "POR", "GER", "HIN", "ARAB", "TUR", "UKR", "POL", "FRE", "NLD")):
        score += 50
    if "MULTI" in n:
        score += 10  # multi-audio usually includes English, but not guaranteed
    if "ENGLISH" in n or "ENG." in n or " EN " in n:
        score -= 30
    if "X264" in n or "H264" in n or "AVC" in n:
        score += 0
    if "720P" in n:
        score += 5
    if "1080P" in n:
        score += 3
    if n.endswith(".MP4") or ".MP4 " in n:
        score -= 2  # MP4 container plays more reliably than MKV
    return score


def resolve_stream(tmdb, mtype, season=None, episode=None):
    """Full flow → direct stream URL."""
    if not RD_KEY:
        return {"error": "REALDEBRID_API_KEY not set"}
    imdb = tmdb_to_imdb(tmdb, "movie" if mtype == "movie" else "tv")
    if not imdb:
        return {"error": "no imdb id"}

    # 0. The user's OWN RD library first (Harbor-style, instant) — already-
    #    downloaded content always works, no 451, no waiting. Only check the
    #    PRIMARY titles (alt-titles in every language would take 40s+).
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
            if url:
                return {"url": url, "source": "realdebrid", "title": f"{q} (from account)", "imdb": imdb}
    except Exception:
        pass

    # Torrentio — aggregates 100+ indexers by IMDb id. Only cached adds
    # return instantly; 451s skip in ~1.5s each. Tight 20s budget.
    candidates = []
    candidates.extend(search_torrentio(imdb, mtype, season, episode))
    order = []
    tried = 0
    deadline = time.time() + 115
    if candidates:
        # Browser-playable first (H264 > HEVC/AV1), then by seeders
        order = sorted(candidates,
                       key=lambda c: (codec_rank(c.get("name", "")), -int(c.get("seeders") or 0)))
        # Stremio-style: try candidates until one streams. Cached = instant;
        # non-cached = RD downloads on their servers (30s-3min).
        deadline = time.time() + 115
        tried = 0
        for c in order:
            if tried >= 8 or time.time() > deadline:
                break
            tried += 1
            try:
                url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"),
                                        filename=c.get("filename"), season=season, episode=episode)
                if url:
                    return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
            except Exception:
                continue
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
                        return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
                except Exception:
                    continue
    # If the deadline fired with no URL and we still have candidates, skip the
    # slow APIBay/EZTV tail entirely and report honestly.
    if not candidates or (tried > 0 and time.time() >= deadline):
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
        self._json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"RD-bridge on :{PORT} (key={'set' if RD_KEY else 'MISSING'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
