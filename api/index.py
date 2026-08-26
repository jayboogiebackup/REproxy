# -*- coding: utf-8 -*-
"""
REproxy — hentaihaven.xxx streaming proxy for repeaks (Vercel Python/Flask).

Vercel runs this file as the WSGI entrypoint (flask in requirements.txt), so no
vercel.json rewrites are needed: every path reaches the Flask app below.

Flow (all server-side HTTP, Chrome TLS impersonation via curl_cffi):
  watch page -> player.php iframe (data blob) -> player.php (x-secure-token)
  -> decode token (ROT13 + base64 x3) -> en/iv -> POST api.php -> stream URL

Endpoints:
  GET /api/stream?slug=<watch-slug>&ep=<episode>   -> { status, src, sources }
  GET /api/search?q=<title>                        -> { status, found, slug, title, url }
  GET /api/catalog?tag=hanime&page=1               -> { status, tag, page, totalPages, count, titles[] }
"""
import base64
import json
import os
import re
import urllib.request
import urllib.parse
from urllib.parse import quote

from flask import Flask, jsonify, request, Response

try:
    from curl_cffi import requests as cf_requests
except Exception as e:  # keep the app importable so errors surface as JSON, not a crash
    cf_requests = None
    _CURL_IMPORT_ERROR = str(e)
else:
    _CURL_IMPORT_ERROR = None

BASE = "https://hentaihaven.xxx"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

app = Flask(__name__)


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Type, Content-Length, Accept-Ranges"
    return resp


def _json(data, status=200):
    resp = jsonify(data)
    resp.status_code = status
    return _cors(resp)


@app.after_request
def add_cors(resp):
    return _cors(resp)


