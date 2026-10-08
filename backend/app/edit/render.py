"""Render an edit plan to a vertical 1080x1920 MP4 with ffmpeg only.

Every shot (or every speed part of it: normal, a speed ramp, slow motion at the strongest moment) is rendered
to its own short file:
- framing: a 9:16 crop that FOLLOWS the action (the keyframes of the plan, interpolated per frame), a little
  closer for far-away action, or the whole frame on a blurred background when the action is too wide;
- speed: slow motion with frame blending, fast parts with motion blur;
- effects at the moment itself: a short punch-in or a short shake at the strongest moment of the action,
  a subtle slow zoom on calm shots;
- the colour grade of the style, the entry transition (clean cut, or now and then a flash, zoom punch, whip
  pan, glitch or dip) and a freeze frame.
A final pass joins the shots exactly (concat filter), puts the music under it (or the original sound), adds
the name on the LAST shot (never the first) and fades.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from app.config import get_settings
from app.edit.editor import STYLES, Style, shot_parts, source_seconds
from app.video import ffmpeg

log = logging.getLogger(__name__)

OUT_W, OUT_H, FPS = 1080, 1920, 30
ZP_SCALE = 1.25  # zoom/shake work on a slightly larger frame (smooth sub-pixel motion)


def _frames(seconds: float) -> int:
    return max(1, int(round(seconds * FPS)))


def _piecewise(points: list[tuple[float, float]]) -> str:
    """Linear interpolation between (t, value) keyframes as an ffmpeg expression of t."""
    if len(points) == 1:
        return f"{points[0][1]:.1f}"
    expr = f"{points[-1][1]:.1f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:], strict=False))):
        if t1 - t0 < 1e-3:
            continue
        expr = f"if(lt(t,{t1:.3f}),{v0:.1f}+({v1 - v0:.1f})*(t-{t0:.3f})/{t1 - t0:.3f},{expr})"
    return expr


def crop_filter(shot: dict, w: int, h: int, *, src_offset: float = 0.0, speed: float = 1.0) -> str:
    """The 9:16 part of the frame that follows the action. ``src_offset``/``speed`` map the time of this part
    of the shot back to the footage, so the crop follows the plan's keyframes (seconds from the shot start)."""
    framing = shot.get("framing") or {}
    zoom = max(1.0, float(framing.get("zoom") or 1.0))
    if w / h > 9 / 16:
        ch = h / zoom
        cw = ch * 9 / 16
    else:
        cw = w / zoom
        ch = min(h, cw * 16 / 9)
    cw, ch = int(round(cw / 2)) * 2, int(round(ch / 2)) * 2
    track = framing.get("track") or [[0.0, float(shot.get("cx", 0.5)), 0.5]]
    xs = [((t - src_offset) / speed, cx * w - cw / 2) for t, cx, _ in track]
    ys = [((t - src_offset) / speed, cy * h - ch / 2) for t, _, cy in track]
    x = f"max(0,min({w - cw},{_piecewise(xs)}))"
    y = f"max(0,min({h - ch},{_piecewise(ys)}))" if ch < h else "0"
    return f"crop=w={cw}:h={ch}:x='{x}':y='{y}'"


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


def _zoompan(shot: dict, style: Style, first: bool, off: int, total: int, peak: int) -> str | None:
    """Subtle zoom, a punch-in or a short shake AT the strongest moment (frame ``peak``), and the zoom/whip
    entry transitions, as a zoompan on the (1.25x) frame; None when the shot has none of them."""
    zoom, shake = shot.get("zoom"), bool(shot.get("shake"))
    whip = first and shot.get("transition") == "whip"
    entry_zoom = first and shot.get("transition") == "zoom"
    if not (zoom or shake or whip or entry_zoom):
        return None
    a = style.zoom_amount
    n = f"(on+{off})"
    p = f"({n}/{max(1, total)})"
    z = {"in": f"1+{a}*{p}", "out": f"1+{a}*(1-{p})"}.get(zoom or "", "1")
    window = f"exp(-pow(({n}-{peak})/6,2))"  # ~0.4 s around the strongest moment
    if zoom == "punch":
        z += f"+0.14*{window}"
    if entry_zoom:
        z += "+0.2*pow(max(0,1-on/7),2)"
    if whip:
        z += "+0.18*max(0,1-on/6)"
    if shake:
        z += f"+0.05*{window}"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"
    if shake:
        x += f"+(iw-iw/zoom)/2*0.6*sin({n}*1.9)*{window}"
        y += f"+(ih-ih/zoom)/2*0.6*cos({n}*2.3)*{window}"
    if whip:
        x += "+(iw-iw/zoom)/2*max(0,1-on/6)"
    x = f"max(0,min(iw-iw/zoom,{x}))"
    y = f"max(0,min(ih-ih/zoom,{y}))"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={OUT_W}x{OUT_H}:fps={FPS}"


