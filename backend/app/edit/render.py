"""Render an edit plan to a vertical 1080x1920 MP4 with ffmpeg only.

Every shot (or both halves of a speed ramp) is rendered to its own short file with its effects:
speed (slow motion with frame blending, fast parts with motion blur), a 9:16 crop aimed at the action (or the
whole frame on a blurred background), zoom in/out/punch, camera shake, the colour grade of the style, an entry
transition (white flash, zoom punch, whip pan, glitch, dip to black) and a freeze frame. A final pass joins
the shots exactly (concat filter), puts the music under it (or the original sound), adds the title and fades.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from app.config import get_settings
from app.edit.editor import STYLES, Style
from app.video import ffmpeg

log = logging.getLogger(__name__)

OUT_W, OUT_H, FPS = 1080, 1920, 30
ZP_SCALE = 1.25  # zoom/shake work on a slightly larger frame (smooth sub-pixel motion)
RAMP = ((0.55, 1.7), (0.45, 0.45))  # a speed ramp: 55% of the shot fast, then 45% in slow motion


def _frames(seconds: float) -> int:
    return max(1, int(round(seconds * FPS)))


def _crop(cx: float, w: int, h: int) -> str:
    """The 9:16 part of the frame around the action (``cx`` = 0..1 horizontal position)."""
    if w / h > 9 / 16:
        cw = min(w, int(round(h * 9 / 16 / 2)) * 2)
        x = int(min(max(cx * w - cw / 2, 0), w - cw))
        return f"crop={cw}:{h}:{x}:0"
    ch = min(h, int(round(w * 16 / 9 / 2)) * 2)
    return f"crop={w}:{ch}:0:{(h - ch) // 2}"


def _atempo(speed: float) -> str:
    parts = []
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def _zoompan(shot: dict, style: Style, first: bool, off: int, total: int) -> str | None:
    """Zoom/shake/whip as a zoompan on the (1.25x) frame; None when the shot has none of them."""
    zoom, shake = shot.get("zoom"), bool(shot.get("shake"))
    whip = first and shot.get("transition") == "whip"
    punch = zoom == "punch" or (first and shot.get("transition") == "zoom")
    if not (zoom or shake or whip or punch):
        return None
    a = style.zoom_amount
    p = f"((on+{off})/{max(1, total)})"
    z = {"in": f"1+{a}*{p}", "out": f"1+{a}*(1-{p})"}.get(zoom or "", "1")
    if punch:
        z += "+0.24*pow(max(0,1-on/7),2)"
    if whip:
        z += "+0.18*max(0,1-on/6)"
    if shake:
        z = f"({z})+0.06"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"
    if shake:
        x += f"+(iw-iw/zoom)/2*0.55*sin((on+{off})*1.9)"
        y += f"+(ih-ih/zoom)/2*0.55*cos((on+{off})*2.3)"
    if whip:
        x += "+(iw-iw/zoom)/2*max(0,1-on/6)"
    x = f"max(0,min(iw-iw/zoom,{x}))"
    y = f"max(0,min(ih-ih/zoom,{y}))"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUT_W}x{OUT_H}:fps={FPS}"


def segment_graph(
    shot: dict, style: Style, w: int, h: int, *, speed: float, seconds: float, first: bool, last: bool,
    off: int, total: int, has_audio: bool, with_music: bool,
) -> str:
    """Filtergraph for one part of a shot: [0:v](,[0:a]) -> [v][a]."""
    moving = seconds - (float(shot.get("freeze") or 0) if last else 0.0)
    chain = [f"setpts=(PTS-STARTPTS)/{speed:.4f}", f"framerate=fps={FPS}" if speed < 0.9 else f"fps={FPS}",
             f"trim=end_frame={_frames(moving)}"]  # exactly the moving part; a freeze frame is added after it
    if style.motion_blur and (speed >= 1.3 or (first and shot.get("transition") == "whip")):
        chain.append("tmix=frames=3")
    zp = _zoompan(shot, style, first, off, total)
    zw, zh = int(round(OUT_W * ZP_SCALE / 2)) * 2, int(round(OUT_H * ZP_SCALE / 2)) * 2
    if style.framing == "blur":
        graph = (
            f"[0:v]{','.join(chain)},split=2[b][f];"
            f"[b]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            "boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.1[bg];"
            f"[f]scale={OUT_W}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        if zp:
            graph += f",scale={zw}:{zh}:flags=lanczos"
    else:
        chain.append(_crop(float(shot.get("cx", 0.5)), w, h))
        chain.append(f"scale={zw}:{zh}:flags=lanczos" if zp else f"scale={OUT_W}:{OUT_H}:flags=lanczos")
        graph = f"[0:v]{','.join(chain)}"
    post = ["setsar=1"]
    if zp:
        post.append(zp)
    if style.grade:
        post.append(style.grade)
    if first:
        post.append({
            "flash": "fade=t=in:st=0:d=0.15:color=white",
            "dip": "fade=t=in:st=0:d=0.25",
            "glitch": "rgbashift=rh=-28:bh=28:enable='lt(t,0.12)',noise=alls=35:allf=t:enable='lt(t,0.1)'",
            "whip": "gblur=sigma=24:steps=2:enable='lt(t,0.12)'",
        }.get(shot.get("transition") or "cut", "null"))
    freeze = float(shot.get("freeze") or 0) if last else 0.0
    if freeze > 0:
        post.append(f"tpad=stop_mode=clone:stop_duration={freeze:.3f}")
    if style.letterbox:
        post.append("drawbox=x=0:y=0:w=iw:h=ih*0.09:color=black:t=fill,drawbox=x=0:y=ih*0.91:w=iw:h=ih*0.09:color=black:t=fill")
    if style.vignette:
        post.append("vignette=angle=PI/5")
    post.append("format=yuv420p")
    graph += "," + ",".join(post) + "[v]"
    if has_audio and not with_music:
        vol = "volume=0.5," if speed < 0.9 else ""
        graph += (
            f";[0:a]asetpts=PTS-STARTPTS,{_atempo(speed)},{vol}aresample=48000,aformat=channel_layouts=stereo,"
            f"afade=t=in:d=0.02,apad,atrim=0:{seconds:.3f},afade=t=out:st={max(0.0, seconds - 0.03):.3f}:d=0.03[a]"
        )
    else:
        graph += ";[1:a]anull[a]"
    return graph


def _parts(shot: dict) -> list[tuple[float, float]]:
    """(speed, output seconds of moving picture) per part of the shot."""
    moving = max(0.1, float(shot["out"]) - float(shot.get("freeze") or 0))
    if shot.get("ramp"):
        return [(speed, moving * share) for share, speed in RAMP]
    return [(float(shot.get("speed") or 1.0), moving)]


def _title_ass(title: str, style: Style, duration: float) -> str:
    title = title.replace("{", "(").replace("}", ")").replace("\\", "/")[:40]
    n = max(1, len(title))
    if style.title == "elegant":
        font, bold, size, outline, shadow, spacing = "Poppins", -1, min(124, int((980 - n * 24) / (0.352 * n))), 0, 3, 24
        tags, start, end, y = "\\fad(500,500)", 0.4, min(duration - 0.3, 2.8), OUT_H * 0.5
    elif style.title == "clean":
        font, bold, size, outline, shadow, spacing = "Poppins", -1, min(130, int(940 / (0.62 * n * 0.5675))), 0, 3, 2
        tags, start, end, y = "\\fad(250,300)", 0.3, min(duration - 0.3, 2.4), OUT_H * 0.78
    else:  # pop
        font, bold, size, outline, shadow, spacing = "Poppins ExtraBold", 0, min(260, int(930 / (0.72 * n * 0.5675))), 8, 0, 0
        tags, start, end, y = "\\fscx135\\fscy135\\fad(60,220)\\t(0,180,\\fscx100\\fscy100)", 0.15, min(duration - 0.3, 1.9), OUT_H * 0.42

    def ts(t: float) -> str:
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

    return (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: T,{font},{max(40, size)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,{bold},0,0,0,100,100,{spacing},0,1,"
        f"{outline},{shadow},5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,{ts(start)},{ts(max(start + 0.5, end))},T,,0,0,0,,{{\\an5\\pos({OUT_W // 2},{int(y)}){tags}}}{title}\n"
    )


def render_edit(
    plan: dict,
    sources: dict[int, tuple[Path, ffmpeg.MediaInfo]],
    out_video: Path,
    out_thumb: Path,
    workdir: Path,
    *,
    music_path: Path | None = None,
    progress: Callable[[float], None] | None = None,
) -> dict:
    style = STYLES.get(plan.get("style") or "hype", STYLES["hype"])
    shots = [s for s in plan.get("shots") or [] if s.get("enabled", True) and int(s["video_id"]) in sources]
    if not shots:
        raise ValueError("De edit heeft geen clips")
    with_music = bool(plan.get("music_enabled") and plan.get("music") and music_path is not None and music_path.exists())
    workdir.mkdir(parents=True, exist_ok=True)
    seg_files: list[Path] = []
    seg_seconds: list[float] = []
    graphs: list[str] = []
    total_parts = sum(len(_parts(s)) for s in shots)
    done = 0
    for si, shot in enumerate(shots):
        media, info = sources[int(shot["video_id"])]
        w, h = info.display_size or (1920, 1080)
        parts = _parts(shot)
        total_frames = sum(_frames(sec) for _, sec in parts)
        src_t = float(shot.get("start", 0.0))
        off = 0
        for pi, (speed, moving) in enumerate(parts):
            last = pi == len(parts) - 1
            seconds = _frames(moving + (float(shot.get("freeze") or 0) if last else 0)) / FPS
            src_len = moving * speed
            graph = segment_graph(shot, style, w, h, speed=speed, seconds=seconds, first=pi == 0, last=last, off=off,
                                  total=total_frames, has_audio=info.has_audio, with_music=with_music)
            seg = workdir / f"seg{si:03d}_{pi}.mp4"
            cmd = [ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{max(0.0, src_t):.3f}", "-t", f"{src_len + 0.2:.3f}",
                   "-i", str(media), "-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                   "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{seconds:.3f}", "-r", str(FPS),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(seg)]
            ffmpeg.run(cmd, timeout=600)
            seg_files.append(seg)
            seg_seconds.append(seconds)
            graphs.append(graph)
            src_t += src_len
            off += _frames(moving)
            done += 1
            if progress:
                progress(done / (total_parts + 1))
    # final pass: join exactly, music or original sound, title, fades
    duration = round(sum(seg_seconds), 3)
    inputs: list[str] = []
    for f in seg_files:
        inputs += ["-i", str(f)]
    n = len(seg_files)
    graph = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]"
    vf = []
    fonts = get_settings().fonts_dir
    if plan.get("text", True) and plan.get("title"):
        ass = workdir / "title.ass"
        ass.write_text(_title_ass(str(plan["title"]), style, duration), encoding="utf-8")
        fopt = f":fontsdir={ffmpeg.escape_filter_path(fonts)}" if fonts.exists() else ""
        vf.append(f"ass=filename={ffmpeg.escape_filter_path(ass)}{fopt}")
    vf.append("fade=t=in:st=0:d=0.25")
    vf.append(f"fade=t=out:st={max(0.0, duration - 0.45):.3f}:d=0.45")
    graph += f";[vc]{','.join(vf)}[v]"
    fade_out = f"afade=t=out:st={max(0.0, duration - 0.8):.3f}:d=0.8"
    if with_music:
        inputs += ["-ss", f"{float(plan['music']['start']):.3f}", "-t", f"{duration + 0.1:.3f}", "-i", str(music_path)]
        graph += f";[{n}:a]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.15,{fade_out},loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        graph += ";[ac]anullsink"  # the original sound is not used under music
    else:
        graph += f";[ac]{fade_out},loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    out_video.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg.ffmpeg_bin(), "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           "-t", f"{duration:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out_video)]
    ffmpeg.run(cmd, timeout=1800)
    ffmpeg.extract_frame(out_video, min(1.0, duration / 2), out_thumb, width=540)
    if progress:
        progress(1.0)
    for f in seg_files:
        f.unlink(missing_ok=True)
    return {
        "duration": round(duration, 3),
        "shots": len(shots),
        "style": style.key,
        "music": plan["music"]["file"] if with_music else None,
        "filters": graphs,
        "effects": sorted(_effects_used(shots, style)),
        "ffmpeg": shutil.which(ffmpeg.ffmpeg_bin()) is not None,
        "fps": FPS,
        "size": [OUT_W, OUT_H],
        "beats_per_minute": plan["music"]["bpm"] if with_music else None,
        "seconds_per_shot": round(duration / len(shots), 2),
    }


def _effects_used(shots: list[dict], style: Style) -> set[str]:
    used = {f"transition:{s['transition']}" for s in shots if s.get("transition") not in (None, "cut")}
    used |= {f"zoom:{s['zoom']}" for s in shots if s.get("zoom")}
    for key, name in (("shake", "camera_shake"), ("ramp", "speed_ramp")):
        if any(s.get(key) for s in shots):
            used.add(name)
    if any(float(s.get("speed") or 1) < 0.9 for s in shots):
        used.add("slow_motion")
    if any(float(s.get("freeze") or 0) > 0 for s in shots):
        used.add("freeze_frame")
    if style.motion_blur:
        used.add("motion_blur")
    if style.letterbox:
        used.add("letterbox")
    used |= {"fade_in_out", "colour_grade"}
    return used
