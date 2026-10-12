#!/usr/bin/env python3
"""Verification for the two HARD stream-selection rules in the RD resolver.
Run: python3 _test_stream_rules.py   (from rd-bridge/)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("REALDEBRID_API_KEY", "test-key")
os.environ.setdefault("TMDB_API_KEY", "test-key")

import server as s  # noqa: E402

# ── hermetic probe-verdict cache ────────────────────────────────────
# The candidate probe verdicts are keyed by info_hash and PERSISTED to the very
# file the live bridge reads (.cand_verdict_cache.json). The synthetic
# candidates below carry 6-char hashes ("show.2", "[grp] "), so a run of this
# script would write its own verdicts into that shared cache — and the NEXT run
# would read them back, overriding the filename hints the checks below assert
# on. Isolate the cache: start empty, never load, never save.
s._cand_verdict = {}
s._cand_verdict_load = lambda: None
s._cand_verdict_save = lambda: None

FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def C(name, seeders=1, **kw):
    d = {"name": name, "seeders": seeders, "info_hash": name[:6]}
    d.update(kw)
    return d


# ── cam detection ───────────────────────────────────────────────────
check("cam: HDCAM", s._is_cam_release("Movie.2024.1080p.HDCAM.x264"))
check("cam: CAMRIP", s._is_cam_release("Movie.2024.CAMRIP.XviD"))
check("cam: TELESYNC", s._is_cam_release("Movie 2024 TELESYNC x264"))
check("cam: bare TS token", s._is_cam_release("Movie.2024.TS.XviD-GROUP"))
check("cam: HDTS", s._is_cam_release("Movie.2024.HDTS.720p.x264"))
check("cam: TELECINE", s._is_cam_release("Movie.2024.TELECINE.1080p"))
check("cam: not DTS (audio codec)", not s._is_cam_release("Movie.2024.1080p.DTS.x264"))
check("cam: not a CLEAN web release", not s._is_cam_release("Movie.2024.1080p.WEB-DL.DDP5.1.H264"))
check("cam: not 'TS' inside a word", not s._is_cam_release("KATSUKI.2024.1080p.BluRay"))

# ── height parsing ──────────────────────────────────────────────────
check("height 2160", s._release_height("Movie.2024.2160p.WEB") == 2160)
check("height 4K alias", s._release_height("Movie.2024.4K.HDR") == 2160)
check("height 1080", s._release_height("Movie.2024.1080p.WEB-DL") == 1080)
check("height 720", s._release_height("Movie.2024.720p.HDTV") == 720)
check("height 480", s._release_height("Movie.2024.480p.DVDRip") == 480)
check("height unmarked -> None", s._release_height("Movie.2024.WEB-DL.x264") is None)

# ── RULE 1: never a cam when an HD alternative exists ───────────────
cands = [
    C("Movie.2024.1080p.HDCAM.x264-GROUP", seeders=9999),
    C("Movie.2024.720p.WEB-DL.H264-GROUP", seeders=5),
]
kept = s._filter_cam_when_hd(cands)
check("rule1: cam dropped, HD kept", len(kept) == 1 and not s._is_cam_release(kept[0]["name"]))

order = sorted(cands, key=lambda c: s._candidate_order_key(c, None, None, None))
check("rule1: HD sorts before cam despite 9999 seeders", not s._is_cam_release(order[0]["name"]))

# cam claiming 2160p still loses to a real 480p WEB when HD exists
cands2 = [C("Movie.2024.2160p.HDCAM.x264"), C("Movie.2024.480p.WEB-DL.H264")]
o2 = sorted(cands2, key=lambda c: s._candidate_order_key(c, None, None, None))
check("rule1: fake-2160p cam loses to real 480p WEB", "480p" in o2[0]["name"])

# an unmarked download beats a cam too (ordering, since nothing declares 720p+)
cands2b = [C("Movie.2024.TS.XviD"), C("Movie.2024.WEB-DL.x264")]
o2b1 = sorted(cands2b, key=lambda c: s._candidate_order_key(c, None, None, None))
check("rule1b: unmarked WEB outranks cam", not s._is_cam_release(o2b1[0]["name"]))

# cam-only set is preserved (a cam stream beats no stream)
cam_only = [C("Movie.2024.HDCAM.x264"), C("Movie.2024.TS.XviD")]
k3 = s._filter_cam_when_hd(cam_only)
check("rule1c: cam-only set kept intact", len(k3) == 2)

# 480p cam + 480p WEB (no >=720p anywhere): filter keeps both but the WEB
# must be ordered first, so the cam is still never the selected release.
cands4 = [C("Movie.2024.480p.HDCAM.x264"), C("Movie.2024.480p.WEB-DL.X264")]
k4 = s._filter_cam_when_hd(cands4)
o4 = sorted(cands4, key=lambda c: s._candidate_order_key(c, None, None, None))
check("rule1d: 480p WEB ordered before 480p cam",
      "WEB" in o4[0]["name"].upper() and s._is_cam_release(o4[-1]["name"]))

# ── RULE 2: highest fidelity first ──────────────────────────────────
# …with the 2026-10-11 caveat: for a plain browser ask a 4K HEVC/x265 must NOT
# outrank a 1080p H.264 (the measured wrong-aspect / implausible-size class,
# incl. Micro4K upscales). Plausibility is a PRIMARY key element now, so the
# 2160p HEVC release here is demoted below every H.264 one; among the H.264
# releases fidelity still decides.
mix = [
    C("Movie.2024.720p.WEB-DL.H264"),
    C("Movie.2024.480p.DVDRip.XviD"),
    C("Movie.2024.1080p.WEB-DL.H264"),
    C("Movie.2024.2160p.WEB-DL.HEVC"),
    C("Movie.2024.WEB-DL.x264"),  # unmarked
]
om = sorted(mix, key=lambda c: s._candidate_order_key(c, None, None, None))
order_names = [c["name"] for c in om]
check("rule2: 1080p H.264 first (4K HEVC demoted)", "1080p" in order_names[0])
check("rule2: 720p second", "720p" in order_names[1])
check("rule2: 480p third", "480p" in order_names[2])
check("rule2: unmarked fourth (no cam present)",
      "WEB-DL.x264" in order_names[3] and order_names[3].endswith("x264"))
check("rule2: 4K HEVC/2160p LAST for a plain browser ask", "2160p" in order_names[4])

# 4K H.264 (a browser CAN decode it) still wins on fidelity — the demotion is
# about the CODEC + height, not about 4K per se.
mix_h264_4k = [
    C("Movie.2024.2160p.WEB-DL.H264", seeders=1, size_gb=9.0),
    C("Movie.2024.1080p.WEB-DL.H264", seeders=5000, size_gb=3.0),
]
o4k = sorted(mix_h264_4k, key=lambda c: s._candidate_order_key(c, None, None, None))
check("quality: 4K H.264 still outranks 1080p H.264 (no over-demotion)",
      "2160p" in o4k[0]["name"])

# A "2160p" release whose size is implausible for 2160p (a Micro4K-style
# upscale) must lose to a plausible 1080p even though its name says 4K.
upscale = [
    C("Movie.2024.2160p.WEB-DL.H265.Micro4K", size_gb=2.0),
    C("Movie.2024.1080p.WEB-DL.H264", size_gb=5.0),
]
ous = sorted(upscale, key=lambda c: s._candidate_order_key(c, None, None, None))
check("quality: Micro4K upscale loses to a plausible 1080p", "1080p" in ous[0]["name"])
check("sanity: implausible size flagged below the 2160p threshold",
      s._res_size_sanity(2160, 2.0) == 2 and s._res_size_sanity(2160, 12.0) == 0)

# cam is a floor: it must never appear above ANY non-cam, whatever the tokens
mix2 = [C("Movie.2024.2160p.HDCAM.HEVC"), C("Movie.2024.480p.WEB-DL.H264")]
o2b = sorted(mix2, key=lambda c: s._candidate_order_key(c, None, None, None))
check("rule2: cam stays last under ordering", s._is_cam_release(o2b[-1]["name"]))

# ordering is total & stable (no exceptions) with sparse candidate dicts
try:
    s._candidate_order_key({}, None, None, "h264")
    check("order key tolerates empty candidate", True)
except Exception as e:  # noqa: BLE001
    check(f"order key tolerates empty candidate ({e})", False)

# ── browser-aware rule 2: Chrome strict, Firefox 1080p-first ────────
ladder = [
    C("Movie.2024.480p.WEB-DL.H264"),
    C("Movie.2024.720p.WEB-DL.H264"),
    C("Movie.2024.1080p.WEB-DL.H264"),
    C("Movie.2024.1440p.WEB-DL.H264"),
    C("Movie.2024.2160p.WEB-DL.H264"),
    C("Movie.2024.WEB-DL.x264"),  # unmarked
]

# Chrome (no codec param): unchanged strict highest-quality-first
oc = [c["name"] for c in sorted(ladder, key=lambda c: s._candidate_order_key(c, None, None, None))]
check("chrome: 2160 first", "2160p" in oc[0])
check("chrome: 1440 second (strict order intact)", "1440p" in oc[1])
check("chrome: 1080 third", "1080p" in oc[2])
check("chrome: unmarked last", oc[-1].endswith("x264") and "p." not in oc[-1].split("WEB-DL")[0])

# Firefox (codec="h264"): 1080p is the top pick for playback speed, then the
# lighter 720p ahead of the equally-distant but heavier 1440p.
of = [c["name"] for c in sorted(ladder, key=lambda c: s._candidate_order_key(c, None, None, "h264"))]
check("firefox: 1080p is the top pick", "1080p" in of[0])
check("firefox: 2160p NOT first", "2160p" not in of[0])
check("firefox: 720p next after 1080p", "720p" in of[1])
check("firefox: 1440p after 720p (lighter wins tie)", "1440p" in of[2])
check("firefox: 2160p last among explicit heights",
      next(i for i, n in enumerate(of) if "2160p" in n) >
      next(i for i, n in enumerate(of) if "480p" in n))
check("firefox: unmarked below every explicit height",
      of.index([n for n in of if n.endswith("x264") and "p." not in n.split("WEB-DL")[0]][0]) == len(of) - 1)

# Firefox keeps the cam floor: a cam never outranks a real release
fcam = [C("Movie.2024.2160p.HDCAM.x264", seeders=99999), C("Movie.2024.1080p.WEB-DL.H264")]
ofc = sorted(fcam, key=lambda c: s._candidate_order_key(c, None, None, "h264"))
check("firefox: cam stays last despite 2160p+99999 seeders", s._is_cam_release(ofc[-1]["name"]))

# Firefox prefers 1080p over a 2160p release even when 2160p is far better seeded
fseed = [C("Movie.2024.2160p.WEB-DL.H264", seeders=5000), C("Movie.2024.1080p.WEB-DL.H264", seeders=2)]
ofs = sorted(fseed, key=lambda c: s._candidate_order_key(c, None, None, "h264"))
check("firefox: 1080p beats a 5000-seeder 2160p", "1080p" in ofs[0]["name"])

# and Chrome still takes that 2160p (strict order preserved)
ocs = sorted(fseed, key=lambda c: s._candidate_order_key(c, None, None, None))
check("chrome: 2160p still wins with strict order", "2160p" in ocs[0]["name"])

# Firefox honours an EXPLICIT quality pick as the sweet spot (the player's
# 1080/720/480 switch must never be outranked by the 1080p default)…
of720 = [c["name"] for c in sorted(ladder, key=lambda c: s._candidate_order_key(c, 720, None, "h264"))]
check("firefox: explicit quality=720 puts 720p top", "720p" in of720[0])
of480 = [c["name"] for c in sorted(ladder, key=lambda c: s._candidate_order_key(c, 480, None, "h264"))]
check("firefox: explicit quality=480 puts 480p top", "480p" in of480[0])
# …while Chrome stays strictly highest-quality-first even with an explicit pick
oc480 = [c["name"] for c in sorted(ladder, key=lambda c: s._candidate_order_key(c, 480, None, None))]
check("chrome: explicit quality=480 does NOT override strict order", "2160p" in oc480[0])

# ── RULE 3: a release that PLAYS AS-IS is preferred over a remux ────
# MEASURED: Mushoku Tensei S1E1 resolved to a [Opus 2.0][AV1] dual-audio
# release whose first track is Japanese → the English ask paid the ffmpeg
# remux: TTFB 22.2s, no first byte in 150s → "no stream available".
check("fit: dual-audio name is a remux risk", s._audio_first_rank(C("[Grp] Show S01 [Dual-Audio] [1080p]"), ("eng",)) == 2)
check("fit: multi-audio name is a remux risk", s._audio_first_rank(C("[Grp] Show S01 (Multi-Audio) 1080p"), ("eng",)) == 2)
check("fit: Japanese release is a remux risk for an English ask",
      s._audio_first_rank(C("[Grp] Show S01 [Japanese] 1080p"), ("eng",)) == 2)
check("fit: English dub name plays as-is",
      s._audio_first_rank(C("[Grp] Show S01 [English Dub] [WEB-DL]"), ("eng",)) == 0)
check("fit: unmarked name is neutral (unknown)",
      s._audio_first_rank(C("[Grp] Show S01 [1080p]"), ("eng",)) == 1)
check("fit: multi-SUBS is not read as multi-audio",
      s._audio_first_rank(C("Show.2024.1080p.MULTi.Subs.AAC.x264"), ("eng",)) == 1)
check("fit: foreign-only release is never tried",
      s._audio_first_rank(C("Show.2024.1080p.SPANISH.DUBBED"), ("eng",)) == 3)
check("fit: anime (jpn accepted) demotes nothing for being JPN-first",
      s._audio_first_rank(C("[Grp] Show S01 [Dual-Audio] 1080p"), ("jpn", "eng")) == 0)

# a fitting release outranks a higher-quality one that would need the remux
# (both H.264 so the check isolates the AUDIO-FIT rule from the codec bound)
fitmix = [
    C("Show.2024.1080p.WEB-DL.H264.Dual-Audio-GROUP", seeders=999),
    C("Show.2024.720p.WEB-DL.H264.English-Dub-GROUP", seeders=1),
]
ofit = sorted(fitmix, key=lambda c: s._candidate_order_key(c, None, None, None, ("eng",)))
check("fit: 720p that plays beats 1080p that needs a remux", "Dub" in ofit[0]["name"])
# …but for an ANIME request (jpn accepted) the dual-audio release is fine, so
# the quality rule is untouched: 1080p still comes first.
ofit_anime = sorted(fitmix, key=lambda c: s._candidate_order_key(c, None, None, None, ("jpn", "eng")))
check("fit: anime keeps quality-first (dual-audio 1080p still first)", "1080p" in ofit_anime[0]["name"])

# ── codec-capability bound (measured 2026-10-11) ────────────────────
# A release whose NAME declares a codec the asking browser cannot decode is
# demoted below every decodable release. It BOUNDS quality exactly like the
# audio-fit bound above, and it is a DEMOTION, never a filter (an HEVC-only
# title still resolves). Causes, both measured: the top-14 candidates for
# MobLand S02E01 were all 2160p HEVC written as "H 265" — a spaced form
# _is_hevc_name used to miss — and The Office S01E01's #1 candidate was an x265
# HEVC release; the player (Chromium has no hvc1) then failed to decode the
# stream it was handed ("loads then fails").
check("codec: spaced 'H 265' is detected as HEVC", s._is_hevc_name("Show.2024.2160p.H 265-GRP"))
check("codec: 'H.265' / 'H-265' / 'H_265' are detected", all(
    s._is_hevc_name(x) for x in ("Movie.2024.H.265", "Movie.2024.H-265", "Movie.2024.H_265")))
check("codec: H.264 (spaced or glued) is not HEVC", not any(
    s._is_hevc_name(x) for x in ("Movie.2024.1080p.H.264.x264", "Movie.2024.1080p.H 264-GRP")))
check("codec: AV1 is not HEVC (it plays natively)", not s._is_hevc_name("Movie.2024.1080p.AV1"))
# an HEVC release never leads a browser ask (default = Chromium: no hvc1)
cx = [C("Show.2024.2160p.WEB-DL.H 265-GROUP", seeders=999),
      C("Show.2024.1080p.WEB-DL.H264-GROUP", seeders=1)]
ocx = sorted(cx, key=lambda c: s._candidate_order_key(c, None, None, None))
check("codec: spaced-HEVC 2160p does NOT outrank a 1080p H264 (default ask)", "H264" in ocx[0]["name"])
# …and a 1080p HEVC does not lead either (the old demotion needed >1080p)
cy = [C("Show.2024.1080p.BluRay.x265.HEVC-GROUP", seeders=999),
      C("Show.2024.1080p.WEB-DL.H264-GROUP", seeders=1)]
ocy = sorted(cy, key=lambda c: s._candidate_order_key(c, None, None, None))
check("codec: 1080p HEVC does NOT outrank a 1080p H264 (default ask)", "H264" in ocy[0]["name"])
# codec=h264 (declared H.264-only) also demotes AV1/VP9 …
cz = [C("Show.2024.1080p.WEB-DL.AV1-GROUP", seeders=999),
      C("Show.2024.1080p.WEB-DL.H264-GROUP", seeders=1)]
ocz = sorted(cz, key=lambda c: s._candidate_order_key(c, None, None, "h264"))
check("codec: AV1 demoted for the h264-only ask", "H264" in ocz[0]["name"])
# …but the default ask keeps AV1 (Firefox/Chrome/Edge decode it natively)
ocd = sorted(cz, key=lambda c: s._candidate_order_key(c, None, None, None))
check("codec: AV1 kept for the default ask", "AV1" in ocd[0]["name"])
# demotion, not a filter: an HEVC-only set still orders fine
oe = sorted([C("Show.2024.1080p.x265.HEVC-A"), C("Show.2024.720p.x265.HEVC-B")],
            key=lambda c: s._candidate_order_key(c, None, None, None))
check("codec: HEVC-only set still resolves (no filter)", len(oe) == 2 and "1080p" in oe[0]["name"])

# a PROBED verdict outranks the filename hint (the name may not say it)
probed = C("Show.2024.1080p.WEB-DL.x265-GROUP")
s._cand_verdict_set(probed, ("eng",), "remux")
check("fit: probe verdict (remux) outranks a neutral name", s._audio_first_rank(probed, ("eng",)) == 2)
probed2 = C("[Grp] Show S01 [Dual-Audio] 1080p")
s._cand_verdict_set(probed2, ("eng",), "fits")
check("fit: probe verdict (fits) outranks a 'dual-audio' name", s._audio_first_rank(probed2, ("eng",)) == 0)
s._cand_verdict_set(probed2, ("eng",), "reject")
check("fit: probe verdict (reject) is never tried again", s._audio_first_rank(probed2, ("eng",)) == 3)

# ── DETERMINISM: identical request → identical pick ─────────────────
# MEASURED 2026-10-11: 182 candidates produced only 170 distinct order keys —
# 11 tie groups whose order was decided by SOURCE (fetch arrival) order, so the
# same request could return a different release run to run. info_hash is now
# the FINAL key element, so keys are unique and the order is reproducible.
import random as _rnd
tie_set = [C(f"Show.2024.1080p.WEB-DL.H264-GRP{i}", seeders=100) for i in range(12)]
for _i, _c in enumerate(tie_set):  # unique, stable hashes: the ONLY difference
    _c["info_hash"] = f"{_i:040x}"
tie_set[1]["info_hash"] = tie_set[1]["info_hash"].upper()  # case-insensitive
_keys = [s._candidate_order_key(c, None, None, None) for c in tie_set]
check("determinism: keys are unique even for otherwise-identical candidates",
      len(set(_keys)) == len(_keys))
check("determinism: final key element is the lowercased info_hash",
      all(k[-1] == (c.get("info_hash") or "").lower() for k, c in zip(_keys, tie_set)))
_base = [c["info_hash"] for c in sorted(tie_set, key=lambda c: s._candidate_order_key(c, None, None, None))]
_stable = True
for _ in range(8):
    _shuf = tie_set[:]
    _rnd.shuffle(_shuf)
    if [c["info_hash"] for c in sorted(_shuf, key=lambda c: s._candidate_order_key(c, None, None, None))] != _base:
        _stable = False
        break
check("determinism: same set shuffled 8x → identical order", _stable)

# ── AUDIO: a bare ENG token next to a foreign majority is NOT proof ──
# MEASURED bug: '…HDR.DV8.2160p.3xUkr.Eng' scored as PROVEN-English-first
# (rank 0) although the name declares three Ukrainian tracks.
check("audio: bare ENG + foreign majority is NOT proven-English-first",
      s._audio_first_rank(C("Obsession.(2025).WEB-DL.HDR.DV8.2160p.3xUkr.Eng"), ("eng",)) == 1)
check("audio: unopposed bare ENG still proves English-first",
      s._audio_first_rank(C("Movie.2024.1080p.WEB-DL.H264.Eng"), ("eng",)) == 0)
check("audio: unopposed bare .ENG tag still proves English-first",
      s._audio_first_rank(C("Movie.2024.1080p.WEB-DL.ENG"), ("eng",)) == 0)
check("audio: bare ENG + spelled-out foreign language is not proof",
      s._audio_first_rank(C("Movie.2024.1080p.WEB-DL.SPANISH.Eng"), ("eng",)) == 1)
check("audio: a spelled-out English declaration survives a foreign tag",
      s._audio_first_rank(C("Movie.2024.1080p.SPANISH.ENGLISH.Dub"), ("eng",)) == 0)
# untagged/und can never be rank 0 (proven) — the probe reports "unknown"
_untagged = C("Show.2024.1080p.WEB-DL")
s._cand_verdict_set(_untagged, ("eng",), "unknown")
check("audio: probe-untagged is rank 1 (unconfirmed), never 'proven'",
      s._audio_first_rank(_untagged, ("eng",)) == 1)

# ── RD 451: reported distinctly, never as "try again in a few minutes" ──
import urllib.error as _ue
check("rd451: _is_451 detects HTTP 451",
      s._is_451(_ue.HTTPError("u", 451, "Unavailable For Legal Reasons", None, None)) is True)
check("rd451: _is_451 ignores other HTTP errors",
      s._is_451(_ue.HTTPError("u", 403, "Forbidden", None, None)) is False)
check("rd451: _is_451 ignores non-HTTP exceptions", s._is_451(RuntimeError("x")) is False)
_saved_stats = dict(s._rd_block_stats)
s._rd_block_stats["adds"] = 5
s._rd_block_stats["n451"] = 5
_err = s._rd_add_and_stream_error("tt33764258", 0, 0)
check("rd451: every fresh add 451 → distinct 'blocked by the provider' error",
      bool(_err) and _err.get("blocked") is True
      and "451" in _err.get("error", "") and "try again" not in _err.get("error", ""))
s._rd_block_stats["n451"] = 2
check("rd451: a partial block does NOT claim the whole title is blocked",
      s._rd_add_and_stream_error("tt33764258", 0, 0) is None)
s._rd_block_stats.update(_saved_stats)

# ── CAM WARNING FLAG: the player must warn, never block ─────────────
# _tag_cam marks the resolve result when the SELECTED release is a cam so the
# player can raise its warning popup. It must only ever fire on a real cam
# marker, and must never fire on a clean HD release.
camres = {"url": "https://cdn.example/Movie.2024.1080p.HDCAM.x264-GROUP.mkv",
          "title": "Movie.2024.1080p.HDCAM.x264-GROUP"}
s._tag_cam(camres)
check("cam flag: set for a cam release name", camres.get("cam") is True)

cleanres = {"url": "https://cdn.example/Movie.2024.1080p.WEB-DL.DDP5.1.H264-GROUP.mkv",
            "title": "Movie.2024.1080p.WEB-DL.DDP5.1.H264-GROUP"}
s._tag_cam(cleanres)
check("cam flag: NOT set for a clean HD release", "cam" not in cleanres)

# the filename inside the RD URL is the fallback signal (account-library path
# carries a title-only match, so the stored file's own name decides)
urlonly = {"url": "https://cdn.example/Movie.2024.TELESYNC.XviD-AVID.mkv",
           "title": "Some Movie (from account)"}
s._tag_cam(urlonly)
check("cam flag: set from the RD URL filename when the title proves nothing",
      urlonly.get("cam") is True)

# a legit DTS audio codec must NOT trip the cam flag (the tokeniser trap)
dtsres = {"url": "https://cdn.example/Movie.2024.1080p.BluRay.DTS.x264-GROUP.mkv",
          "title": "Movie.2024.1080p.BluRay.DTS.x264-GROUP"}
s._tag_cam(dtsres)
check("cam flag: DTS audio codec is not a cam", "cam" not in dtsres)

# a resolve with no url is never tagged
s._tag_cam({"error": "no stream available"})
check("cam flag: no-url result untouched", True)

# ── CONTENT IDENTITY GATE (title / season / episode / duration) ─────
# MEASURED 2026-10-11: "Rick and Morty s8e7 it plays a movie". The release the
# resolver served was a FAKE ThePirateBay upload named
# "Rick and Morty S08E07 1080p WEB H264-SuccessfulCrab" whose single .mkv ran
# 1h59m45s / 1.5 GB — a two-hour movie wearing an episode's filename. Every
# gate the resolver had was NAME-based, so it passed. These checks are the ones
# that can see through it. Both TMDB helpers and file_duration are stubbed so
# the test stays hermetic (no network, no ffprobe).
s._tmdb_meta = lambda tmdb, mtype, season=None, episode=None: {
    "runtime": {"movie": 97}.get(mtype, 23), "title": "Ricker than Fiction"}
s._tmdb_titles = lambda tmdb, mtype: ["Rick and Morty"]

FAKE_URL = ("https://mia5-4.download.real-debrid.com/d/JUVMJS7GSEXLW133/"
            "Rick%20and%20Morty%20S08E07%201080p%20WEB%20H264-SuccessfulCrab.mkv")
GOOD_URL = ("https://x.download.real-debrid.com/d/AAAAAAAAAAAA/"
            "Rick.and.Morty.S08E07.Ricker.than.Fiction.1080p.WEB.H264.mkv")

# episode-number parsing
check("ep: S08E07 parsed", s._ep_pairs("Rick.and.Morty.S08E07.1080p") == {(8, 7)})
check("ep: s1e2 parsed", s._ep_pairs("show.s1e2.mkv") == {(1, 2)})
check("ep: 1x03 parsed", s._ep_pairs("Show.Name.1x03.HDTV") == {(1, 3)})
check("ep: no marker -> empty", s._ep_pairs("Rick.and.Morty.2025.1080p") == set())

# identity: the wrong MEDIUM (a movie for an episode request) — the title of the
# fake still said "S08E07", so only the runtime can catch it (next block); the
# identity check catches the inverse (an episode file for a film request).
check("identity: movie request + episode file rejected",
      s.identity_mismatch(GOOD_URL, 999, "movie", None, None, names=["Some Film"]) is not None)
check("identity: wrong episode number rejected",
      s.identity_mismatch("https://x/d/A/Show.S08E06.1080p.mkv", 60625, "tv", 8, 7) is not None)
check("identity: right episode accepted",
      s.identity_mismatch(GOOD_URL, 60625, "tv", 8, 7) is None)
check("identity: a different title rejected",
      s.identity_mismatch("https://x/d/A/Oppenheimer.2023.1080p.WEB.Bluray-CriterionEdition.mkv",
                          999, "movie", None, None, names=["Some Film"]) is not None)
check("identity: episode-title-only name is exempt",
      s.identity_mismatch("https://x/d/A/S08E07.Ricker.than.Fiction.mkv",
                          60625, "tv", 8, 7) is None)
check("identity: unparseable name fails open",
      s.identity_mismatch("https://x/d/A/1080p.mkv", 60625, "tv", 8, 7) is None)

# duration: the actual measured numbers. TMDB says S08E07 is 23 min.
s.file_duration = lambda url: 7185.792          # the fake: 1h59m45s
check("duration: 120-min file for a 23-min episode REJECTED",
      s.duration_mismatch(FAKE_URL, 60625, "tv", 8, 7) is not None)
s.file_duration = lambda url: 1380.0            # a real 23-min episode
check("duration: real 23-min episode accepted",
      s.duration_mismatch(GOOD_URL, 60625, "tv", 8, 7) is None)
s.file_duration = lambda url: 2700.0            # a 45-min double episode
check("duration: double-episode (2x) still accepted",
      s.duration_mismatch(GOOD_URL, 60625, "tv", 8, 7) is None)
s.file_duration = lambda url: 1300.0            # a 22-min file
check("duration: short file for a 97-min film REJECTED",
      s.duration_mismatch(GOOD_URL, 999, "movie") is not None)
s.file_duration = lambda url: 6000.0            # a 100-min film
check("duration: real 100-min film accepted",
      s.duration_mismatch(GOOD_URL, 999, "movie") is None)
s.file_duration = lambda url: None
check("duration: unprobeable file fails open",
      s.duration_mismatch(FAKE_URL, 60625, "tv", 8, 7) is None)

# content_gate: the exact reported case, end to end
s.file_duration = lambda url: 7185.792
reason = s.content_gate(FAKE_URL, 60625, "tv", 8, 7)
check("gate: the measured 'S08E07 is a movie' file is rejected",
      reason is not None and "min" in reason)
s.file_duration = lambda url: 1380.0
check("gate: the real S08E07 file passes", s.content_gate(GOOD_URL, 60625, "tv", 8, 7) is None)

# spam/repack bundles (the file list of the measured fake)
FAKE_FILES = [{"path": "/Thepiratebay new Version!!.txt", "bytes": 0},
              {"path": "/Rick and Morty S08E07 1080p WEB H264-SuccessfulCrab.mkv"},
              {"path": "/Downloaded from thepirateheaven.org.txt", "bytes": 479}]
CLEAN_FILES = [{"path": "/Rick.and.Morty.S08E07.1080p.mkv"},
               {"path": "/Rick.and.Morty.S08E07.1080p.srt"},
               {"path": "/Rick.and.Morty.S08E07.1080p.nfo"}]
check("fake: the measured spam bundle detected", s.fake_upload(FAKE_FILES))
check("fake: a clean release file list passes", not s.fake_upload(CLEAN_FILES))
check("fake: installer extension detected", s.fake_upload(["/movie.mkv", "/setup.exe"]))
check("fake: a release-site DOMAIN in the name is NOT spam",
      not s.fake_upload(["/www.UIndex.org - Rick and Morty S08E08 1080p.mkv"]))
check("fake: empty file list passes", not s.fake_upload([]))

# url_matches_type: the exact-episode bound
check("type: right episode passes", s.url_matches_type(GOOD_URL, "tv", 8, 7))
check("type: wrong episode number fails", not s.url_matches_type(
    "https://x/d/A/Show.S08E06.1080p.mkv", "tv", 8, 7))
check("type: a movie for a tv request fails", not s.url_matches_type(
    "https://x/d/A/Some.Movie.2025.1080p.mkv", "tv", 8, 7))
check("type: an episode for a movie request fails", not s.url_matches_type(GOOD_URL, "movie"))
check("type: a plain movie passes", s.url_matches_type(
    "https://x/d/A/Some.Movie.2025.1080p.mkv", "movie"))

print()
if FAILED:
    print(f"{len(FAILED)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
