#!/usr/bin/env python3
"""Diagnostic: show how the candidate ordering differs between a SUB and a DUB
anime request, and how many of the DUB-ordered leaders carry NO accepted audio
(audio_first_rank == 3, i.e. subs-only releases) — those are the candidates the
bounded walk burns its budget on before it ever reaches a real English dub."""
import os, sys, json
os.environ["RD_SKIP_CACHE_PURGE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S

def dump(tmdb, imdb, season, episode, label):
    cands = S.search_torrentio(imdb, "anime", season, episode)
    print(f"\n===== {label}  tmdb={tmdb} imdb={imdb} S{season}E{episode}  torrentio={len(cands)} =====")
    for lang, tag in (("dub", "DUB"), (None, "SUB")):
        accept = S._accept_langs("anime", lang)
        want_h = None
        order = sorted(cands, key=lambda c: S._candidate_order_key(c, want_h, lang, "h264", accept))
        print(f"\n--- lang={lang} accept={accept} (top 15) ---")
        n_reject = 0
        for i, c in enumerate(order):
            afr = S._audio_first_rank(c, accept)
            anr = S._audio_native_rank(c, accept)
            lr = S._lang_rank(c, lang)
            if afr == S._NEVER_RANK:
                n_reject += 1
            if i < 15:
                print(f"  {i:2d} q={S._quality_rank_browser(c,'h264',want_h):5d} afr={afr} anr={anr} lr={lr} foreign_only={S._foreign_only_name(c.get('name',''))!s:5s} {c.get('name','')[:78]}")
        print(f"  ...{n_reject}/{len(order)} candidates are NEVER_RANK (carry no accepted audio)")

if __name__ == "__main__":
    dump(77939, "tt7965802", 1, 1, "MEGALOBOX")
