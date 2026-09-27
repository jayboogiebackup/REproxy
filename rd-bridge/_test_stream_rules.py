#!/usr/bin/env python3
"""Verification for the two HARD stream-selection rules in the RD resolver.
Run: python3 _test_stream_rules.py   (from rd-bridge/)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("REALDEBRID_API_KEY", "test-key")
os.environ.setdefault("TMDB_API_KEY", "test-key")

import server as s  # noqa: E402

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

print()
if FAILED:
    print(f"{len(FAILED)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
