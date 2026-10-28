#!/usr/bin/env python3
"""
ISO-639 audio-language sniff straight out of MPEG-TS packets.

Port of yapgrid's `eJ()` gate. Reads the head of a stream, walks 188-byte TS
packets, finds the PAT -> PMT, and pulls the language descriptor (tag 0x0A) off
every audio elementary stream. No ffmpeg, no full download, no metadata trust —
which is the whole point: many RD/CASTLE files carry NO audio lang tag in the
container metadata, so a metadata gate goes blind and serves Spanish/Italian.

Audio stream types considered:
    0x03 MPEG-1 audio   0x04 MPEG-2 audio   0x0F AAC (ADTS)
    0x11 AAC (LATM)     0x81 AC-3           0x87 DTS

Verdict rule (deliberately conservative, same as theirs):
    * fewer than 2 audio tracks -> PASS (nothing to choose, can't do better)
    * 2+ tracks                 -> first track must be a wanted language
"""

import re

TS_PACKET = 188
SYNC = 0x47
TS_HEADER = 4
MAX_READ = 32768

AUDIO_STREAM_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}

# ISO-639-2 -> the codes a wanted-language list may use
LANG_ALIASES = {
    "en": {"eng"}, "hi": {"hin"}, "ta": {"tam"}, "te": {"tel"}, "ml": {"mal"},
    "kn": {"kan"}, "bn": {"ben"}, "mr": {"mar"}, "pa": {"pan"}, "ko": {"kor"},
    "ja": {"jpn"}, "zh": {"chi", "zho"}, "cn": {"chi", "zho"}, "es": {"spa"},
    "fr": {"fre", "fra"}, "de": {"ger", "deu"}, "it": {"ita"}, "pt": {"por"},
    "ru": {"rus"}, "tr": {"tur"}, "ar": {"ara"}, "th": {"tha"}, "id": {"ind"},
    "tl": {"tgl", "fil"}, "sq": {"alb", "sqi"}, "nl": {"dut", "nld"},
    "pl": {"pol"}, "sv": {"swe"}, "da": {"dan"}, "no": {"nor"}, "nb": {"nob"},
    "fi": {"fin"}, "cs": {"cze", "ces"}, "el": {"gre", "ell"}, "he": {"heb"},
    "iw": {"heb"}, "hu": {"hun"}, "ro": {"rum", "ron"}, "uk": {"ukr"},
    "vi": {"vie"}, "ms": {"may", "msa"}, "is": {"ice", "isl"}, "fa": {"per", "fas"},
}


def _parse_audio_langs(buf):
    """Return the ordered list of ISO-639 codes found on audio ES entries."""
    pmt_pid = -1
    n = len(buf)

    for off in range(0, n - TS_PACKET + 1, TS_PACKET):
        if buf[off] != SYNC:
            continue
        b1, b2, b3 = buf[off + 1], buf[off + 2], buf[off + 3]
        pid = ((b1 & 0x1F) << 8) | b2

        # must have a payload and be a payload-unit start
        if not (b1 & 0x40) or not (b3 & 0x10):
            continue

        p = off + TS_HEADER
        afc = (b3 >> 4) & 0x03          # adaptation_field_control
        if afc & 0x02:                  # adaptation field present
            p += 1 + buf[p]
        if afc == 0 or p >= off + TS_PACKET:
            continue
        p += 1 + buf[p]                 # pointer_field
        if p + 3 > off + TS_PACKET:
            continue

        table_id = buf[p]
        sec_len = ((buf[p + 1] & 0x0F) << 8) | buf[p + 2]
        end = min(p + 3 + sec_len - 4, off + TS_PACKET)
        if end <= p:
            continue

        if pid == 0 and table_id == 0 and pmt_pid < 0:
            # PAT -> first real program's PMT pid
            q = p + 8
            while q + 4 <= end:
                prog = (buf[q] << 8) | buf[q + 1]
                if prog != 0:                       # skip network PID
                    pmt_pid = ((buf[q + 2] & 0x1F) << 8) | buf[q + 3]
                    break
                q += 4

        elif pid == pmt_pid and table_id == 2:
            # PMT -> walk ES entries
            q = p + 12 + (((buf[p + 10] & 0x0F) << 8) | buf[p + 11])
            langs = []
            while q + 5 <= end:
                stream_type = buf[q]
                es_info_len = ((buf[q + 3] & 0x0F) << 8) | buf[q + 4]
                if stream_type in AUDIO_STREAM_TYPES:
                    lang = ""
                    d = q + 5
                    d_end = min(q + 5 + es_info_len, end)
                    while d + 2 <= d_end:
                        tag, dlen = buf[d], buf[d + 1]
                        if tag == 0x0A and dlen >= 3 and d + 4 < d_end:
                            lang = bytes(buf[d + 2:d + 5]).decode(
                                "ascii", "ignore").lower()
                            lang = re.sub(r"[^a-z]", "", lang)
                        d += 2 + dlen
                    langs.append(lang)
                q += 5 + es_info_len
            return langs

    return []


def sniff(buf):
    """-> {"langs": [...], "safe": bool, "reason": str}"""
    langs = _parse_audio_langs(buf)
    if not langs:
        # No PMT/descriptor found. Do NOT block playback on a parse miss.
        return {"langs": [], "safe": True, "reason": "no-ts-audio-descriptor"}
    if len(langs) < 2:
        return {"langs": langs, "safe": True, "reason": f"single-audio({langs[0] or 'untagged'})"}

    first = langs[0]
    if not first:
        return {"langs": langs, "safe": False, "reason": "first-track-untagged"}
    return {"langs": langs, "safe": None, "reason": "needs-language-check"}


def verdict(buf, wanted=("en",), default_when_loud=None):
    """Full gate. Returns (safe: bool, detail: dict).

    Two shapes matter:
      * 1 audio track  -> only accept it when it is untagged ("und") or English.
                          A LONE Hindi/Tamil/Spanish track means the file IS that
                          dub, so it is refused. (yapgrid passed single-track
                          files because they were only choosing which track to
                          play inside their own player; we are deciding whether
                          to serve the file at all, so this has to be strict.)
      * 2+ audio tracks -> the FIRST one is what a browser plays by default, so
                          it must be untagged or English.
    """
    res = sniff(buf)
    langs = res["langs"]
    if not langs:
        # No PMT/descriptor found. Do NOT block playback on a parse miss.
        return True, {**res, "checked": False}

    ok_untagged = {"", "und", "unk", "zxx"}

    if len(langs) == 1:
        first = langs[0]
        ok = first in ok_untagged or first in LANG_ALIASES.get("en", {"eng"})
        return ok, {
            "langs": langs,
            "first": first,
            "checked": True,
            "reason": "single-audio-ok" if ok else f"single-audio-is-{first}",
        }

    allowed = set(ok_untagged)
    for code in wanted:
        allowed |= LANG_ALIASES.get(code.lower(), {code.lower()})

    first = langs[0]
    ok = first in allowed
    return ok, {
        "langs": langs,
        "first": first,
        "allowed": sorted(allowed),
        "checked": True,
        "reason": "first-audio-ok" if ok else f"first-audio-is-{first}",
    }


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read() if len(sys.argv) > 1 else sys.stdin.buffer.read()
    ok, detail = verdict(data[:MAX_READ])
    print("safe:", ok)
    for k, v in detail.items():
        print(f"  {k}: {v}")