def segment_graph(
    shot: dict, style: Style, w: int, h: int, *, speed: float, seconds: float, moving_frames: int, first: bool,
    last: bool, off: int, total: int, peak: int, src_offset: float, has_audio: bool, with_music: bool,
) -> str:
    """Filtergraph for one part of a shot: [0:v](,[0:a]) -> [v][a]."""
    chain = [f"setpts=(PTS-STARTPTS)/{speed:.4f}", f"framerate=fps={FPS}" if speed < 0.9 else f"fps={FPS}",
             f"trim=end_frame={moving_frames}"]  # exactly the moving part; a freeze frame is added after it
    if style.motion_blur and (speed >= 1.3 or (first and shot.get("transition") == "whip")):
        chain.append("tmix=frames=3")
    zp = _zoompan(shot, style, first, off, total, peak)
    zw, zh = int(round(OUT_W * ZP_SCALE / 2)) * 2, int(round(OUT_H * ZP_SCALE / 2)) * 2
    mode = (shot.get("framing") or {}).get("mode") or ("fit" if style.framing == "blur" else "crop")
    if mode == "fit":  # the whole frame (the action is too wide for 9:16) on a blurred background
        graph = (
            f"[0:v]{','.join(chain)},split=2[b][f];"
            f"[b]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
            "boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.1[bg];"
            f"[f]scale={OUT_W}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        if zp:
            graph += f",scale={zw}:{zh}:flags=lanczos"
    else:
        chain.append(crop_filter(shot, w, h, src_offset=src_offset, speed=speed))
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


def _peak_frame(shot: dict, parts: list[tuple[float, float]], part_frames: list[int]) -> int:
    """Output frame (within the shot) of its strongest moment."""
    peak = shot.get("peak")
    peak = float(peak) if peak is not None else source_seconds(shot) / 2
    acc_src, acc_frames = 0.0, 0
    for (src, speed), nfr in zip(parts, part_frames, strict=True):
        if peak <= acc_src + src:
            return acc_frames + int(round((peak - acc_src) / speed * FPS))
        acc_src += src
        acc_frames += nfr
    return max(0, acc_frames - 1)


def title_window(shot_starts: list[float], duration: float) -> tuple[float, float]:
    """The name appears on the LAST shot (never on the first one): from just after its start to the end."""
    start = shot_starts[-1] + 0.15 if len(shot_starts) > 1 else max(0.8, duration - 1.6)
    start = min(start, max(0.0, duration - 1.2))
    return round(start, 3), round(max(start + 0.6, duration - 0.35), 3)


def _title_ass(title: str, style: Style, start: float, end: float) -> str:
    title = title.replace("{", "(").replace("}", ")").replace("\\", "/")[:40]
    n = max(1, len(title))
    if style.title == "elegant":
        font, bold, size, outline, shadow, spacing = "Poppins", -1, min(96, int((980 - n * 22) / (0.352 * n))), 0, 3, 22
        tags, y = "\\fad(450,400)", OUT_H * 0.8
    elif style.title == "clean":
        font, bold, size, outline, shadow, spacing = "Poppins", -1, min(104, int(940 / (0.62 * n * 0.5675))), 0, 3, 2
        tags, y = "\\fad(250,300)", OUT_H * 0.8
    else:  # pop
        font, bold, size, outline, shadow, spacing = "Poppins ExtraBold", 0, min(150, int(930 / (0.72 * n * 0.5675))), 6, 0, 0
        tags, y = "\\fscx120\\fscy120\\fad(80,250)\\t(0,180,\\fscx100\\fscy100)", OUT_H * 0.8

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
        f"Dialogue: 0,{ts(start)},{ts(end)},T,,0,0,0,,{{\\an5\\pos({OUT_W // 2},{int(y)}){tags}}}{title}\n"
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
    shot_starts: list[float] = []
    graphs: list[str] = []
    total_parts = sum(len(shot_parts(s)) for s in shots)
    done = 0
    for si, shot in enumerate(shots):
        media, info = sources[int(shot["video_id"])]
        w, h = info.display_size or (1920, 1080)
        parts = shot_parts(shot)
        part_frames = [_frames(src / speed) for src, speed in parts]
        total_frames = sum(part_frames)
        peak = _peak_frame(shot, parts, part_frames)
        freeze = float(shot.get("freeze") or 0)
        shot_starts.append(round(sum(seg_seconds), 3))
        src_t, src_off, off = float(shot.get("start", 0.0)), 0.0, 0
        for pi, ((src_len, speed), nfr) in enumerate(zip(parts, part_frames, strict=True)):
            last = pi == len(parts) - 1
            seconds = (nfr + (_frames(freeze) if last and freeze > 0 else 0)) / FPS
            graph = segment_graph(shot, style, w, h, speed=speed, seconds=seconds, moving_frames=nfr, first=pi == 0,
                                  last=last, off=off, total=total_frames, peak=peak, src_offset=src_off,
                                  has_audio=info.has_audio, with_music=with_music)
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
            src_off += src_len
            off += nfr
            done += 1
            if progress:
                progress(done / (total_parts + 1))
    # final pass: join exactly, music or original sound, the name (on the last shot), fades
    duration = round(sum(seg_seconds), 3)
    inputs: list[str] = []
    for f in seg_files:
        inputs += ["-i", str(f)]
    n = len(seg_files)
    graph = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]"
    vf = []
    fonts = get_settings().fonts_dir
    title_at = None
    if plan.get("text", True) and plan.get("title"):
        title_at = title_window(shot_starts, duration)
        ass = workdir / "title.ass"
        ass.write_text(_title_ass(str(plan["title"]), style, *title_at), encoding="utf-8")
        fopt = f":fontsdir={ffmpeg.escape_filter_path(fonts)}" if fonts.exists() else ""
        vf.append(f"ass=filename={ffmpeg.escape_filter_path(ass)}{fopt}")
    vf.append("fade=t=in:st=0:d=0.2")
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
        "shot_starts": shot_starts,
        "title_at": title_at,
        "quality": plan.get("quality"),
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
    if any(len((s.get("framing") or {}).get("track") or []) > 1 for s in shots):
        used.add("action_tracking")
    if any((s.get("framing") or {}).get("mode") == "fit" for s in shots):
        used.add("full_frame")
    if style.motion_blur and any(float(s.get("speed") or 1) >= 1.3 or s.get("ramp") or s.get("transition") == "whip" for s in shots):
        used.add("motion_blur")
    if style.letterbox:
        used.add("letterbox")
    used |= {"fade_in_out", "colour_grade"}
    return used
