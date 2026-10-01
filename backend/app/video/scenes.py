"""Camera cut / scene change detection with ffmpeg's scene score (no extra dependencies)."""

from __future__ import annotations

import re
from pathlib import Path

from app.video import ffmpeg

_PTS = re.compile(r"pts_time:([0-9.]+)")


def detect_scene_cuts(
    media: Path, threshold: float = 0.32, start: float | None = None, end: float | None = None
) -> list[float]:
    cmd = [ffmpeg.ffmpeg_bin(), "-v", "info", "-nostats"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(media)]
    if start is not None and end is not None:
        cmd += ["-t", f"{max(0.1, end - start):.3f}"]
    cmd += [
        "-an",
        "-sn",
        "-vf",
        f"scale=192:-2,select='gt(scene\\,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
    proc = ffmpeg.run(cmd, timeout=3600)
    offset = start or 0.0
    cuts = [offset + float(m) for m in _PTS.findall(proc.stderr.decode("utf-8", "replace"))]
    # Collapse flashes / fast transitions into one cut.
    out: list[float] = []
    for t in sorted(cuts):
        if not out or t - out[-1] > 0.4:
            out.append(round(t, 3))
    return out
