# -*- coding: utf-8 -*-
"""Shared helpers for the REproxy hentaihaven.xxx proxy (Vercel Python runtime)."""
import base64
import json
import re
from urllib.parse import parse_qs, urlparse

try:
    from curl_cffi import requests as cf_requests
except Exception as e:  # keep importable so errors surface as JSON, not a crash
    cf_requests = None
    _CURL_IMPORT_ERROR = str(e)
else:
    _CURL_IMPORT_ERROR = None

BASE = "https://hentaihaven.xxx"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-store",
    }


def json_resp(data, status=200):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", **cors_headers()},
        "body": json.dumps(data),
    }


def err(message, status=500):
    return json_resp({"status": False, "error": message}, status)


def parse_request(request):
    """Return (path, query_dict, method) from a Vercel Python request."""
    raw = getattr(request, "url", None) or ""
    if hasattr(raw, "path"):  # urllib.parse.ParseResult
        path, qs = raw.path, raw.query
    else:
        u = urlparse(str(raw))
        path, qs = u.path, u.query
    query = {k: v[0] for k, v in parse_qs(qs).items()}
    method = (getattr(request, "method", None) or "GET").upper()
    return path, query, method


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
