# -*- coding: utf-8 -*-
"""GET /api/search?q=<title> -> { status, found, slug, title, url }"""
import re
from urllib.parse import quote

from _lib import parse_request, json_resp, err, cors_headers, http_get, BASE


def handler(request):
    if parse_request(request)[2] == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers(), "body": ""}

    _, query, _ = parse_request(request)
    q = (query.get("q") or "").strip()
    if not q:
        return err('Missing "q" query param', 400)

    html = http_get(f"{BASE}/?s={quote(q, safe='')}")
    if not html:
        return err("Search request failed", 502)

    m = re.search(r'href="https://hentaihaven\.xxx/watch/([^"/]+)/"[^>]*title="([^"]+)"', html)
    if not m:
        return json_resp({"status": False, "found": False})

    return json_resp(
        {"status": True, "found": True, "slug": m.group(1), "title": m.group(2), "url": f"{BASE}/watch/{m.group(1)}/"}
    )