@app.route("/", methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def preflight(path=None):
    resp = jsonify({"ok": True})
    resp.status_code = 204
    return _cors(resp)


def http_get(url: str) -> str | None:
    if cf_requests is None:
        return None
    try:
        r = cf_requests.get(
            url,
            impersonate="chrome",
            timeout=25,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if r.status_code != 200 or "Just a moment" in r.text:
            return None
        return r.text
    except Exception:
        return None


# ═══════════════ YouTube InnerTube (RE:music) ═══════════════

YT_API = "https://www.youtube.com/youtubei/v1"
YT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def yt_post(endpoint: str, body: dict) -> dict | None:
    if cf_requests is None:
        return None
    try:
        r = cf_requests.post(
            f"{YT_API}/{endpoint}?prettyPrint=false",
            impersonate="chrome",
            timeout=25,
            headers={
                "User-Agent": YT_UA,
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://www.youtube.com",
                "Referer": "https://www.youtube.com/",
            },
            json={"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240701.01.00"}}, **body},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def yt_walk(node, out: list):
    """Recursively collect videoRenderer items from InnerTube responses."""
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            out.append(node["videoRenderer"])
        if "videoWithContextRenderer" in node and isinstance(node["videoWithContextRenderer"], dict):
            out.append(node["videoWithContextRenderer"])
        for v in node.values():
            yt_walk(v, out)
    elif isinstance(node, list):
        for v in node:
            yt_walk(v, out)


def yt_dur(text) -> int:
    if not text:
        return 0
    try:
        parts = [int(x) for x in str(text).split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0] or 0
    except Exception:
        return 0


def yt_map(v: dict) -> dict | None:
    vid = v.get("videoId")
    if not vid:
        return None
    title_r = v.get("title") or {}
    title = (title_r.get("runs") or [{}])[0].get("text") or title_r.get("simpleText") or ""
    if not title:
        return None
    owner_r = v.get("ownerText") or {}
    author = (owner_r.get("runs") or [{}])[0].get("text") or (v.get("shortBylineText") or {}).get("runs", [{}])[0].get("text") or "Unknown"
    dt = v.get("lengthText") or {}
    dur_text = dt.get("simpleText") or "".join(x.get("text", "") for x in (dt.get("runs") or []))
    thumbs = (v.get("thumbnail") or {}).get("thumbnails") or []
    thumb = None
    for t in reversed(thumbs):
        u = t.get("url") or ""
        if "maxres" in u or "hq720" in u or "hqdefault" in u:
            thumb = u
            break
    thumb = thumb or (thumbs[-1].get("url") if thumbs else None) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    thumb = thumb.split("?")[0]
    return {"videoId": vid, "title": title, "author": author, "thumb": thumb, "duration": yt_dur(dur_text)}


@app.route("/api/ytmusic/search")
def ytmusic_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return _json({"error": "Missing q param", "items": []}), 400
    data = yt_post("search", {"query": q, "params": "EgWKAQIIAWoKEAoQCRADEAA%3D"})
    if not data:
        return _json({"error": "YouTube blocked the request", "items": []}), 502
    items: list = []
    yt_walk(data.get("contents"), items)
    tracks = [t for t in (yt_map(v) for v in items) if t]
    return _json({"items": tracks, "count": len(tracks), "source": "innertube"})


@app.route("/api/ytmusic/stream")
def ytmusic_stream():
    vid = (request.args.get("id") or "").strip()
    if not vid:
        return _json({"error": "Missing id param"}), 400

    # 0) Piped instances first (they proxy YouTube streams themselves and often work from Vercel)
    for piped in [
        "https://api.piped.private.coffee",
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.adminforge.de",
        "https://pipedapi.reallyaweso.me",
    ]:
        if cf_requests is None:
            break
        try:
            r = cf_requests.get(
                f"{piped}/streams/{vid}",
                impersonate="chrome",
                timeout=20,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            if r.status_code != 200:
                continue
            d = r.json()
            audio = [s for s in d.get("audioStreams") or [] if s.get("url")]
            if audio:
                audio.sort(key=lambda s: s.get("bitrate") or 0, reverse=True)
                pick = next((s for s in audio if "mp4" in str(s.get("mimeType", ""))), audio[0])
                return _json({
                    "id": vid,
                    "title": d.get("title", ""),
                    "author": (d.get("uploader") or {}).get("name", ""),
                    "lengthSeconds": int(d.get("duration") or 0),
                    "url": pick.get("url", ""),
                    "mimeType": pick.get("mimeType", ""),
                    "itag": pick.get("itag", 0),
                    "bitrate": pick.get("bitrate", 0),
                    "hls": d.get("hls"),
                    "source": f"piped:{piped}",
                })
            if d.get("hls"):
                return _json({
                    "id": vid, "title": d.get("title", ""), "author": (d.get("uploader") or {}).get("name", ""),
                    "lengthSeconds": int(d.get("duration") or 0), "url": "", "mimeType": "", "itag": 0,
                    "bitrate": 0, "hls": d.get("hls"), "source": f"piped-hls:{piped}",
                })
        except Exception:
            continue

    # 1) YouTube player API with client rotation
    statuses: list = []
    for client in [
        {"clientName": "ANDROID", "clientVersion": "20.10.20"},
        {"clientName": "ANDROID", "clientVersion": "19.09.37"},
        {"clientName": "WEB", "clientVersion": "2.20240701.01.00"},
        {"clientName": "IOS", "clientVersion": "19.09.3"},
        {"clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER", "clientVersion": "2.0"},
    ]:
        if cf_requests is None:
            break
        try:
            r = cf_requests.post(
                f"{YT_API}/player?prettyPrint=false",
                impersonate="chrome",
                timeout=25,
                headers={
                    "User-Agent": YT_UA,
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                    "Origin": "https://www.youtube.com",
                    "Referer": f"https://www.youtube.com/watch?v={vid}",
                },
                json={
                    "context": {"client": client},
                    "videoId": vid,
                    "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
                    "contentCheckOk": True,
                    "racyCheckOk": True,
                },
            )
            if r.status_code != 200:
                continue
            d = r.json()
            sd = d.get("streamingData") or {}
            audio = [f for f in sd.get("adaptiveFormats", []) if str(f.get("mimeType", "")).startswith("audio")]
            if not audio and not sd.get("hlsManifestUrl"):
                continue
            audio.sort(key=lambda f: f.get("bitrate") or 0, reverse=True)
            pick = next((f for f in audio if "mp4" in str(f.get("mimeType", ""))), audio[0] if audio else None)
            return _json({
                "id": vid,
                "title": (d.get("videoDetails") or {}).get("title", ""),
                "author": (d.get("videoDetails") or {}).get("author", ""),
                "lengthSeconds": int((d.get("videoDetails") or {}).get("lengthSeconds") or 0),
                "url": (pick or {}).get("url", ""),
                "mimeType": (pick or {}).get("mimeType", ""),
                "itag": (pick or {}).get("itag", 0),
                "bitrate": (pick or {}).get("bitrate", 0),
                "hls": sd.get("hlsManifestUrl"),
                "expiresInSeconds": sd.get("expiresInSeconds", 0),
                "client": client["clientName"],
            })
        except Exception:
            continue
    return _json({"error": "Could not get a playable stream (blocked)", "id": vid, "attempts": [c["clientName"] + "@" + c["clientVersion"] for c in [
        {"clientName": "ANDROID", "clientVersion": "20.10.20"},
        {"clientName": "ANDROID", "clientVersion": "19.09.37"},
        {"clientName": "WEB", "clientVersion": "2.20240701.01.00"},
        {"clientName": "IOS", "clientVersion": "19.09.3"},
        {"clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER", "clientVersion": "2.0"},
    ]]}), 502


@app.route("/api/ytmusic/play")
def ytmusic_play():
    """Relay the audio stream through the proxy (avoids YouTube IP-locking the URL).

    The /stream endpoint returns a direct googlevideo URL bound to the proxy's IP.
    Browsers on other IPs get 403. This endpoint fetches the bytes server-side
    and streams them back so playback always works. CORS * for the <audio> tag.
    """
    vid = (request.args.get("id") or "").strip()
    if not vid:
        return _json({"error": "Missing id param"}), 400
    # Resolve the stream URL first (reuse the resolution logic)
    import urllib.request as _ur

    resolved = None
    # quick inline resolution: hit our own stream endpoint
    try:
        with _ur.urlopen(f"{request.host_url}api/ytmusic/stream?id={vid}", timeout=25) as r:
            data = json.loads(r.read().decode())
        resolved = data.get("url") or data.get("hls")
    except Exception:
        resolved = None
    if not resolved:
        return _json({"error": "Could not resolve stream", "id": vid}), 502
    if cf_requests is None:
        return _json({"error": "curl_cffi unavailable"}), 502
    try:
        r = cf_requests.get(resolved, impersonate="chrome", timeout=30, stream=True)
        if r.status_code != 200:
            return _json({"error": f"Upstream {r.status_code}"}), 502
        resp = Response(
            r.iter_content(chunk_size=64 * 1024),
            status=200,
            mimetype=r.headers.get("Content-Type", "audio/mp4"),
        )
        resp.headers["Content-Disposition"] = "inline"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as e:
        return _json({"error": f"Relay failed: {e}"}), 502


def _rot13(s: str) -> str:
    out = []
    for c in s:
        if "a" <= c <= "z":
            out.append(chr((ord(c) - 97 + 13) % 26 + 97))
        elif "A" <= c <= "Z":
            out.append(chr((ord(c) - 65 + 13) % 26 + 65))
        else:
            out.append(c)
    return "".join(out)


def decode_secure_token(content: str) -> dict | None:
    try:
        data = content.replace("sha512-", "", 1)
        for _ in range(3):
            data = _rot13(data)
            data = base64.b64decode(data).decode("utf-8")
        return json.loads(data)
    except Exception:
        return None


def get_player_config(watch_url: str) -> dict | None:
    html = http_get(watch_url)
    if not html:
        return None
    m = re.search(r'src="([^"]*player\.php\?data=[^"]+)"', html)
    if not m:
        return None
    player_url = m.group(1)
    if not player_url.startswith("http"):
        player_url = BASE + player_url
    player_html = http_get(player_url)
    if not player_html:
        return None
    m = re.search(r'name="x-secure-token" content="([^"]+)"', player_html)
    if not m:
        return None
    cfg = decode_secure_token(m.group(1))
    if not cfg or not cfg.get("en") or not cfg.get("iv"):
        return None
    uri = cfg["uri"]
    if not uri.startswith("http"):
        uri = "https:" + uri
    return {"en": cfg["en"], "iv": cfg["iv"], "uri": uri}


def get_stream_data(uri: str, en: str, iv: str) -> dict | None:
    if cf_requests is None:
        return None
    try:
        r = cf_requests.post(
            uri.rstrip("/") + "/api.php",
            impersonate="chrome",
            timeout=25,
            headers={
                "User-Agent": UA,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE,
                "Referer": BASE + "/",
            },
            data={"action": "zarat_get_data_player_ajax", "a": en, "b": iv},
        )
        data = r.json()
        if not data.get("status") or not data.get("data", {}).get("sources"):
            return None
        sources = data["data"]["sources"]
        if not sources or not sources[0].get("src"):
            return None
        return {"src": sources[0]["src"], "sources": sources}
    except Exception:
        return None


@app.route("/")
def api_health():
    return _json(
        {
            "status": True,
            "service": "REproxy",
            "curl_cffi_loaded": cf_requests is not None,
            "curl_cffi_error": _CURL_IMPORT_ERROR,
            "routes": ["/api/catalog", "/api/search", "/api/stream"],
        }
    )


@app.route("/api/replayer/relay")
def api_replayer_relay():
    """Relay the nebula HLS stream with corrected content-types.
    The CDN labels every file image/jpeg which breaks hls.js. We fetch the
    exact upstream URL and serve it with a proper content-type; playlist
    bodies have their relative URLs rewritten to go through this relay.
    GET /api/replayer/relay?url=<encoded nebula url>
    """
    import re as _re

    url = request.args.get("url") or ""
    allowed_prefixes = (
        "https://nebula.bright67.online/",
        "https://moon.peakstorm.top/",
        "https://rapidnight.top/",
        "https://stormgate.top/",
    )
    if not url.startswith(allowed_prefixes):
        return _json({"status": False, "error": "invalid url"}), 400

    referer = "https://www.vidking.net/" if url.startswith(("https://moon.peakstorm.top/", "https://rapidnight.top/", "https://stormgate.top/")) else "https://cinesrc.st/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as exc:
        return _json({"status": False, "error": f"upstream: {exc}"}), 502

    is_playlist = data[:20].startswith(b"#EXTM3U")

    if is_playlist:
        ct = "application/vnd.apple.mpegurl"
        body = data.decode("utf-8", "replace")
        base = url.rsplit("/", 1)[0]

        def fix(rel):
            rel = rel.strip()
            if rel.startswith("#"):
                return rel
            # Absolute vidking CDN URLs → route through the relay too (content-type fix)
            if rel.startswith(("https://stormgate.top/", "https://rapidnight.top/", "https://moon.peakstorm.top/", "https://nebula.bright67.online/")):
                return f"https://reproxy-seven.vercel.app/api/replayer/relay?url={urllib.parse.quote(rel)}"
            if rel.startswith("http"):
                return rel
            return f"https://reproxy-seven.vercel.app/api/replayer/relay?url={urllib.parse.quote(base + '/' + rel)}"

        body = _re.sub(r"(?m)^([^#\n][^\n]*)$", lambda m: fix(m.group(1)), body)
        # Also rewrite quoted URIs inside attributes (EXT-X-MAP, EXT-X-KEY...)
        body = _re.sub(r'URI="([^"]+)"', lambda m: f'URI="{fix(m.group(1))}"', body)
        data = body.encode()
    else:
        ct = "video/mp4" if url.endswith((".mp4", ".jpg", ".png", ".jpeg")) else "application/octet-stream"

    return Response(data, mimetype=ct, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
    })




@app.route("/api/replayer/stream")
def api_replayer_stream():
    """RE:player standalone stream — direct HLS from the resolver service.
    GET /api/replayer/stream?tmdb=387&type=tv&season=1&episode=1
    Returns the resolved nebula m3u8 + metadata. Falls back to the cinesrc
    embed URL if the resolver is down.
    """
    tmdb = (request.args.get("tmdb") or "").strip()
    mtype = (request.args.get("type") or "").strip()
    season = (request.args.get("season") or "").strip()
    episode = (request.args.get("episode") or "").strip()
    if not tmdb or mtype not in ("movie", "tv"):
        return _json({"status": False, "error": "tmdb (number) + type (movie|tv) required"}), 400

    resolver = os.environ.get(
        "RESOLVER_URL",
        "https://england-successfully-jaguar-poems.trycloudflare.com",
    ).rstrip("/")
    url = None
    if resolver:
        try:
            params = {"tmdb": tmdb, "type": mtype}
            if mtype == "tv":
                params["season"] = season or "1"
                params["episode"] = episode or "1"
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{resolver}/resolve?{qs}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            if data.get("url"):
                    url = data["url"]
                    return _json({
                        "status": True,
                        "player": "RE:player",
                        "source": "direct-hls",
                        "tmdb": int(tmdb),
                        "type": mtype,
                        "season": int(season) if season else None,
                        "episode": int(episode) if episode else None,
                        "url": url,
                        "provider": data.get("provider", "vidking"),
                        "quality": data.get("quality", "1080p/720p/480p"),
                        "cached": data.get("cached", False),
                        "ms": data.get("ms"),
                        "servers": [{"id": "vidking", "name": "RE:player", "url": url}],
                    })
        except Exception as exc:
            return _json({"status": False, "error": f"resolver error: {exc}"}), 502

    # Resolver unavailable → give the embed fallback
    if mtype == "tv":
        embed = f"https://cinesrc.st/embed/tv/{tmdb}?s={season or 1}&e={episode or 1}"
    else:
        embed = f"https://cinesrc.st/embed/movie/{tmdb}"
    return _json({
        "status": True,
        "player": "RE:player",
        "source": "embed-fallback",
        "tmdb": int(tmdb),
        "type": mtype,
        "season": int(season) if season else None,
        "episode": int(episode) if episode else None,
        "url": embed,
    })


@app.route("/api/stream")
def api_stream():
    slug = (request.args.get("slug") or "").strip()
    ep = (request.args.get("ep") or "1").strip()
    if not slug:
        return _json({"status": False, "error": 'Missing "slug" query param'}), 400

    watch_candidates = []
    if ep and ep != "1":
        watch_candidates.append(f"{BASE}/watch/{slug}/episode-{ep}/")
    watch_candidates.append(f"{BASE}/watch/{slug}/episode-1/")
    watch_candidates.append(f"{BASE}/watch/{slug}/")

    cfg = None
    for w in watch_candidates:
        cfg = get_player_config(w)
        if cfg:
            break
    if not cfg:
        return _json({"status": False, "error": f"Could not resolve player config for {slug}"}), 404

    stream = get_stream_data(cfg["uri"], cfg["en"], cfg["iv"])
    if not stream:
        return _json({"status": False, "error": "Could not fetch stream URL"}), 502

    return _json(
        {"status": True, "src": stream["src"], "sources": stream["sources"], "slug": slug, "episode": ep}
    )


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return _json({"status": False, "error": 'Missing "q" query param'}), 400

    # normalize: drop trailing episode/season markers (" 2", " season 1", " 1080p")
    base_q = re.sub(r"\s+\d+\s*$", "", q)
    base_q = re.sub(r"\s*[-\u2013]\s*(episode|season)?\s*\d+\s*$", "", base_q, flags=re.I).strip()
    queries = list(dict.fromkeys([q, base_q])) if base_q and base_q != q else [q]

    best = None
    best_score = 999
    for query in queries:
        html = http_get(f"{BASE}/?s={quote(query, safe='')}")
        if not html:
            continue
        # collect all watch results (slug, title) — WordPress relevance is unreliable
        results = re.findall(r'href="https://hentaihaven\.xxx/watch/([^"/]+)/"[^>]*title="([^"]+)"', html)
        seen = {}
        for slug, title in results:
            t = title.strip()
            if slug not in seen:
                seen[slug] = t

        ql = query.lower().strip()
        for slug, title in seen.items():
            tl = title.lower()
            if tl == ql:
                score = 0
            elif tl.startswith(ql):
                score = 1
            elif ql.startswith(tl):
                score = 2
            elif ql in tl:
                score = 3
            else:
                score = 99
            if score < best_score:
                best_score = score
                best = (slug, title)
        if best_score == 0:
            break

    if not best:
        return _json({"status": False, "found": False})
    return _json(
        {"status": True, "found": True, "slug": best[0], "title": best[1], "url": f"{BASE}/watch/{best[0]}/"}
    )


XREELS_SUBS = {
    "for-you": ["nsfw", "gonewild", "realgirls", "nsfw_gif", "legalteens", "petitegonewild", "ass", "boobs", "thick", "asiansgonewild", "creampies", "cumsluts", "blowjobs", "milf", "mombod", "pawg", "tiktoknsfw"],
    "amateur": ["realgirls", "gonewild", "amateur", "homegrown", "petitegonewild"],
    "ass": ["ass", "bigasses", "pawg", "thick", "booty", "asstastic"],
    "boobs": ["boobs", "bigboobs", "busty", "hugeboobs", "smallboobs"],
    "blowjob": ["blowjobs", "blowjob", "girlsfinishingthejob", "deepthroat"],
    "creampie": ["creampies", "creampie"],
    "milf": ["milf", "mombod", "cougars", "hotmoms"],
    "asian": ["asiansgonewild", "asiannsfw", "asiangirls"],
    "lesbian": ["lesbians", "lesbian"],
    "gifs": ["nsfw_gif", "porn_gifs"],
}


_reddit_token_cache = {"token": None, "expires": 0}


def _reddit_oauth_token() -> str | None:
    """Get an OAuth token for Reddit's official API (oauth.reddit.com).

    Uses REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET env vars (script app,
    read-only — free to create at reddit.com/prefs/apps). Without them,
    falls back to the scraping endpoint.
    """
    now = int(__import__("time").time())
    if _reddit_token_cache["token"] and _reddit_token_cache["expires"] > now + 60:
        return _reddit_token_cache["token"]
    cid = os.environ.get("REDDIT_CLIENT_ID")
    sec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec or cf_requests is None:
        return None
    try:
        r = cf_requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(cid, sec),
            impersonate="chrome",
            timeout=20,
            headers={"User-Agent": "linux:xreels.repeaks:v1.0 (by /u/xreels_bot)"},
        )
        if r.status_code != 200:
            return None
        j = r.json()
        tok = j.get("access_token")
        if tok:
            _reddit_token_cache["token"] = tok
            _reddit_token_cache["expires"] = now + int(j.get("expires_in", 3600))
        return tok
    except Exception:
        return None


