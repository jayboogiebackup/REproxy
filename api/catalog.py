# -*- coding: utf-8 -*-
"""GET /api/catalog?tag=hanime&page=1 -> { status, tag, page, totalPages, count, titles[] }"""
import re

from _lib import parse_request, json_resp, err, cors_headers, http_get, BASE


def handler(request):
    if parse_request(request)[2] == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers(), "body": ""}

    _, query, _ = parse_request(request)
    tag = (query.get("tag") or "hanime").strip().lower()
    try:
        page = max(1, int(query.get("page") or "1"))
    except ValueError:
        page = 1

    page_url = f"{BASE}/tag/{tag}/" if page == 1 else f"{BASE}/tag/{tag}/page/{page}/"
    html = http_get(page_url)
    if not html:
        return err("Failed to fetch tag page", 502)

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
        return err("No entries found (tag may not exist)", 404)

    lp = re.search(r'page/(\d+)/"[^>]*>\s*»', html)
    total_pages = int(lp.group(1)) if lp else page
    return json_resp(
        {"status": True, "tag": tag, "page": page, "totalPages": total_pages, "count": len(titles), "titles": titles}
    )
