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
import re
from urllib.parse import quote

from flask import Flask, jsonify, request

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


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    return jsonify(
        {
            "status": True,
            "service": "REproxy",
            "curl_cffi_loaded": cf_requests is not None,
            "curl_cffi_error": _CURL_IMPORT_ERROR,
            "routes": ["/api/catalog", "/api/search", "/api/stream"],
        }
    )


@app.route("/api/stream")
def api_stream():
    slug = (request.args.get("slug") or "").strip()
    ep = (request.args.get("ep") or "1").strip()
    if not slug:
        return jsonify({"status": False, "error": 'Missing "slug" query param'}), 400

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
        return jsonify({"status": False, "error": f"Could not resolve player config for {slug}"}), 404

    stream = get_stream_data(cfg["uri"], cfg["en"], cfg["iv"])
    if not stream:
        return jsonify({"status": False, "error": "Could not fetch stream URL"}), 502

    return jsonify(
        {"status": True, "src": stream["src"], "sources": stream["sources"], "slug": slug, "episode": ep}
    )


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"status": False, "error": 'Missing "q" query param'}), 400

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
        return jsonify({"status": False, "found": False})
    return jsonify(
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


def _reddit_get(url: str) -> dict | None:
    """Fetch reddit JSON with Chrome TLS impersonation (bypasses curl's fingerprint block)."""
    if cf_requests is None:
        return None
    try:
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

    posts, next_after = [], None
    for sub in subs[:6]:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
        if after:
            url += f"&after={after}"
        data = _reddit_get(url)
        if not data:
            continue
        children = (data.get("data") or {}).get("children") or []
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
    return jsonify({"posts": posts[:limit * 2], "after": next_after, "count": len(posts), "source": "reddit"})


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
        return jsonify({"status": False, "error": "Failed to fetch tag page"}), 502

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
        return jsonify({"status": False, "error": "No entries found (tag may not exist)"}), 404

    lp = re.search(r'page/(\d+)/"[^>]*>\s*»', html)
    total_pages = int(lp.group(1)) if lp else page
    return jsonify(
        {"status": True, "tag": tag, "page": page, "totalPages": total_pages, "count": len(titles), "titles": titles}
    )


if __name__ == "__main__":
    app.run(port=8787)
