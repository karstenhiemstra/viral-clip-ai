"""Local analysis for the Auto Edit: what happens where in the footage, and where the beats are in the music.

Footage: a low-resolution pass over the video (ffmpeg -> numpy) measures per moment how much moves, where in
the frame the movement is (to aim the 9:16 crop at the action), how bright the frame is (skip fades and black
frames), where the camera cuts are, and how loud the sound is (cheering, shouting). The result is cached per
video, so making another edit from the same footage is fast.

Music: onset strength (spectral flux) -> tempo by autocorrelation -> beat times by dynamic programming
(the classic Ellis beat tracker), plus the loudness per beat to start the edit in an energetic part.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.video import ffmpeg
from app.video.audio_features import load_profile

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 1
ANALYSIS_WIDTH = 160  # px: plenty to measure movement, cheap to decode
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


@dataclass
class Footage:
    """Per-sample measurements of one source video (``fps`` samples per second)."""

    video_id: int
    fps: float
    duration: float
    width: int
    height: int
    motion: np.ndarray  # mean absolute frame difference, 0..1
    cx: np.ndarray  # horizontal position of the movement, 0 (left) .. 1 (right)
    bright: np.ndarray  # mean brightness, 0..1
    audio: np.ndarray  # loudness z-score
    cuts: list[float] = field(default_factory=list)  # camera cuts (s)

    def to_json(self) -> dict:
        r = lambda a: [round(float(x), 4) for x in a]  # noqa: E731
        return {"v": ANALYSIS_VERSION, "video_id": self.video_id, "fps": self.fps, "duration": self.duration,
                "width": self.width, "height": self.height, "motion": r(self.motion), "cx": r(self.cx),
                "bright": r(self.bright), "audio": r(self.audio), "cuts": self.cuts}

    @classmethod
    def from_json(cls, d: dict) -> Footage:
        a = lambda k: np.asarray(d.get(k) or [], dtype=np.float32)  # noqa: E731
        return cls(int(d["video_id"]), float(d["fps"]), float(d["duration"]), int(d["width"]), int(d["height"]),
                   a("motion"), a("cx"), a("bright"), a("audio"), [float(c) for c in d.get("cuts") or []])

    def scores(self) -> np.ndarray:
        """How interesting each moment looks: movement and loudness relative to the rest of this video;
        black/faded frames and camera cuts themselves score low."""
        n = len(self.motion)
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        m = self.motion.astype(np.float64)
        med = float(np.median(m))
        mad = float(np.median(np.abs(m - med))) * 1.4826 + 1e-4
        s = np.clip((m - med) / mad, -2.0, 3.0)
        if len(self.audio) == n:
            s = s + 0.6 * np.clip(self.audio, -2.0, 3.0)
        s = s - 3.0 * (self.bright < 0.06)
        for c in self.cuts:  # the cut frame itself is not "motion"
            i = int(round(c * self.fps))
            s[max(0, i - 1) : i + 2] = np.minimum(s[max(0, i - 1) : i + 2], 0.0)
        return s.astype(np.float32)

    def shots(self, edge: float = 0.0) -> list[tuple[float, float]]:
        """Continuous camera shots [start, end) between the cuts (skipping ``edge`` s at both ends)."""
        bounds = [edge] + [c for c in self.cuts if edge < c < self.duration - edge] + [max(edge, self.duration - edge)]
        return [(a, b) for a, b in zip(bounds, bounds[1:], strict=False) if b - a >= 0.3]


def _rolling_median(x: np.ndarray, half: int) -> np.ndarray:
    if len(x) == 0:
        return x
    padded = np.pad(x, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1)
    return np.median(windows, axis=1)


def analyze_footage(media: Path, video_id: int, progress=None) -> Footage:
    info = ffmpeg.probe(media)
    if not info.has_video:
        raise ValueError("Dit bestand heeft geen beeld")
    duration = info.duration
    fps = 5.0 if duration <= 20 * 60 else 2.0
    frames, w, h = ffmpeg.frame_reader(media, 0.0, duration, fps, ANALYSIS_WIDTH)
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    motion, cx, bright = [], [], []
    prev: np.ndarray | None = None
    total = max(1, int(duration * fps))
    for i, frame in enumerate(frames):
        g = frame.mean(axis=2, dtype=np.float32) / 255.0
        bright.append(float(g.mean()))
        if prev is None:
            motion.append(0.0)
            cx.append(0.5)
        else:
            d = np.abs(g - prev)
            motion.append(float(d.mean()))
            col = d.mean(axis=0)
            s = float(col.sum())
            cx.append(float((col * xs).sum() / s) if s > 1e-6 else 0.5)
        prev = g
        if progress and i % 200 == 0:
            progress(min(1.0, i / total))
    m = np.asarray(motion, dtype=np.float32)
    # camera cuts: a frame difference far above the level around it
    cuts: list[float] = []
    if len(m) > 3:
        local = _rolling_median(m, int(2 * fps))
        for i in np.where((m > 0.08) & (m > 3.0 * local + 0.02))[0]:
            t = round(i / fps, 2)
            if not cuts or t - cuts[-1] > 0.4:
                cuts.append(t)
    audio = np.zeros(len(m), dtype=np.float32)
    if info.has_audio and len(m):
        try:
            prof = load_profile(media)
            if len(prof.z):
                audio = np.asarray([prof.z[min(len(prof.z) - 1, prof.idx(i / fps))] for i in range(len(m))], dtype=np.float32)
        except ffmpeg.FFmpegError as e:  # pragma: no cover - broken audio track
            log.warning("Audio analysis failed for %s: %s", media.name, e)
    size = info.display_size or (w, h)
    # smooth where the action is (single frames jump around)
    cxa = np.asarray(cx, dtype=np.float32)
    if len(cxa) > 4:
        cxa = np.convolve(np.pad(cxa, 2, mode="edge"), np.ones(5) / 5, mode="valid").astype(np.float32)
    return Footage(video_id, fps, duration, int(size[0]), int(size[1]), m, cxa,
                   np.asarray(bright, dtype=np.float32), audio, cuts)


def load_or_analyze(media: Path, video_id: int, cache_file: Path, progress=None) -> Footage:
    """Cached analysis (re-done when the file or the analysis version changes)."""
    stamp = f"{media.stat().st_size}-{int(media.stat().st_mtime)}"
    if cache_file.exists():
        try:
            d = json.loads(cache_file.read_text())
            if d.get("v") == ANALYSIS_VERSION and d.get("stamp") == stamp:
                return Footage.from_json(d)
        except (ValueError, KeyError):
            pass
    fx = analyze_footage(media, video_id, progress)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(fx.to_json() | {"stamp": stamp}))
    return fx


# --- music ------------------------------------------------------------------------------------------


@dataclass
class Music:
    path: Path
    duration: float
    bpm: float
    beats: np.ndarray  # beat times (s)
    energy: np.ndarray  # loudness per beat (relative)

    def best_start(self, length: float, rng: np.random.Generator) -> float:
        """A beat to start on so the edit runs over an energetic part of the song (one of the best few)."""
        usable = [i for i, b in enumerate(self.beats) if b + length <= self.duration - 0.2]
        if not usable:
            return 0.0
        n_beats = max(1, int(round(length * self.bpm / 60)))
        scored = sorted(usable, key=lambda i: -float(np.mean(self.energy[i : i + n_beats])))
        top = scored[: max(1, min(3, len(scored)))]
        return float(self.beats[int(rng.choice(top))])


def detect_beats(path: Path, max_seconds: float = 360.0) -> Music:
    sr, n_fft, hop = 11025, 1024, 256
    x = ffmpeg.decode_pcm(path, sample_rate=sr, duration=max_seconds)
    duration = len(x) / sr
    if len(x) < n_fft * 4:
        raise ValueError("Het muziekbestand is te kort")
    window = np.hanning(n_fft).astype(np.float32)
    n = 1 + (len(x) - n_fft) // hop
    flux = np.zeros(n, dtype=np.float32)
    rms = np.zeros(n, dtype=np.float32)
    prev = None
    for c0 in range(0, n, 2048):  # in chunks: constant memory for long songs
        c1 = min(n, c0 + 2048)
        idx = (np.arange(c0, c1) * hop)[:, None] + np.arange(n_fft)[None, :]
        frames = x[idx]
        rms[c0:c1] = np.sqrt(np.mean(frames**2, axis=1))
        mag = np.log1p(100.0 * np.abs(np.fft.rfft(frames * window, axis=1)))
        if prev is not None:
            mag_prev = np.vstack([prev[None, :], mag[:-1]])
        else:
            mag_prev = np.vstack([mag[:1], mag[:-1]])
        flux[c0:c1] = np.maximum(0.0, mag - mag_prev).sum(axis=1)
        prev = mag[-1]
    frame_rate = sr / hop
    # onset envelope: flux above its local average, normalised
    k = max(1, int(frame_rate * 0.5))
    onset = flux - np.convolve(flux, np.ones(k) / k, mode="same")
    onset = np.maximum(onset, 0.0)
    onset = onset / (onset.std() + 1e-9)
    # tempo: autocorrelation, with a preference for ~120 BPM
    lags = np.arange(int(frame_rate * 60 / 190), int(frame_rate * 60 / 65) + 1)
    ac = np.array([float(np.dot(onset[:-lag], onset[lag:])) / (len(onset) - lag) for lag in lags])
    bpms = 60 * frame_rate / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    period = float(lags[int(np.argmax(ac * prior))])
    # beat tracking (dynamic programming): beats on strong onsets, about one period apart
    score = onset.astype(np.float64).copy()
    back = np.full(len(onset), -1)
    lo_off, hi_off = int(round(2 * period)), max(1, int(round(period / 2)))
    for t in range(hi_off, len(onset)):
        prev_idx = np.arange(max(0, t - lo_off), t - hi_off + 1)
        if len(prev_idx) == 0:
            continue
        cand = score[prev_idx] - 100.0 * np.log((t - prev_idx) / period) ** 2
        j = int(np.argmax(cand))
        score[t] = onset[t] + cand[j]
        back[t] = prev_idx[j]
    tail = len(score) - int(period)
    t = int(np.argmax(score[max(0, tail):])) + max(0, tail)
    beats = []
    while t >= 0:
        beats.append(t)
        t = int(back[t])
    beat_frames = np.array(sorted(beats))
    beat_times = beat_frames * hop / sr + n_fft / (2 * sr)
    energy = np.array([float(rms[f : f + int(period)].mean()) for f in beat_frames])
    energy = energy / (energy.max() + 1e-9)
    return Music(path, duration, round(60 * frame_rate / period, 1), beat_times.astype(np.float64), energy)


def music_files(music_dir: Path) -> list[Path]:
    if not music_dir.exists():
        return []
    return sorted(p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS)
