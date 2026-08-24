# -*- coding: utf-8 -*-
"""GET /api/stream?slug=<watch-slug>&ep=<episode> -> { status, src, sources }"""
from _lib import (
    parse_request,
    json_resp,
    err,
    cors_headers,
    get_player_config,
    get_stream_data,
    BASE,
)


def handler(request):
    if parse_request(request)[2] == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers(), "body": ""}

    _, query, _ = parse_request(request)
    slug = (query.get("slug") or "").strip()
    ep = (query.get("ep") or "1").strip()
    if not slug:
        return err('Missing "slug" query param', 400)

    watch_url = f"{BASE}/watch/{slug}/"
    if ep and ep != "1":
        watch_url = f"{BASE}/watch/{slug}/episode-{ep}/"

    cfg = get_player_config(watch_url)
    if not cfg:
        cfg = get_player_config(f"{BASE}/watch/{slug}/")
    if not cfg:
        return err(f"Could not resolve player config for {slug}", 404)

    stream = get_stream_data(cfg["uri"], cfg["en"], cfg["iv"])
    if not stream:
        return err("Could not fetch stream URL", 502)

    return json_resp(
        {"status": True, "src": stream["src"], "sources": stream["sources"], "slug": slug, "episode": ep}
    )