def _reddit_get(url: str) -> dict | None:
    """Fetch reddit JSON — official OAuth API first, scraping fallback."""
    if cf_requests is None:
        return None
    token = _reddit_oauth_token()
    try:
        if token:
            r = cf_requests.get(
                url.replace("https://www.reddit.com", "https://oauth.reddit.com"),
                impersonate="chrome",
                timeout=25,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "linux:xreels.repeaks:v1.0 (by /u/xreels_bot)",
                    "Accept": "application/json",
                },
            )
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return None
        # fallback: scraping (chrome impersonation)
        r = cf_requests.get(
            url,
            impersonate="chrome",
            timeout=25,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None
    except Exception:
        return None


def _reel_from_post(p: dict) -> dict | None:
    """Extract a playable reel from a reddit post dict."""
    media = None
    if p.get("is_video") and p.get("media", {}).get("reddit_video"):
        media = p["media"]["reddit_video"]
    cp = p.get("crosspost_parent_list") or []
    if not media and cp and cp[0].get("is_video") and cp[0].get("media", {}).get("reddit_video"):
        media = cp[0]["media"]["reddit_video"]
        p = cp[0]
    if not media:
        return None
    poster = None
    try:
        poster = (p.get("preview", {}).get("images", [{}])[0].get("source", {}).get("url") or "").replace("&amp;", "&") or None
    except Exception:
        poster = None
    return {
        "id": p.get("id"),
        "title": p.get("title"),
        "subreddit": p.get("subreddit"),
        "video": media.get("hls_url") or media.get("dash_url") or media.get("fallback_url", "").split("?")[0],
        "poster": poster or p.get("thumbnail") or None,
        "duration": media.get("duration") or 0,
        "width": media.get("width") or 0,
        "height": media.get("height") or 0,
        "ups": p.get("ups") or 0,
        "num_comments": p.get("num_comments") or 0,
        "url": "https://www.reddit.com" + (p.get("permalink") or ""),
        "created": p.get("created_utc") or 0,
    }


@app.route("/api/xreels")
def api_xreels():
    """GET /api/xreels?cat=for-you&limit=12 — reddit video reels (kinkgrid-style feed)."""
    cat = (request.args.get("cat") or "for-you").strip().lower()
    limit = max(5, min(25, int(request.args.get("limit") or "12")))
    subs = XREELS_SUBS.get(cat) or XREELS_SUBS["for-you"]
    after = request.args.get("after")

    posts, next_after, debug = [], None, {}
    debug["oauth"] = "configured" if os.environ.get("REDDIT_CLIENT_ID") else "not-configured"
    for sub in subs[:6]:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
        if after:
            url += f"&after={after}"
        data = _reddit_get(url)
        if not data:
            debug[sub] = "blocked/err"
            continue
        children = (data.get("data") or {}).get("children") or []
        debug[sub] = f"ok children={len(children)}"
        for c in children:
            p = c.get("data") or {}
            if not p.get("over_18"):
                continue
            reel = _reel_from_post(p)
            if reel and reel.get("video"):
                posts.append(reel)
        if not next_after and (data.get("data") or {}).get("after"):
            next_after = data["data"]["after"]

    import random
    random.shuffle(posts)
    return _json({"posts": posts[:limit * 2], "after": next_after, "count": len(posts), "source": "reddit", "debug": debug})


# ═══════════════ SoundCloud (RE:music) ═══════════════

SC_API = "https://api-v2.soundcloud.com"
SC_CLIENT_ID = "Pb72ranhoyt6gw7hM7TkzUItXlMWSNSo"


def sc_get(path: str, **params) -> dict | None:
    if cf_requests is None:
        return None
    try:
        r = cf_requests.get(
            f"{SC_API}{path}",
            impersonate="chrome",
            timeout=20,
            headers={"User-Agent": UA, "Accept": "application/json"},
            params={"client_id": SC_CLIENT_ID, "app_locale": "en", **params},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def sc_resolve_stream(track: dict) -> dict | None:
    """Find a playable progressive MP3 (or HLS) stream URL for a track."""
    if cf_requests is None:
        return None
    tc = (track.get("media") or {}).get("transcodings") or []
    if not tc:
        return None
    # 1) progressive mp3 — simplest, plays everywhere
    prog = next((x for x in tc if x.get("format", {}).get("protocol") == "progressive"), None)
    if prog:
        try:
            r = cf_requests.get(
                prog["url"] + f"?client_id={SC_CLIENT_ID}",
                impersonate="chrome",
                timeout=15,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("url"):
                    return {
                        "url": d["url"],
                        "mimeType": "audio/mpeg",
                        "protocol": "progressive",
                    }
        except Exception:
            pass
    # 2) HLS (mp4 audio)
    hls = next((x for x in tc if x.get("format", {}).get("protocol") == "hls"), None)
    if hls:
        try:
            r = cf_requests.get(
                hls["url"] + f"?client_id={SC_CLIENT_ID}",
                impersonate="chrome",
                timeout=15,
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("url"):
                    return {"url": d["url"], "mimeType": "application/vnd.apple.mpegurl", "protocol": "hls"}
        except Exception:
            pass
    return None


@app.route("/api/sc/search")
def sc_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return _json({"error": "Missing q param", "items": []}), 400
    data = sc_get("/search/tracks", q=q, limit=24, offset=0)
    if not data:
        return _json({"error": "SoundCloud blocked the request", "items": []}), 502
    items = []
    for t in data.get("collection") or []:
        if not t.get("id") or not t.get("title"):
            continue
        art = (t.get("artwork_url") or "").replace("large", "t500x500")
        if not art:
            art = (t.get("user") or {}).get("avatar_url") or ""
        items.append({
            "videoId": f"sc:{t['id']}",
            "title": t.get("title", ""),
            "author": (t.get("user") or {}).get("username", "Unknown"),
            "thumb": art or "",
            "duration": int((t.get("duration") or 0) / 1000),
            "permalink": (t.get("permalink_url") or "") or f"https://soundcloud.com/{(t.get('user') or {}).get('permalink') or 'unknown'}/{t.get('permalink') or t.get('id')}",
        })
    return _json({"items": items, "count": len(items), "source": "soundcloud"})


@app.route("/api/sc/trending")
def sc_trending():
    """SoundCloud trending/charts — top 24 tracks, same shape as /search."""
    data = sc_get("/charts", kind="trending", genre="soundcloud:genres:all-music", limit=24, offset=0)
    if not data:
        return _json({"error": "SoundCloud blocked the request", "items": []}), 502
    items = []
    for t in data.get("collection") or []:
        tr = t.get("track") or t
        if not tr.get("id") or not tr.get("title"):
            continue
        art = (tr.get("artwork_url") or "").replace("large", "t500x500")
        if not art:
            art = (tr.get("user") or {}).get("avatar_url") or ""
        items.append({
            "videoId": f"sc:{tr['id']}",
            "title": tr.get("title", ""),
            "author": (tr.get("user") or {}).get("username", "Unknown"),
            "thumb": art or "",
            "duration": int((tr.get("duration") or 0) / 1000),
            "permalink": (tr.get("permalink_url") or "") or f"https://soundcloud.com/{(tr.get('user') or {}).get('permalink') or 'unknown'}/{tr.get('permalink') or tr.get('id')}",
        })
    return _json({"items": items, "count": len(items), "source": "soundcloud"})


@app.route("/api/sc/stream")
def sc_stream():
    vid = (request.args.get("id") or "").strip()
    if not vid:
        return _json({"error": "Missing id param"}), 400
    sc_id = vid.replace("sc:", "")
    if not sc_id.isdigit():
        return _json({"error": "Invalid id", "id": vid}), 400
    # fetch the track by id
    data = sc_get(f"/tracks/{sc_id}")
    if not data:
        return _json({"error": "Track not found", "id": vid}), 404
    stream = sc_resolve_stream(data)
    if not stream:
        return _json({"error": "No playable stream", "id": vid}), 502
    return _json({
        "id": vid,
        "title": data.get("title", ""),
        "author": (data.get("user") or {}).get("username", ""),
        "permalink": (data.get("permalink_url") or "") or f"https://soundcloud.com/{(data.get('user') or {}).get('permalink') or 'unknown'}/{data.get('permalink') or sc_id}",
        "lengthSeconds": int((data.get("duration") or 0) / 1000),
        "url": stream["url"],
        "mimeType": stream["mimeType"],
        "protocol": stream["protocol"],
    })


@app.route("/api/catalog")
def api_catalog():
    tag = (request.args.get("tag") or "hanime").strip().lower()
    try:
        page = max(1, int(request.args.get("page") or "1"))
    except ValueError:
        page = 1

    page_url = f"{BASE}/tag/{tag}/" if page == 1 else f"{BASE}/tag/{tag}/page/{page}/"
    html = http_get(page_url)
    if not html:
        return _json({"status": False, "error": "Failed to fetch tag page"}), 502

    titles = []
    seen = set()
    card_re = re.compile(
        r'id="manga-item-(\d+)"[\s\S]*?href="https://hentaihaven\.xxx/watch/([^"/]+)/"[^>]*title="([^"]+)"[\s\S]*?<img[^>]*src="([^"]+)"'
    )
    for m in card_re.finditer(html):
        pid, slug, title, poster = m.group(1), m.group(2), m.group(3), m.group(4)
        if slug in seen:
            continue
        seen.add(slug)
        block = html[m.start() : m.start() + 6000]
        eps = set(re.findall(r"watch/[^\"/]+/episode-(\d+)", block))
        titles.append(
            {
                "id": int(pid),
                "title": title.strip(),
                "slug": slug,
                "poster": poster,
                "url": f"{BASE}/watch/{slug}/",
                "episodes": len(eps),
                "episodeNumbers": sorted(int(e) for e in eps),
            }
        )

    if not titles:
        return _json({"status": False, "error": "No entries found (tag may not exist)"}), 404

    lp = re.search(r'page/(\d+)/"[^>]*>\s*»', html)
    total_pages = int(lp.group(1)) if lp else page
    return _json(
        {"status": True, "tag": tag, "page": page, "totalPages": total_pages, "count": len(titles), "titles": titles}
    )


if __name__ == "__main__":
    app.run(port=8787)
