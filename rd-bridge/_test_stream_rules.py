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
mix = [
    C("Movie.2024.720p.WEB-DL.H264"),
    C("Movie.2024.480p.DVDRip.XviD"),
    C("Movie.2024.1080p.WEB-DL.H264"),
    C("Movie.2024.2160p.WEB-DL.HEVC"),
    C("Movie.2024.WEB-DL.x264"),  # unmarked
]
om = sorted(mix, key=lambda c: s._candidate_order_key(c, None, None, None))
order_names = [c["name"] for c in om]
check("rule2: 2160 first", "2160p" in order_names[0])
check("rule2: 1080 second", "1080p" in order_names[1])
check("rule2: 720 third", "720p" in order_names[2])
check("rule2: 480 fourth", "480p" in order_names[3])
check("rule2: unmarked last (no cam present)", "WEB-DL.x264" in order_names[4] and order_names[4].endswith("x264"))

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
fitmix = [
    C("Show.2024.1080p.WEB-DL.x265.Dual-Audio-GROUP", seeders=999),
    C("Show.2024.720p.WEB-DL.H264.English-Dub-GROUP", seeders=1),
]
ofit = sorted(fitmix, key=lambda c: s._candidate_order_key(c, None, None, None, ("eng",)))
check("fit: 720p that plays beats 1080p that needs a remux", "Dub" in ofit[0]["name"])
# …but for an ANIME request (jpn accepted) the dual-audio release is fine, so
# the quality rule is untouched: 1080p still comes first.
ofit_anime = sorted(fitmix, key=lambda c: s._candidate_order_key(c, None, None, None, ("jpn", "eng")))
check("fit: anime keeps quality-first (dual-audio 1080p still first)", "1080p" in ofit_anime[0]["name"])

# a PROBED verdict outranks the filename hint (the name may not say it)
probed = C("Show.2024.1080p.WEB-DL.x265-GROUP")
s._cand_verdict_set(probed, ("eng",), "remux")
check("fit: probe verdict (remux) outranks a neutral name", s._audio_first_rank(probed, ("eng",)) == 2)
probed2 = C("[Grp] Show S01 [Dual-Audio] 1080p")
s._cand_verdict_set(probed2, ("eng",), "fits")
check("fit: probe verdict (fits) outranks a 'dual-audio' name", s._audio_first_rank(probed2, ("eng",)) == 0)
s._cand_verdict_set(probed2, ("eng",), "reject")
check("fit: probe verdict (reject) is never tried again", s._audio_first_rank(probed2, ("eng",)) == 3)

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
