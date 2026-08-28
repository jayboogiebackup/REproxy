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
    req = urllib.request.Request(f"{RD_API}{path}", data=body, headers={
        "Authorization": f"Bearer {RD_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


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


def rd_add_and_stream(info_hash, file_idx=None, filename=None):
    """addMagnet → selectFiles → wait → unrestrict → direct URL.
       filename (from Torrentio behaviorHints) picks the exact episode file
       from RD's real file list, avoiding fileIdx mismatches on packs."""
    # 0. Already in the account? Use the existing link (instant, no re-add).
    #    BUT skip this when we have a filename hint — the existing link may
    #    point at a different episode file of a season pack.
    existing = rd_find_existing(info_hash)
    if existing and not filename:
        try:
            ur = rd_post("/unrestrict/link", {"link": existing})
            return ur.get("download") or ur.get("streamable")
        except Exception:
            return existing  # the link itself may already be playable
    # 1. Add magnet
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    try:
        added = rd_post("/torrents/addMagnet", {"magnet": magnet})
    except Exception:
        return None
    tid = added.get("id")
    if not tid:
        return None  # 451 infringing / invalid → next candidate
    # 2. Info + pick the largest video file
    try:
        info = rd_get(f"/torrents/info/{tid}")
    except Exception:
        return None
    files = info.get("files", [])
    video = [f for f in files if re.search(r"\.(mp4|mkv|avi|mov|webm)$", f.get("path", ""), re.I)]
    if not video:
        return None
    chosen = None
    # 1) exact filename match (Torrentio behaviorHints.filename)
    if filename:
        for f in video:
            if filename.lower() in f.get("path", "").lower() or f.get("path", "").lower().endswith(filename.lower()):
                chosen = f.get("id")
                break
    # 2) fileIdx (RD ids are 1-based; Torrentio is usually 0-based → try both)
    if chosen is None and file_idx is not None:
        for fid in (int(file_idx) + 1, int(file_idx)):
            if any(f.get("id") == fid for f in video):
                chosen = fid
                break
    # 3) largest video file
    if chosen is None:
        chosen = max(video, key=lambda f: f.get("bytes", 0)).get("id")
    # 3. Select it — RD expects `files` + `torrent_id` (verified: 204 success)
    rd_post("/torrents/selectFiles", {"files": str(chosen), "torrent_id": str(tid)})
    # 4. Wait for the link (RD processes instantly for cached, downloads for new)
    link = None
    for _ in range(40):  # up to 60s — RD downloads non-cached torrents itself
        time.sleep(1.5)
        try:
            info = rd_get(f"/torrents/info/{tid}")
            if info.get("status") == "downloaded" and info.get("links"):
                link = info["links"][0]
                break
            if info.get("status") in ("magnet_error", "error", "dead"):
                break
        except Exception:
            break
    if not link:
        return None
    # 5. Unrestrict → direct playable URL
    try:
        ur = rd_post("/unrestrict/link", {"link": link})
        return ur.get("download") or ur.get("streamable")
    except Exception:
        return None


def resolve_stream(tmdb, mtype, season=None, episode=None):
    """Full flow → direct stream URL."""
    if not RD_KEY:
        return {"error": "REALDEBRID_API_KEY not set"}
    imdb = tmdb_to_imdb(tmdb, "movie" if mtype == "movie" else "tv")
    if not imdb:
        return {"error": "no imdb id"}

    # Torrentio first — per-episode releases give the exact file (an account
    # season-pack may point at E01 even when asking for E13)
    candidates = []
    candidates.extend(search_torrentio(imdb, mtype, season, episode))
    if candidates:
        order = sorted(candidates, key=lambda c: -int(c.get("seeders") or 0))
        tried = 0
        for c in order:
            if tried >= 30:
                break
            tried += 1
            try:
                url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"), filename=c.get("filename"))
                if url:
                    return {"url": url, "source": "realdebrid", "title": c.get("name", "")[:80], "imdb": imdb}
            except Exception:
                continue
    # Fall back to the user's OWN RD library — already-downloaded content
    # always works (no 451 walls) and streams instantly.
    try:
        st, body = http_get(f"https://api.themoviedb.org/3/{'movie' if mtype == 'movie' else 'tv'}/{tmdb}?api_key={TMDB_KEY}", timeout=15)
        tdata = json.loads(body)
        names = set()
        for k in ("title", "name", "original_title", "original_name"):
            if tdata.get(k):
                names.add(tdata[k])
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
            url = rd_add_and_stream(c["info_hash"], file_idx=c.get("file_idx"))
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
            try:
                result = resolve_stream(int(tmdb), mtype,
                                        int(season) if season else None,
                                        int(episode) if episode else None)
                return self._json(result, 200 if result.get("url") else 404)
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
