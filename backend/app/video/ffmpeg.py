"""Thin, well-tested wrappers around the ffmpeg/ffprobe CLIs."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import get_settings

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int | None
    height: int | None
    fps: float | None
    has_video: bool
    has_audio: bool
    rotation: int = 0

    @property
    def display_size(self) -> tuple[int, int] | None:
        if not self.width or not self.height:
            return None
        if self.rotation in (90, 270, -90):
            return self.height, self.width
        return self.width, self.height

    def to_dict(self) -> dict:
        size = self.display_size
        return {
            "duration": self.duration,
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "fps": self.fps,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
        }


def ffmpeg_bin() -> str:
    return get_settings().ffmpeg_bin


def ffprobe_bin() -> str:
    return get_settings().ffprobe_bin


def available() -> bool:
    return shutil.which(ffmpeg_bin()) is not None and shutil.which(ffprobe_bin()) is not None


def run(cmd: list[str], *, timeout: float | None = None, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes)
    except FileNotFoundError as e:
        raise FFmpegError(f"{cmd[0]} niet gevonden. Installeer ffmpeg (zie README).") from e
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"{cmd[0]} timeout na {timeout}s") from e
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-2500:]
        raise FFmpegError(f"{Path(cmd[0]).name} faalde (exit {proc.returncode}): {tail}")
    return proc


def _fps(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    try:
        if "/" in rate:
            n, d = rate.split("/")
            return float(n) / float(d) if float(d) else None
        return float(rate)
    except ValueError:
        return None


def probe(path: Path) -> MediaInfo:
    proc = run(
        [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=120,
    )
    data = json.loads(proc.stdout or b"{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0) or float((v or a or {}).get("duration") or 0)
    rotation = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(float(sd["rotation"]))
        rotation = int(v.get("tags", {}).get("rotate", rotation) or rotation)
    return MediaInfo(
        duration=duration,
        width=int(v["width"]) if v and v.get("width") else None,
        height=int(v["height"]) if v and v.get("height") else None,
        fps=_fps(v.get("avg_frame_rate") or v.get("r_frame_rate")) if v else None,
        has_video=v is not None,
        has_audio=a is not None,
        rotation=abs(rotation) % 360,
    )


def extract_audio(
    src: Path,
    dst: Path,
    *,
    start: float | None = None,
    duration: float | None = None,
    sample_rate: int = 16000,
    bitrate: str = "32k",
) -> Path:
    """Mono low-bitrate mp3: ~0.24 MB/min, small enough for the Whisper API 25 MB limit per chunk."""
    cmd = [ffmpeg_bin(), "-y", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-b:a", bitrate, str(dst)]
    run(cmd, timeout=3600)
    return dst


def decode_pcm(src: Path, sample_rate: int = 8000, start: float | None = None, duration: float | None = None) -> np.ndarray:
    """Decode the audio track (or [start, start+duration)) to mono float32 in [-1, 1]. 8 kHz is plenty
    for loudness features."""
    cmd = [ffmpeg_bin(), "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{max(0.05, duration):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"]
    proc = run(cmd, timeout=3600)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def extract_frame(src: Path, t: float, dst: Path, width: int | None = None) -> Path:
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1"]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    cmd += ["-q:v", "3", str(dst)]
    run(cmd, timeout=120)
    return dst


def frame_reader(src: Path, start: float, end: float, fps: float, width: int) -> tuple[Iterator[np.ndarray], int, int]:
    """Stream [start, end) at ``fps`` scaled to ``width`` px, one BGR frame at a time (constant memory
    however long the clip). Returns (iterator, W, H)."""
    info = probe(src)
    size = info.display_size or (16, 9)
    height = int(round(width * size[1] / size[0] / 2) * 2)
    cmd = [
        ffmpeg_bin(), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(src), "-t", f"{max(0.1, end - start):.3f}",
        "-vf", f"fps={fps},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]

    def frames() -> Iterator[np.ndarray]:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise FFmpegError(f"{cmd[0]} niet gevonden. Installeer ffmpeg (zie README).") from e
        frame_size = width * height * 3
        count = 0
        try:
            assert proc.stdout is not None
            while True:
                buf = proc.stdout.read(frame_size)
                if len(buf) < frame_size:
                    break
                count += 1
                yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
            if proc.wait() != 0 and count == 0:
                raise FFmpegError(f"ffmpeg kon geen beelden lezen uit {src.name}")
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            proc.kill()
            proc.wait()

    return frames(), width, height


def escape_filter_path(path: Path | str) -> str:
    """Escape a path for use inside an ffmpeg filter argument (e.g. ass=filename=...)."""
    s = str(path).replace("\\", "/")
    for ch in (":", "'", "[", "]", ",", ";"):
        s = s.replace(ch, "\\" + ch)
    return s
