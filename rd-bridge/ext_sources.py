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
    r.raise_for_status()
    src = (r.json() or {}).get("src")
    if not src:
        return None
    embed = s.get(f"{VIX_BASE}{src}", timeout=25)
    embed.raise_for_status()
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


def resolve(tmdb, mtype, season=None, episode=None, source="castle",
            info=None, require_english=True, attempts=3):
    """-> {"url","source","audio","rejected":[...]} or {"error":...}

    `info` = {"title","originalTitle","year","originalLanguage"} from TMDB.
    CastleTV is often Hindi-first on major titles, so each candidate's head is
    sniffed and non-English-first picks are skipped rather than served.
    """
    ck = f"{source}|{tmdb}|{mtype}|{season}|{episode}|{int(require_english)}"
    hit = _cache_get(_result_cache, ck, _RESULT_TTL)
    if hit:
        return hit

    if source == "vixsrc":
        master = vixsrc_master(mtype, tmdb, season, episode)
        if not master:
            return {"error": "vixsrc: no master playlist"}
        out = {"url": master, "source": "vixsrc", "audio": "unknown",
               "needs_remux": False, "via": "relay-required",
               "referer": VIX_BASE + "/"}
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
