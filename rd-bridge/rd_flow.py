#!/usr/bin/env python3
"""RD add→select→stream — Harbor-proven flow.
   selectFiles is POST /torrents/selectFiles/{torrentId} with files=1,2,3.
   Links map to selected files via pickLinkIndex. Episode matching via SxxExx regex."""
import re
import time

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".ts", ".mov", ".wmv")


def episode_file_regex(season: int, episode: int):
    s = str(season).zfill(2)
    e = str(episode).zfill(2)
    return re.compile(
        rf"s0*{season}[^0-9]?e0*{episode}(?![0-9])|{s}{e}(?![0-9])|\b{season}x0*{episode}(?![0-9])",
        re.I,
    )


def match_episode_file_index(names, season, episode):
    """Return the index of the file matching SxxExx (prefer video extensions)."""
    rex = episode_file_regex(season, episode)
    any_match = -1
    for i, name in enumerate(names or []):
        if not rex.search(name or ""):
            continue
        if name.lower().endswith(VIDEO_EXTS):
            return i
        if any_match < 0:
            any_match = i
    return any_match


def pick_rd_files(files, file_idx):
    """File ids to select: the matching episode/video files (Harbor's approach)."""
    if file_idx is not None and 0 <= file_idx < len(files):
        return [files[file_idx]["id"]]
    videos = [f for f in files if f.get("path", "").lower().endswith(VIDEO_EXTS)]
    if not videos:
        return [f["id"] for f in files]
    return [f["id"] for f in videos]


def pick_link_index(files, file_idx, link_count):
    """Map the selected file to its link index (selected-file order, not raw id)."""
    if not files or file_idx is None:
        return 0
    selected = [f for f in files if f.get("selected") == 1]
    target = files[file_idx] if 0 <= file_idx < len(files) else None
    if not target:
        return 0
    offset = next((i for i, f in enumerate(selected) if f["id"] == target["id"]), -1)
    if offset < 0:
        return 0
    return min(offset, link_count - 1)
