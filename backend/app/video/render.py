"""Final render: cut (with dead-air removal) -> reframe to 9:16 -> captions -> loudness -> H.264 mp4."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.boundaries import map_to_output_time
from app.config import get_settings
from app.video import ffmpeg
from app.video.captions import CaptionWord, build_ass, caption_y, cues_to_groups
from app.video.reframe import CropPlan, plan_crop

log = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920


@dataclass
class RenderResult:
    video: Path
    thumbnail: Path
    duration: float
    meta: dict[str, Any]


def _video_chain(plan: CropPlan, fps: int) -> str:
    """Filter chain from [vc] (concatenated source video) to [vl] (1080x1920)."""
    tail = f"setsar=1,fps={fps},format=yuv420p"
    if plan.layout == "fit_blur":
        return (
            "[vc]split=2[vb][vf];"
            f"[vb]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            "boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.12[bg];"
            f"[vf]scale={OUT_W}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}[vl]"
        )
    if plan.layout == "split" and len(plan.split_boxes) == 2:
        (x1, y1, w1, h1), (x2, y2, w2, h2) = plan.split_boxes
        half = OUT_H // 2
        return (
            "[vc]split=2[va][vb];"
            f"[va]crop={w1}:{h1}:{x1}:{y1},scale={OUT_W}:{half}:flags=lanczos[top];"
            f"[vb]crop={w2}:{h2}:{x2}:{y2},scale={OUT_W}:{half}:flags=lanczos[bot];"
            f"[top][bot]vstack=inputs=2,{tail}[vl]"
        )
    if plan.layout == "center" and plan.crop_w >= plan.src_w:
        return f"[vc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,{tail}[vl]"
    x = plan.x_expression()
    return (
        f"[vc]crop=w={plan.crop_w}:h={plan.crop_h}:x='{x}':y=0,"
        f"scale={OUT_W}:{OUT_H}:flags=lanczos,{tail}[vl]"
    )


def output_words(words: list[tuple[float, float, str]], segments: list[tuple[float, float]]) -> list[CaptionWord]:
    """Source-time words -> caption words in clip time (after dead-air removal)."""
    out = []
    for s, e, t in words:
        os_ = map_to_output_time(segments, s)
        oe = map_to_output_time(segments, e)
        if oe - os_ < 0.02:
            oe = os_ + 0.12
        out.append(CaptionWord(os_, oe, t))
    return out


def plan_from_meta(meta: dict[str, Any] | None, info: ffmpeg.MediaInfo) -> CropPlan | None:
    """The crop plan of the last render (from ``render_meta``), so a caption preview needs no new tracking."""
    crop = (meta or {}).get("crop")
    if not crop or not crop.get("layout"):
        return None
    if crop["layout"] == "audio" or not info.has_video:
        return CropPlan("audio", OUT_W, OUT_H, OUT_W, OUT_H)
    src_w, src_h = info.display_size or (1920, 1080)
    return CropPlan(
        crop["layout"], src_w, src_h, int(crop["crop_w"]), int(crop["crop_h"]),
        keyframes=[tuple(k) for k in crop.get("keyframes") or []],
        split_boxes=[tuple(b) for b in crop.get("split_boxes") or []],
        face_top_ratio=crop.get("face_top_ratio"), face_bottom_ratio=crop.get("face_bottom_ratio"),
        detector=crop.get("detector") or "none",
    )


def render_clip(
    media: Path,
    info: ffmpeg.MediaInfo,
    segments: list[tuple[float, float]],
    words: list[tuple[float, float, str]],
    out_video: Path,
    out_thumb: Path,
    *,
    caption_preset: str = "capcut",
    layout: str = "auto",
    emphasis: list[str] | None = None,
    title: str | None = None,
    scene_cuts: list[float] | None = None,
    captions: list[dict] | None = None,
    plan: CropPlan | None = None,
    fast: bool = False,
) -> RenderResult:
    """``captions``: cues edited by the user (None = automatic captions from ``words``). ``plan``: reuse a
    crop plan instead of tracking again. ``fast``: quick low-quality encode (caption preview)."""
    if not segments:
        raise ValueError("Geen segmenten om te renderen")
    base = segments[0][0]
    span_end = segments[-1][1]
    if plan is None:
        plan = plan_crop(media, info, segments, layout=layout, scene_cuts=scene_cuts, words=words) if layout != "audio" else CropPlan(
            "audio", OUT_W, OUT_H, OUT_W, OUT_H
        )
    fps = int(round(min(60.0, info.fps or 30.0))) or 30
    out_duration = sum(b - a for a, b in segments)

    with tempfile.TemporaryDirectory(prefix="vc-render-", dir=get_settings().tmp_dir) as tmp:
        tmpdir = Path(tmp)
        graph: list[str] = []
        n = len(segments)
        for i, (a, b) in enumerate(segments):
            rs, re_ = a - base, b - base
            seg_len = b - a
            if info.has_video:
                graph.append(f"[0:v]trim=start={rs:.3f}:end={re_:.3f},setpts=PTS-STARTPTS[v{i}]")
            fade_out = max(0.0, seg_len - 0.03)
            graph.append(
                f"[0:a]atrim=start={rs:.3f}:end={re_:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d=0.02,afade=t=out:st={fade_out:.3f}:d=0.03[a{i}]"
            )
        if info.has_video:
            if n > 1:
                graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
            else:
                graph.append("[v0]null[vc]")
                graph.append("[a0]anull[ac]")
            graph.append(_video_chain(plan, fps))
        else:
            if n > 1:
                graph.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ac0]")
            else:
                graph.append("[a0]anull[ac0]")
            graph.append("[ac0]asplit=2[ac][aw]")
            graph.append(
                f"color=c=0x101014:s={OUT_W}x{OUT_H}:r={fps}:d={out_duration:.3f}[bgc];"
                f"[aw]showwaves=s={OUT_W}x360:mode=cline:colors=0xFF5A3C|0xFFFFFF:rate={fps}[wave];"
                f"[bgc][wave]overlay=0:(H-h)/2-220:shortest=1,setsar=1,format=yuv420p[vl]"
            )

        # captions
        v_out = "[vl]"
        if caption_preset != "none" and (words or captions is not None):
            cap_words = output_words(words, segments)
            groups = cues_to_groups(captions, cap_words) if captions is not None else None
            y = caption_y(plan.face_bottom_ratio, plan.face_top_ratio, plan.layout)
            ass_text = build_ass(cap_words, caption_preset, y=y, emphasis=emphasis, title=title, groups=groups)
            ass_path = tmpdir / "captions.ass"
            ass_path.write_text(ass_text, encoding="utf-8")
            fonts_dir = get_settings().fonts_dir
            fonts_opt = f":fontsdir={ffmpeg.escape_filter_path(fonts_dir)}" if fonts_dir.exists() and any(fonts_dir.iterdir()) else ""
            graph.append(f"[vl]ass=filename={ffmpeg.escape_filter_path(ass_path)}{fonts_opt}[vout]")
            v_out = "[vout]"
        elif title:
            ass_path = tmpdir / "title.ass"
            ass_path.write_text(build_ass([], "capcut", y=0, title=title), encoding="utf-8")
            graph.append(f"[vl]ass=filename={ffmpeg.escape_filter_path(ass_path)}[vout]")
            v_out = "[vout]"
        graph.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

        out_video.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg.ffmpeg_bin(), "-y", "-v", "error",
            "-ss", f"{base:.3f}", "-t", f"{span_end - base + 0.05:.3f}", "-i", str(media),
            "-filter_complex", ";".join(graph),
            "-map", v_out, "-map", "[aout]",
            "-c:v", "libx264", "-preset", "ultrafast" if fast else "veryfast", "-crf", "28" if fast else "20",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart",
            "-t", f"{out_duration:.3f}",
            str(out_video),
        ]
        ffmpeg.run(cmd, timeout=1800)

    thumb_t = min(max(0.5, out_duration * 0.15), max(0.0, out_duration - 0.1))
    out_thumb.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.extract_frame(out_video, thumb_t, out_thumb, width=540)
    return RenderResult(
        video=out_video,
        thumbnail=out_thumb,
        duration=round(out_duration, 3),
        meta={"crop": plan.to_dict(), "fps": fps, "width": OUT_W, "height": OUT_H, "segments": len(segments)},
    )
