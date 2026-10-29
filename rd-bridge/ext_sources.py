#!/usr/bin/env python3
"""
RE:player external sources — CastleTV (hscow direct CDN) + Vixsrc.

Reverse engineered from yapgrid's /player bundle; see yapgrid-re/FINDINGS.md.

Why CastleTV first: hscow.com signed URLs play DIRECTLY in the browser. No
Referer, no cookie, no proxy hop — so it costs this bridge nothing in bandwidth
and adds no relay latency. Its weakness is that major titles are frequently
Hindi-first, which is exactly what ts_lang gates.

Vixsrc returns a cleaner HLS master (proper LANGUAGE tags, English DEFAULT=YES)
but every one of its URLs needs `Referer: https://vixsrc.to/`, which a browser
cannot forge — so it can only be used through a relay. Kept here for
completeness; castle is the default.

Nothing in here touches Real-Debrid. No paid quota is spent.
"""

import base64
import html as _html
import json
import re
import threading
import time
import urllib.parse

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CASTLE_API = "https://api.hlowb.com"
CASTLE_PKG = "com.external.castle"
CASTLE_QS = "channel=IndiaA&clientType=1&lang=en-US"
CASTLE_SALT = b"T!BgJB"
CASTLE_APK_SIGN = "ED0955EB04E67A1D9F3305B95454FED485261475"

