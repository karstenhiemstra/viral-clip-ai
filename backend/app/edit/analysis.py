"""Local analysis for the Auto Edit: what happens where in the footage, and where the beats are in the music.

Footage: a low-resolution pass over the video (ffmpeg -> numpy/OpenCV). Per moment it measures how the camera
moves (phase correlation between frames) and what moves *in* the picture once that camera movement is taken
out: the player, the ball and the opponent. Static overlays (score bar, logo) and the panning background drop
out because the movement has to be visible both before and after compensating the camera. From that:
how much action there is, where it is (to aim the 9:16 crop at it and follow it), how spread out it is (does
it fit in a 9:16 crop?), plus brightness, camera cuts, loudness and a tiny thumbnail per second (to skip
near-identical shots). The result is cached per video.

``detect_actions`` turns this into complete actions: a run of movement with a start and an end inside one
camera shot (a dribble, a skill move, a sprint, a shot on goal), each with quality checks.

Music: onset strength (spectral flux) -> tempo by autocorrelation -> beat times by dynamic programming
(the classic Ellis beat tracker), plus the loudness per beat to start the edit in an energetic part.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.video import ffmpeg
from app.video.audio_features import load_profile

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 2
ANALYSIS_WIDTH = 160  # px: plenty to measure movement, cheap to decode
CROP_FRACTION = 9 / 16 / (16 / 9)  # width of a full-height 9:16 crop in a 16:9 frame (0.316)
NOISE = 0.035  # frame differences below this are compression noise / flicker
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


@dataclass
class Footage:
    """Per-sample measurements of one source video (``fps`` samples per second)."""

    video_id: int
    fps: float
    duration: float
    width: int
    height: int
    motion: np.ndarray  # mean absolute frame difference (camera + content), 0..1
    cx: np.ndarray  # horizontal centre of the action, 0 (left) .. 1 (right)
    bright: np.ndarray  # mean brightness, 0..1
    audio: np.ndarray  # loudness z-score
    cuts: list[float] = field(default_factory=list)  # camera cuts (s)
    activity: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # movement in the picture
    cy: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # vertical centre of the action
    cover: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # share of it inside a 9:16 crop
    extent: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # its width (p10-p90), 0..1
    pan: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # camera movement per second
    thumbs: list[list[float]] = field(default_factory=list)  # 8x6 grey thumbnail per second

    ARRAYS = ("motion", "cx", "bright", "audio", "activity", "cy", "cover", "extent", "pan")

    def to_json(self) -> dict:
        r = lambda a: [round(float(x), 4) for x in a]  # noqa: E731
        return {"v": ANALYSIS_VERSION, "video_id": self.video_id, "fps": self.fps, "duration": self.duration,
                "width": self.width, "height": self.height, "cuts": self.cuts, "thumbs": self.thumbs,
                **{k: r(getattr(self, k)) for k in self.ARRAYS}}

    @classmethod
    def from_json(cls, d: dict) -> Footage:
        arrays = {k: np.asarray(d.get(k) or [], dtype=np.float32) for k in cls.ARRAYS}
        return cls(int(d["video_id"]), float(d["fps"]), float(d["duration"]), int(d["width"]), int(d["height"]),
                   cuts=[float(c) for c in d.get("cuts") or []], thumbs=d.get("thumbs") or [], **arrays)

    def idx(self, t: float) -> int:
        return int(max(0, min(len(self.motion) - 1, round(t * self.fps))))

    def scores(self) -> np.ndarray:
        """How interesting each moment looks: movement in the picture and loudness relative to the rest of
        this video; black/faded frames and camera cuts themselves score low."""
        n = len(self.motion)
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        m = (self.activity if len(self.activity) == n else self.motion).astype(np.float64)
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

    def thumb(self, t: float) -> np.ndarray | None:
        if not self.thumbs:
            return None
        return np.asarray(self.thumbs[int(max(0, min(len(self.thumbs) - 1, round(t))))], dtype=np.float32)


def _rolling_median(x: np.ndarray, half: int) -> np.ndarray:
    if len(x) == 0:
        return x
    padded = np.pad(x, half, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1)
    return np.median(windows, axis=1)


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) < 2 or n <= 1:
        return x.astype(np.float32)
    half = n // 2
    return np.convolve(np.pad(x, half, mode="edge"), np.ones(2 * half + 1) / (2 * half + 1), mode="valid").astype(np.float32)


def _shifted(prev: np.ndarray, dx: int, dy: int) -> tuple[np.ndarray, np.ndarray]:
    """``prev`` moved by (dx, dy) and the mask of pixels that are valid after the move."""
    h, w = prev.shape[:2]
    out = np.zeros_like(prev)
    valid = np.zeros((h, w), dtype=bool)
    ys, yd = (slice(0, h - dy), slice(dy, h)) if dy >= 0 else (slice(-dy, h), slice(0, h + dy))
    xs, xd = (slice(0, w - dx), slice(dx, w)) if dx >= 0 else (slice(-dx, w), slice(0, w + dx))
    out[yd, xd] = prev[ys, xs]
    valid[yd, xd] = True
    return out, valid


def _action_place(local: np.ndarray) -> tuple[float, float, float, float]:
    """(cx, cy, share inside the best 9:16 window, extent) of the movement in the picture."""
    h, w = local.shape
    col = local.sum(axis=0)
    total = float(col.sum())
    if total <= 1e-6:
        return 0.5, 0.5, 1.0, 0.0
    win = max(1, int(round(CROP_FRACTION * w)))
    sums = np.convolve(col, np.ones(win), mode="valid")
    best = int(np.argmax(sums))
    cover = float(sums[best] / total)
    xs = np.arange(w, dtype=np.float32)
    seg = col[best : best + win]
    cx = float((seg * xs[best : best + win]).sum() / max(1e-6, seg.sum())) / max(1, w - 1)
    cum = np.cumsum(col) / total
    extent = float(np.searchsorted(cum, 0.9) - np.searchsorted(cum, 0.1)) / max(1, w - 1)
    row = local.sum(axis=1)
    cy = float((row * np.arange(h)).sum() / max(1e-6, row.sum())) / max(1, h - 1)
    return cx, cy, cover, extent


def analyze_footage(media: Path, video_id: int, progress=None) -> Footage:
    info = ffmpeg.probe(media)
    if not info.has_video:
        raise ValueError("Dit bestand heeft geen beeld")
    duration = info.duration
    fps = 5.0 if duration <= 20 * 60 else 2.0
    frames, w, h = ffmpeg.frame_reader(media, 0.0, duration, fps, ANALYSIS_WIDTH)
    window = cv2.createHanningWindow((w, h), cv2.CV_32F)
    rows: dict[str, list[float]] = {k: [] for k in ("motion", "cx", "bright", "activity", "cy", "cover", "extent", "pan", "match")}
    thumbs: list[list[float]] = []
    prev: np.ndarray | None = None
    prev_rgb: np.ndarray | None = None
    last_place = (0.5, 0.5, 1.0, 0.0)
    total = max(1, int(duration * fps))
    every = max(1, int(round(fps)))
    for i, frame in enumerate(frames):
        rgb = frame.astype(np.float32) / 255.0
        g = rgb.mean(axis=2)
        rows["bright"].append(float(g.mean()))
        if i % every == 0:
            thumbs.append([round(float(v), 3) for v in cv2.resize(g, (8, 6), interpolation=cv2.INTER_AREA).ravel()])
        if prev is None:
            for k, v in (("motion", 0.0), ("cx", 0.5), ("activity", 0.0), ("cy", 0.5), ("cover", 1.0), ("extent", 0.0), ("pan", 0.0), ("match", 1.0)):
                rows[k].append(v)
        else:
            rows["motion"].append(float(np.abs(g - prev).mean()))
            # colour-aware: a red shirt on green grass can be as bright as the grass
            raw = np.abs(rgb - prev_rgb).max(axis=2)
            # camera movement between the two frames, then what moves on top of it
            # (copies: phaseCorrelate applies the window to its inputs in place)
            (sx, sy), match = cv2.phaseCorrelate(prev.copy(), g.copy(), window)
            rows["match"].append(float(match))
            dx, dy = int(round(sx)), int(round(sy))
            if abs(dx) > w // 3 or abs(dy) > h // 3:  # no sensible match (a cut): no "action" here
                dx = dy = 0
            moved, valid = _shifted(prev_rgb, dx, dy)
            residual = np.where(valid, np.abs(rgb - moved).max(axis=2), 0.0)
            # what is left after compensating the camera; minus this frame's own noise level (grain,
            # compression), so only real movement in the picture remains
            floor = max(NOISE, 3.0 * float(np.median(residual[valid]))) if valid.any() else NOISE
            local = np.maximum(0.0, np.minimum(residual, raw) - floor)
            rows["activity"].append(float(local.mean()))
            # where the action is; with (almost) nothing moving, keep the last known place
            cx, cy, cover, extent = last_place = _action_place(local) if local.mean() > 2e-4 else last_place
            rows["cx"].append(cx)
            rows["cy"].append(cy)
            rows["cover"].append(cover)
            rows["extent"].append(extent)
            rows["pan"].append(float(np.hypot(sx, sy)) / w * fps)
        prev, prev_rgb = g, rgb
        if progress and i % 200 == 0:
            progress(min(1.0, i / total))
    m = np.asarray(rows["motion"], dtype=np.float32)
    match = np.asarray(rows["match"], dtype=np.float32)
    # camera cuts: a frame difference far above the level around it, or two frames that do not match at all
    cuts: list[float] = []
    if len(m) > 3:
        local_level = _rolling_median(m, int(2 * fps))
        jump = (m > 0.08) & (m > 3.0 * local_level + 0.02)
        unrelated = (match < 0.12) & (m > 1.8 * local_level + 0.01) & (_rolling_median(match, int(fps)) > 0.2)
        bright = np.asarray(rows["bright"], dtype=np.float32)
        step = np.abs(np.diff(bright, prepend=bright[:1])) > 0.05
        lighting = step & (m > 2.0 * local_level + 0.01)  # another camera: the whole picture changes level
        for i in np.where(jump | unrelated | lighting)[0]:
            t = round(i / fps, 2)
            if not cuts or t - cuts[-1] > 0.4:
                cuts.append(t)
    act = np.asarray(rows["activity"], dtype=np.float32)
    for c in cuts:  # a cut is not action
        j = int(round(c * fps))
        act[max(0, j - 1) : j + 1] = 0.0
    audio = np.zeros(len(m), dtype=np.float32)
    if info.has_audio and len(m):
        try:
            prof = load_profile(media)
            if len(prof.z):
                audio = np.asarray([prof.z[min(len(prof.z) - 1, prof.idx(i / fps))] for i in range(len(m))], dtype=np.float32)
        except ffmpeg.FFmpegError as e:  # pragma: no cover - broken audio track
            log.warning("Audio analysis failed for %s: %s", media.name, e)
    size = info.display_size or (w, h)
    return Footage(
        video_id, fps, duration, int(size[0]), int(size[1]), m, _smooth(np.asarray(rows["cx"], np.float32), 3),
        np.asarray(rows["bright"], dtype=np.float32), audio, cuts, activity=act,
        cy=_smooth(np.asarray(rows["cy"], np.float32), 3), cover=np.asarray(rows["cover"], np.float32),
        extent=np.asarray(rows["extent"], np.float32), pan=np.asarray(rows["pan"], np.float32), thumbs=thumbs,
    )


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


# --- complete actions ---------------------------------------------------------------------------------

LEAD = 0.5  # s of context before an action (the run-up)
TAIL = 0.4  # s after it (the player going on, the reaction)


@dataclass
class Action:
    """One complete action inside one camera shot, with the context window around it."""

    video_id: int
    start: float  # the movement begins
    end: float  # the movement is over
    peak: float  # the strongest moment (the skill move, the shot)
    win_start: float  # window shown: a little context before ...
    win_end: float  # ... and after
    intensity: float  # strength of the movement relative to this video
    score: float  # overall quality, higher is better
    checks: dict = field(default_factory=dict)
    shot_start: float = 0.0  # the camera shot it is in (the window may grow inside it, never beyond)
    shot_end: float = 0.0

    @property
    def length(self) -> float:
        return self.win_end - self.win_start


def detect_actions(fx: Footage, min_len: float = 0.6, max_len: float = 4.0) -> list[Action]:
    """Complete actions with quality checks, best first.

    An action is a run of clearly more movement in the picture than usual for this video. It must lie inside
    one camera shot; it is shown with up to ``LEAD`` s before and ``TAIL`` s after. Checks: complete (the
    movement starts and ends inside the shot, with context around it), visible (there is a clear moving
    subject), size (not tiny in the frame), in frame (the movement fits in a 9:16 crop, or the whole frame is
    shown), not dark."""
    n = len(fx.activity)
    if n < 3:
        return []
    fps = fx.fps
    act = _smooth(fx.activity, max(1, int(round(0.6 * fps))))
    base = float(np.median(act))
    spread = float(np.median(np.abs(act - base))) * 1.4826 + 1e-4
    level = base + 0.8 * spread
    strong = base + 2.0 * spread
    out: list[Action] = []
    for sa, sb in fx.shots():
        i0, i1 = fx.idx(sa), max(fx.idx(sa) + 1, fx.idx(sb))
        on = act[i0:i1] > level
        # runs of movement (gaps < 0.4 s are one action)
        runs: list[list[int]] = []
        for k, flag in enumerate(on):
            if flag:
                if runs and k - runs[-1][1] <= max(1, int(0.4 * fps)):
                    runs[-1][1] = k
                else:
                    runs.append([k, k])
        for a, b in runs:
            # long runs: split at the calmest moments (natural pauses between two actions)
            pieces = [(a, b)]
            while any((q - p + 1) / fps > max_len for p, q in pieces):
                p, q = next(x for x in pieces if (x[1] - x[0] + 1) / fps > max_len)
                lo, hi = p + int(min_len * fps), q - int(min_len * fps)
                if hi <= lo:
                    break
                cut = lo + int(np.argmin(act[i0 + lo : i0 + hi + 1]))
                pieces.remove((p, q))
                pieces += [(p, cut), (cut + 1, q)]
            for p, q in pieces:
                start, end = sa + p / fps, min(sb, sa + (q + 1) / fps)
                if end - start < min_len:
                    continue
                seg = act[i0 + p : i0 + q + 1]
                peak = start + float(np.argmax(seg)) / fps
                lead_room, tail_room = start - sa, sb - end
                win_start, win_end = start - min(LEAD, lead_room), end + min(TAIL, tail_room)
                j0, j1 = i0 + p, i0 + q + 1
                cover = float(np.median(fx.cover[j0:j1])) if len(fx.cover) >= j1 else 1.0
                extent = float(np.percentile(fx.extent[j0:j1], 80)) if len(fx.extent) >= j1 else 0.2
                intensity = float((seg.max() - base) / spread)
                audio = float(np.max(fx.audio[j0 : min(n, j1 + int(fps))])) if len(fx.audio) == n else 0.0
                checks = {
                    "complete": bool(lead_room >= 0.25 and tail_room >= 0.15),
                    "context_before": round(min(LEAD, lead_room), 2),
                    "context_after": round(min(TAIL, tail_room), 2),
                    "visible": bool(seg.max() > strong or intensity > 2.0),
                    "size_ok": bool(extent >= 0.04),
                    "in_frame": round(cover, 2),
                    "fits_crop": bool(cover >= 0.5),
                    "dark": bool(float(np.mean(fx.bright[j0:j1])) < 0.08),
                }
                score = min(4.0, intensity) + 0.5 * max(0.0, min(3.0, audio))
                score += 1.0 if checks["complete"] else -1.5
                score += 0.5 if checks["visible"] else -1.0
                score += 0.4 * min(1.0, cover / 0.6) - (0.8 if not checks["size_ok"] else 0.0)
                score -= 3.0 if checks["dark"] else 0.0
                score -= 0.3 * max(0.0, (win_end - win_start) - 3.5)  # very long: a little less snappy
                out.append(Action(fx.video_id, round(start, 2), round(end, 2), round(peak, 2), round(win_start, 2),
                                  round(win_end, 2), round(intensity, 2), round(score, 3), checks, round(sa, 3), round(sb, 3)))
    return sorted(out, key=lambda x: -x.score)


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