VIX_BASE = "https://vixsrc.to"
VIX_HEADERS = {"Referer": "https://vixsrc.to/", "Origin": "https://vixsrc.to"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# hscow segments are real MPEG-TS wearing a .jpg name; 32KB is plenty to reach
# the PAT/PMT, and a Range request keeps the gate to one small GET.
SNIFF_RANGE = 32768

_lock = threading.Lock()
_key_cache = {"key": None, "at": 0.0}
_result_cache = {}          # "source|tmdb|type|s|e" -> (ts, payload)
_lang_cache = {}            # segment-cache-key -> (ts, verdict)
_RESULT_TTL = 3 * 3600
_LANG_TTL = 12 * 3600


def _sess(headers=None):
    s = requests.Session()
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    s.headers.update(h)
    return s


def _cache_get(store, key, ttl):
    with _lock:
        hit = store.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if hit:
            store.pop(key, None)
    return None


def _cache_put(store, key, val):
    with _lock:
        store[key] = (time.time(), val)


# ───────────────────────────── CastleTV crypto ─────────────────────────────

def castle_key(force=False):
    """material = b64decode(key) || 'T!BgJB';  IV = KEY = material[:16]."""
    if not force:
        c = _cache_get({"_": (_key_cache["at"], _key_cache["key"])}, "_", 1800)
        if c:
            return c
    s = _sess({"Origin": CASTLE_API, "Referer": CASTLE_API + "/"})
    r = s.get(f"{CASTLE_API}/v0.1/system/getSecurityKey/1?{CASTLE_QS}", timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 200 or not isinstance(j.get("data"), str):
        raise RuntimeError(f"castle security key unavailable: {j}")
    mat = base64.b64decode(j["data"]) + CASTLE_SALT
    if len(mat) < 16:
        raise RuntimeError(f"castle short key material {mat!r}")
    k = mat[:16]
    _key_cache.update(key=k, at=time.time())
    return k


def _extract_json(text):
    """Castle trails padding after the JSON body -> brace-match, don't trust PKCS#7."""
    start = text.find("{")
    if start < 0:
        raise RuntimeError("castle: no JSON in payload")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body = text[start:i + 1]
                # bare 16+ digit ints are not valid JSON numbers for our purposes
                body = re.sub(r'([:{[,]\s*)(\d{16,})(?=\s*[,}\]])', r'\1"\2"', body)
                return json.loads(body)
    raise RuntimeError("castle: unterminated JSON")


def castle_decrypt(blob):
    key = castle_key()
    d = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    plain = d.update(base64.b64decode(blob)) + d.finalize()
    return _extract_json(plain.decode("utf-8", "replace"))


def castle_call(path, method="GET", body=None, retries=1):
    s = _sess({"Origin": CASTLE_API, "Referer": CASTLE_API + "/"})
    url = path if path.startswith("http") else CASTLE_API + path
    last = None
    for attempt in range(retries + 1):
        try:
            r = s.request(method, url, json=body, timeout=25)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(1.0)
                continue
            r.raise_for_status()
            txt = (r.text or "").strip()
            if not txt:
                raise RuntimeError("castle: empty response")
            try:
                j = json.loads(txt)
                if isinstance(j, dict) and isinstance(j.get("data"), str):
                    return castle_decrypt(j["data"])
                return j
            except json.JSONDecodeError:
                return castle_decrypt(txt)
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.0)
    raise RuntimeError(f"castle call failed: {last}")


# ───────────────────────────── title matching ─────────────────────────────

def _norm(s):
    s = str(s or "")
    s = unicodedata_normalize(s)
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def unicodedata_normalize(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.category(c).startswith("Mn"))


def pick_row(rows, titles, year, mtype):
    """Port of yapgrid's eW(): exact title match beats title+year; movies are
    year-checked so a remake is never served for the original."""
    wanted = [t for t in (_norm(x) for x in titles) if t]
    if not wanted:
        return None
    media_type = 2 if mtype == "movie" else None
    scored = []
    for row in rows or []:
        mt = row.get("movieType")
        try:
            mt = int(mt)
        except (TypeError, ValueError):
            mt = None
        if mtype == "movie":
            if mt != 2:
                continue
        else:
            if mt not in (1, 3, 5):
                continue
        rt = _norm(row.get("title") or row.get("name"))
        if not rt:
            continue
        score = 0
        if rt in wanted:
            score = 3
        else:
            for w in wanted:
                if rt.startswith(w + " ") and re.match(r"^(19|20)\d{2}$", rt[len(w) + 1:]):
                    score = 2
                    break
        if not score:
            continue
        if mtype == "movie" and year:
            pt = row.get("publishTime")
            try:
                ry = time.gmtime(int(pt) / 1000).tm_year if pt else None
            except (TypeError, ValueError, OSError):
                ry = None
            if ry and abs(ry - int(year)) > 1:
                score = 0
        if score:
            scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


def castle_search(title, size=30):
    qs = urllib.parse.urlencode(dict(channel="IndiaA", clientType="1", keyword=title,
                                     lang="en-US", mode="1", packageName=CASTLE_PKG,
                                     page="1", size=str(size)))
    payload = castle_call(f"/film-api/v1.1.0/movie/searchByKeyword?{qs}")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return payload.get("rows") or []


def castle_detail(movie_id):
    p = castle_call(f"/film-api/v1.9.9/movie?{CASTLE_QS}"
                    f"&movieId={movie_id}&packageName={CASTLE_PKG}")
    return p.get("data") if isinstance(p.get("data"), dict) else p


def castle_pick_language(tracks, original_language="en"):
    cands = [t for t in (tracks or []) if t and t.get("languageId") is not None]
    if not cands:
        return None
    orig = str(original_language or "en").lower()

    def rank(t):
        ab = str(t.get("abbreviate") or "").lower()
        nm = str(t.get("languageName") or "").lower()
        if ab == orig or ab == "ost" or nm == "ost" or "original" in nm:
            return 0
        if ab == "en" or nm.startswith("english"):
            return 1
        return 2 if t.get("existIndividualVideo") else 3

    return sorted(cands, key=rank)[0]


def castle_video(series_id, episode_id, language_id=None, resolutions=(3, 2, 1)):
    """movieId = SERIES id, episodeId = episode record id. Both required;
    swapping them returns HTTP 200 with no videoUrl."""
    for res in resolutions:
        body = {
            "mode": "1", "appMarket": "GuanWang", "clientType": "1",
            "woolUser": "false", "apkSignKey": CASTLE_APK_SIGN,
            "androidVersion": "13", "movieId": str(series_id),
            "episodeId": str(episode_id),
        }
        if language_id is not None:
            body["languageId"] = str(language_id)
        body.update({"isNewUser": "true", "resolution": str(res),
                     "packageName": CASTLE_PKG})
        try:
            v = castle_call(f"/film-api/v2.0.1/movie/getVideo2?clientType=1"
                            f"&packageName={CASTLE_PKG}&channel=IndiaA&lang=en-US",
                            "POST", body)
        except Exception:
            continue
        if isinstance(v.get("data"), dict):
            v = v["data"]
        # `videos[]` is metadata only (resolution/size, NO url). videoUrl is the link.
        u = v.get("videoUrl")
        if isinstance(u, str) and u.startswith("https://"):
            return u
    return None


# ───────────────────────────── vixsrc ─────────────────────────────

def vixsrc_master(mtype, tmdb, season=None, episode=None, lang="en"):
    s = _sess(VIX_HEADERS)
    path = (f"/api/tv/{tmdb}/{int(season or 1)}/{int(episode or 1)}"
            if mtype != "movie" else f"/api/movie/{tmdb}")
    r = s.get(f"{VIX_BASE}{path}", timeout=20)
    # A 404 here is normal: vixsrc simply does not carry every title. Raising
    # propagated out of the "repeaks" composite and killed the whole chain, so a
    # missing show must degrade to None, not an exception.
    if r.status_code != 200:
        return None
    src = (r.json() or {}).get("src")
    if not src:
        return None
    embed = s.get(f"{VIX_BASE}{src}", timeout=25)
    if embed.status_code != 200:
        return None
    html = embed.text

    m_url = re.search(r"url:\s*'(https://vixsrc\.to/playlist/\d+)'", html)
    m_tok = re.search(r"'token':\s*'([a-f0-9]+)'", html)
    m_exp = re.search(r"'expires':\s*'(\d+)'", html)
    if not (m_url and m_tok and m_exp):
        return None
    return (f"{m_url.group(1)}?token={m_tok.group(1)}"
            f"&expires={m_exp.group(1)}&h=1&lang={urllib.parse.quote(lang)}")


def vixsrc_audio_rendition(master_text, lang="eng"):
    """Return the explicit audio rendition URI for the wanted language."""
    cur = {}
    for line in master_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA"):
            attrs = dict(re.findall(r'([A-Z-]+)="([^"]*)"', line))
            if attrs.get("TYPE") == "AUDIO":
                if attrs.get("LANGUAGE", "").lower() == lang:
                    return attrs.get("URI")
                cur = attrs
    return None


# ───────────────────────────── resolver ─────────────────────────────

def _sniff_english(seg_url, wanted=("en",)):
    """One ranged GET of the head of a TS segment -> English verdict."""
    ck = seg_url.split("?")[0]
    hit = _cache_get(_lang_cache, ck, _LANG_TTL)
    if hit is not None:
        return hit
    import ts_lang
    s = _sess({"Referer": VIX_BASE + "/"})
    r = s.get(seg_url, headers={"Range": f"bytes=0-{SNIFF_RANGE - 1}"}, timeout=25)
    if r.status_code not in (200, 206) or not r.content:
        return {"safe": True, "reason": "sniff-unavailable", "langs": []}
    ok, detail = ts_lang.verdict(r.content[:SNIFF_RANGE], wanted=wanted)
    detail["safe"] = ok
    _cache_put(_lang_cache, ck, detail)
    return detail


def _first_segment(master_url, timeout=25):
    """Walk master -> variant -> media playlist -> first segment URI."""
    s = _sess(VIX_HEADERS)
    url = master_url
    for _ in range(3):
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        body = r.text
        uris = [l.strip() for l in body.splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
        if not uris:
            return None
        nxt = urllib.parse.urljoin(url, uris[0])
        if "#EXT-X-STREAM-INF" in body:
            url = nxt
            continue
        return nxt
    return None


def _fail(ck, payload):
    """Cache a DETERMINISTIC failure so repeat loads don't re-pay the castle walk.

    Only failures that cannot change on their own are cached: no such title, no
    episodes, and audio that is not English. Transient failures (network, no
    video url) are NOT cached, so a blip never pins a title to the fallback.
    """
    _cache_put(_result_cache, ck, payload)
    return payload


_CACHEABLE_ERRORS = (
    "no matching title",
    "no episodes",
    "not English",
    "only non-English audio",
)


def _cacheable(payload):
    err = str((payload or {}).get("error") or "")
    return any(k in err for k in _CACHEABLE_ERRORS)


# ── anikoto / megaplay ───────────────────────────────────────────────────
# Anime source. No Cloudflare challenge on the resolve chain (unlike miruro),
# 1080p, and a real English DUB track. Media is Referer-gated on megaplay.buzz,
# so the player relays it through /api/replayer/anime.
ANI_BASE = "https://anikototv.to"
ANI_KEY = "i?LMTAx0Q6,:}50U"
ANI_IV = "W0;27ToaUpl_P%'c"


def _ani_pad(s, n):
    b = s.encode()
    return (b + b"\x00" * n)[:n]


def _ani_decrypt(enc):
    import base64 as _b64

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    s = enc.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    ct = _b64.b64decode(s)
    dec = Cipher(algorithms.AES(_ani_pad(ANI_KEY, 32)), modes.CBC(_ani_pad(ANI_IV, 16))).decryptor()
    pt = dec.update(ct) + dec.finalize()
    n = pt[-1]
    body = pt[:-n] if 0 < n <= 16 else pt
    return json.loads(body.decode("utf-8"))


def _ani_norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _ani_tokens(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _ani_extra_ok(name, query):
    """Is `name` just `query` plus benign qualifiers?

    Prevents a different production from passing on a prefix match. Real example:
    query "One Piece" matched "One Piece Film: Red" (extra: film, red) — a
    completely different title that happened to fall within a year of the ask.
    Only season/part/TV/cour style qualifiers are allowed to extend a title.
    """
    q, n = _ani_tokens(query), _ani_tokens(name)
    if not q or n[: len(q)] != q:
        return False
    extra = n[len(q):]
    if not extra:
        return True
    allowed = {"tv", "season", "part", "cour", "final", "st", "nd", "rd", "th", "the", "series"}
    return all(t in allowed or t.isdigit() for t in extra)


def _ani_score(name, year, want_titles, want_year):
    """How confidently does this search hit match what was asked for.

    anikoto is the ONLY source matched by text (castle and vixsrc are keyed on the
    TMDB id, so they cannot mismatch). Accepting a loose substring match is how
    the player ended up opening a different anime than the one clicked, so the
    bar here is deliberately high: exact title, or the same title plus only
    season/part qualifiers. A wrong-era hit is pushed below the threshold.
    """
    n = _ani_norm(name)
    if not n:
        return 0
    best = 0
    for t in want_titles:
        q = _ani_norm(t)
        if not q:
            continue
        if n == q:
            best = max(best, 5)
        elif _ani_extra_ok(name, t):
            best = max(best, 3)
    if best == 0:
        return 0
    if want_year and year:
        best += 3 if abs(int(year) - int(want_year)) <= 1 else -4
    return best


def _attr(tag, name):
    """Read one attribute out of an HTML tag (order-independent)."""
    m = re.search(r'\b' + re.escape(name) + r'="([^"]*)"', tag)
    return m.group(1) if m else None


def _mal_lookup(titles, year=None):
    """Authoritative MAL id for an anime title, via AniList's idMal field.

    Jikan (the usual MAL wrapper) is unreachable from this host — it times out —
    but AniList answers in ~250ms and publishes the MAL id directly. This is the
    identity anchor for anikoto, which is text-searched and therefore always one
    fuzzy match away from serving a different show.
    """
    q = ("query($s:String){Media(search:$s,type:ANIME)"
         "{idMal episodes title{romaji english}}}")
    for t in [x for x in (titles or []) if x][:2]:
        try:
            r = requests.post("https://graphql.anilist.co",
                              json={"query": q, "variables": {"s": t}}, timeout=15)
            if r.status_code != 200:
                continue
            media = (r.json().get("data") or {}).get("Media") or {}
            if media.get("idMal"):
                return {"idMal": media["idMal"], "episodes": media.get("episodes")}
        except Exception:
            continue
    return {}


def anikoto_resolve(titles, season=None, episode=None, prefer="dub", year=None,
                    ep_override=None):
    """TMDB titles -> anikoto search -> megaplay master.m3u8."""
    import time as _t
    s = _sess({"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0.0.0 Safari/537.36"),
               "Referer": ANI_BASE + "/", "X-Requested-With": "XMLHttpRequest"})
    hit = None
    matched = None
    want = [t for t in (titles or []) if t]
    for title in want[:2]:
        try:
            r = s.get(f"{ANI_BASE}/ajax/anime/search", params={"keyword": title}, timeout=20)
            html = ((r.json().get("result") or {}).get("html")) or ""
            best = None
            for href, body in re.findall(r'<a[^>]*class="item"[^>]*href="([^"]+)"(.*?)</a>', html, re.S):
                m = re.search(r'class="name[^"]*"[^>]*>\s*([^<]+)', body)
                # Result titles are HTML-escaped ("Howl&#039;s Moving Castle"), and
                # _ani_norm strips punctuation but keeps DIGITS — so the entity
                # normalised to "howl039smovingcastle" and never matched. Unescape
                # before scoring.
                nm = _html.unescape(m.group(1).strip()) if m else ""
                plain = re.sub(r'<[^>]+>', ' ', body)
                ym = re.search(r'\b(19|20)\d{2}\b', plain)
                yr = int(ym.group(0)) if ym else None
                sc = _ani_score(nm, yr, want, year)
                if best is None or sc > best[0]:
                    best = (sc, href, nm, yr)
            # 5 == exact title, or 3 + 3 for a near-exact title in the right year.
            # Anything below that is a guess and must not be played.
            if best and best[0] >= 5:
                hit = best[1]
                matched = {"title": best[2], "year": best[3], "score": best[0]}
                break
        except Exception:
            continue
    if not hit:
        return {"error": "anikoto: no verified match"}

    try:
        aid = re.search(r'data-anime-id="(\d+)"', s.get(hit, timeout=20).text)
        if not aid:
            return {"error": "anikoto: no anime id"}
        aid = aid.group(1)
        r = s.get(f"{ANI_BASE}/ajax/episode/list/{aid}", timeout=20)
        html = (r.json().get("result") or "")
        eps = []
        # Per-tag, order-independent: the old fixed-order regex silently produced
        # no episodes whenever the site reordered attributes.
        for tag in re.findall(r'<a\b[^>]*>', html):
            ids = _attr(tag, "data-ids")
            num = _attr(tag, "data-num")
            if not ids or not num:
                continue
            eps.append({"num": int(num), "dub": _attr(tag, "data-dub") == "1",
                        "ids": ids, "mal": _attr(tag, "data-mal")})
        if not eps:
            return {"error": "anikoto: no episodes"}
        # ── MAL IDENTITY GATE ────────────────────────────────────────────
        # anikoto is matched by TEXT, so a fuzzy hit can serve a different show.
        # AniList gives the authoritative MAL id for the requested title, and
        # every episode row carries data-mal. If they disagree we refuse instead
        # of playing whatever the search returned. No verification, no playback.
        _mal = _mal_lookup(want, year)
        if not _mal.get("idMal"):
            return {"error": "anikoto: unverified (no MAL id)"}
        _tagged = [e for e in eps if e.get("mal")]
        if _tagged and all(str(e["mal"]) != str(_mal["idMal"]) for e in _tagged):
            return {"error": "anikoto: MAL mismatch (wrong anime %s != %s)"
                             % (_tagged[0]["mal"], _mal["idMal"])}

        # Flat-numbered shows (One Piece: 1..1180) need the ABSOLUTE episode;
        # sending TMDB season 21 episode 1 as "1" played the 1999 pilot.
        want = int(ep_override or episode or 1)
        cand = [e for e in eps if e["num"] == want] or [e for e in eps if e["num"] == 1]
        ep = cand[0]

        r = s.get(f"{ANI_BASE}/ajax/server/list", params={"servers": ep["ids"]}, timeout=20)
        html = (r.json().get("result") or "")
        srv = []
        for grp in re.split(r'data-type="', html):
            t = grp[:3]
            if t not in ("sub", "dub"):
                continue
            # data-sv-id is NOT numeric — values look like "323", "e54", "1a7".
            # Requiring \d+ silently dropped every non-numeric server, which is
            # how Cyberpunk ended up stuck on a 640x480 mirror while the 1080p
            # servers existed right there in the same list.
            for m in re.finditer(
                    r'<li[^>]*data-sv-id="[^"]+"[^>]*data-link-id="([^"]+)"[^>]*>\s*([^<]+)',
                    grp):
                srv.append({"type": t, "link": m.group(1), "name": m.group(2).strip()})
        if not srv:
            return {"error": "anikoto: no servers"}
        # English first: dub when the episode has one, else sub (JP + English subs).
        order = ([x for x in srv if x["type"] == prefer] + [x for x in srv if x["type"] != prefer]
                 if prefer else srv)

        def _rank(sv):
            nm = (sv.get("name") or "").lower()
            if nm.startswith("hd-1"):
                return 0
            if nm.startswith("hd-2"):
                return 1
            if "hd" in nm:
                return 2
            return 3

        order = sorted(order, key=lambda x: (x["type"] != prefer, _rank(x)))

        def _res_of(url):
            """Highest rung in a master playlist, or None when unreadable.

            None (not 0) matters: after several requests the CDN starts answering
            403, and treating that as "0p" made a dead probe look like a real
            measurement and win the comparison.
            """
            import time
            for attempt in range(2):
                try:
                    rr = s.get(url, headers={"Referer": "https://megaplay.buzz/",
                                             "Origin": "https://megaplay.buzz"}, timeout=20)
                    if rr.status_code != 200:
                        import os
                        if os.environ.get("ANI_DEBUG"):
                            print("[anikoto] res_of %s status=%s (retry)" % (
                                url[-28:], rr.status_code), flush=True)
                        time.sleep(0.35)
                        continue
                    hs = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", rr.text)]
                    import os
                    if os.environ.get("ANI_DEBUG"):
                        print("[anikoto] res_of %s status=%s res=%s" % (
                            url[-28:], rr.status_code, hs), flush=True)
                    return max(hs) if hs else None
                except Exception as _e:
                    import os
                    if os.environ.get("ANI_DEBUG"):
                        print("[anikoto] res_of EXC %r" % (_e,), flush=True)
                    time.sleep(0.35)
            return None

        best = None
        for sv in order[:5]:
            try:
                r = s.get(f"{ANI_BASE}/ajax/server", params={"get": sv["link"]}, timeout=20)
                embed = ((r.json().get("result") or {}).get("url")) or ""
                if "megaplay" not in embed:
                    continue
                em = s.get(embed, timeout=20)
                # EPISODE-level id, not anime-level: `data-mediaid` is constant
                # across a series (Apothecary: 643 for ep1/2/5) so it served one
                # file for every episode. `data-realid` differs per episode.
                rid = re.search(r'data-realid="(\d+)"', em.text)
                mid = re.search(r'data-mediaid="(\d+)"', em.text)
                sid = rid or mid
                if not sid:
                    continue
                g = s.get("https://megaplay.buzz/stream/getSources",
                          params={"id": sid.group(1)},
                          headers={"Referer": embed}, timeout=20)
                j = g.json()
                if not j.get("enc"):
                    continue
                inner = _ani_decrypt(j["enc"])
                master = inner.get("file")
                if not master:
                    continue
                h = _res_of(master)
                subs = [t for t in (j.get("tracks") or []) if t.get("kind") == "captions"]
                cand = {"height": h, "type": sv["type"], "url": master, "subs": subs,
                        "name": sv.get("name")}
                # Known resolution beats unknown; unknown still beats nothing.
                if (best is None
                        or (h is not None and (best["height"] is None or h > best["height"]))):
                    best = cand
                if h is not None and h >= 1000:
                    break
            except Exception:
                continue
        if not best:
            return {"error": "anikoto: no master"}
        return {"url": best["url"], "source": "anikoto", "audio_type": best["type"],
                "audio": "eng" if best["type"] == "dub" else "jpn",
                "audio_options": sorted({x["type"] for x in srv}),
                "needs_remux": False, "via": "megaplay",
                "height": best["height"], "server": best.get("name"),
                "subtitles": best["subs"][:12], "anime_id": aid, "matched": matched,
                "anikoto_ep": ep["num"]}
    except Exception as e:
        return {"error": f"anikoto: {str(e)[:80]}"}


def _ani_safe(titles, season, episode, year=None, ep_override=None, prefer=None):
    """anikoto_resolve that never raises, so one dead provider can't break the chain."""
    try:
        return anikoto_resolve(titles, season, episode,
                               prefer=(prefer or "dub"), year=year,
                               ep_override=ep_override) or {"error": "anikoto: empty"}
    except Exception as e:
        return {"error": f"anikoto: {str(e)[:80]}"}


def _safe_resolve(tmdb, mtype, season, episode, source, info, require_english):
    """resolve() that never raises.

    A dead or missing provider is routine (vixsrc 404s on plenty of titles); it
    must degrade to an error dict so the next provider in the chain still runs.
    """
    try:
        return resolve(tmdb, mtype, season, episode, source=source,
                       info=info, require_english=require_english) or {
                           "error": f"{source}: empty result"}
    except Exception as e:  # noqa: BLE001 - deliberately broad
        return {"error": f"{source}: {str(e)[:80]}"}


def resolve(tmdb, mtype, season=None, episode=None, source="castle",
            info=None, require_english=True, attempts=3,
             prefer=None):
    """-> {"url","source","audio","rejected":[...]} or {"error":...}

    `info` = {"title","originalTitle","year","originalLanguage"} from TMDB.
    CastleTV is often Hindi-first on major titles, so each candidate's head is
    sniffed and non-English-first picks are skipped rather than served.
    """
    # prefer MUST be in the key: without it, switching Dub <-> Sub in the player
    # re-served the cached stream from the other audio choice.
    ck = f"{source}|{tmdb}|{mtype}|{season}|{episode}|{int(require_english)}|{prefer or ''}"
    hit = _cache_get(_result_cache, ck, _RESULT_TTL)
    if hit:
        return hit

    # "repeaks" is the player's own multi-server source: one server name in the
    # UI that fans out across the providers underneath it. vixsrc first (real
    # 1080p with an English rendition, DEFAULT=YES, plays straight off the CDN),
    # castle second. Each keeps its own cache entry.
    if source == "repeaks":
        # One source failing must never abort the chain: a missing vixsrc title is
        # routine, and an exception here previously took castle down with it.
        # isAnime is resolved from TMDB genre+language+country (see server.py);
        # originalLanguage alone misses titles TMDB mislabels, e.g. Cyberpunk:
        # Edgerunners is tagged 'en' but is a Japanese production.
        is_anime = bool((info or {}).get("isAnime")) or \
            str((info or {}).get("originalLanguage") or "").lower() == "ja"
        titles = [t for t in ((info or {}).get("title"), (info or {}).get("originalTitle")) if t]

        # ANIME FIRST. vixsrc carries no anime at all (404), and castle's anime is
        # Hindi/Tamil-first and only 720p — those two failing is exactly why every
        # anime episode used to end up on repokapone. anikoto is the one source
        # with an English DUB at 1080p, so for a Japanese-original title it goes
        # to the front of the chain.
        if is_anime:
            ani = _ani_safe(titles, season, episode, (info or {}).get("year"),
                          (info or {}).get("absEpisode"), prefer=prefer)
            if ani.get("url"):
                out = dict(ani, source="repeaks")
                _cache_put(_result_cache, ck, out)
                return out

        first = _safe_resolve(tmdb, mtype, season, episode, "vixsrc", info, require_english)
        if first.get("url"):
            out = dict(first, source="repeaks", via=first.get("via") or "direct")
            _cache_put(_result_cache, ck, out)
            return out
        second = _safe_resolve(tmdb, mtype, season, episode, "castle", info, require_english)
        if second.get("url"):
            out = dict(second, source="repeaks")
            _cache_put(_result_cache, ck, out)
            return out

        # NO anikoto fallback for non-anime. Measured: TMDB 111110 is the
        # LIVE-ACTION One Piece (originalLanguage 'en'), and searching anikoto for
        # it returned "One Piece Film: Red" — a different production entirely.
        # anikoto is an anime catalogue; a non-anime title must never be resolved
        # against it, even as a last resort.
        reasons = [r["error"] for r in (first, second) if r.get("error")]
        return {"error": "; ".join(reasons)[:180] or "repeaks: no stream",
                "rejected": (second.get("rejected") or first.get("rejected") or [])}

    if source == "anikoto":
        return _ani_safe([t for t in ((info or {}).get("title"), (info or {}).get("originalTitle")) if t],
                         season, episode, (info or {}).get("year"))

    if source == "vixsrc":
        master = vixsrc_master(mtype, tmdb, season, episode)
        if not master:
            return {"error": "vixsrc: no master playlist"}
        # Plays DIRECT: measured CORS `*` on the master, every variant and the
        # segment host, with no Referer needed from a residential IP. The only
        # thing that gets blocked is a datacenter IP, which is why this must be
        # played by the browser and never fetched through our edge.
        out = {"url": master, "source": "vixsrc", "audio": "eng",
               "needs_remux": False, "via": "direct"}
        _cache_put(_result_cache, ck, out)
        return out

    # ── castle
    if not info or not info.get("title"):
        return {"error": "castle: title required (TMDB info missing)"}
    titles = [info.get("title"), info.get("originalTitle")]
    rows = castle_search(info["title"])
    if not rows and info.get("originalTitle") and info["originalTitle"] != info["title"]:
        rows = castle_search(info["originalTitle"])
    row = pick_row(rows, titles, info.get("year"), mtype)
    if not row:
        return _fail(ck, {"error": "castle: no matching title"})

    series_id = str(row.get("id") or row.get("redirectId") or "")
    detail = castle_detail(series_id)
    if not isinstance(detail, dict):
        return {"error": "castle: detail unavailable"}
    if season is not None:
        srow = next((x for x in (detail.get("seasons") or [])
                     if x.get("number") == int(season)), None)
        if srow and srow.get("movieId"):
            series_id = str(srow["movieId"])
            detail = castle_detail(series_id)
            if not isinstance(detail, dict):
                return {"error": "castle: season detail unavailable"}

    eps = detail.get("episodes") or []
    if not eps:
        return _fail(ck, {"error": "castle: no episodes"})
    if episode is not None:
        ep = next((x for x in eps if x.get("number") == int(episode)), None)
    else:
        ep = eps[0]
    ep = ep or eps[0]
    ep_id = ep.get("id")
    if not ep_id:
        return {"error": "castle: episode has no id"}

    lang = castle_pick_language(ep.get("tracks"), info.get("originalLanguage") or "en")
    lang_id = lang.get("languageId") if lang else None
    url = castle_video(series_id, ep_id, lang_id)
    if not url:
        return {"error": "castle: no video url"}

    rejected = []
    if require_english:
        seg = None
        try:
            seg = _first_segment(url)
        except Exception:
            seg = None
        if seg:
            v = _sniff_english(seg, wanted=("en",))
            if v.get("safe") is False:
                rejected.append({"url": url, "langs": v.get("langs"),
                                 "reason": v.get("reason")})
                # try the explicit English track if castle exposes one
                eng = None
                for t in (ep.get("tracks") or []):
                    ab = str(t.get("abbreviate") or "").lower()
                    nm = str(t.get("languageName") or "").lower()
                    if ab in ("en", "eng") or nm.startswith("english"):
                        eng = t
                        break
                alt = None
                if eng and eng.get("languageId") != lang_id:
                    alt = castle_video(series_id, ep_id, eng.get("languageId"))
                if alt:
                    seg2 = _first_segment(alt)
                    v2 = _sniff_english(seg2, wanted=("en",)) if seg2 else {"safe": True}
                    if v2.get("safe") is not False:
                        url = alt
                    else:
                        return _fail(ck, {
                            "error": "castle: only non-English audio available",
                            "rejected": rejected + [{"url": alt, "langs": v2.get("langs")}]})
                else:
                    return _fail(ck, {
                        "error": "castle: first audio track is not English",
                        "rejected": rejected})
            out = {"url": url, "source": "castle", "audio": "fits",
                   "needs_remux": False, "via": "direct",
                   "langs": v.get("langs"), "rejected": rejected}
        else:
            out = {"url": url, "source": "castle", "audio": "unknown",
                   "needs_remux": False, "via": "direct", "rejected": rejected}
    else:
        out = {"url": url, "source": "castle", "audio": "unknown",
               "needs_remux": False, "via": "direct", "rejected": rejected}

    _cache_put(_result_cache, ck, out)
    return out


def status():
    return {"sources": ["castle", "vixsrc"],
            "castle_key_cached": bool(_key_cache["key"]),
            "cached_results": len(_result_cache)}
