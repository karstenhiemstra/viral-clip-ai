"""Cheap, local audio signals: loudness curve, excitement peaks and silences.

These are proxies for shouting, laughter bursts, crowd reactions and dead air. They cost nothing
(no API) and are computed once per video, then sliced per candidate clip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.video import ffmpeg

HOP = 0.25  # seconds per loudness frame


@dataclass
class AudioProfile:
    hop: float
    db: np.ndarray  # loudness per frame (dBFS)
    z: np.ndarray  # robust z-score of loudness relative to the whole video
    silence: np.ndarray  # bool per frame
    median_db: float

    @property
    def duration(self) -> float:
        return len(self.db) * self.hop

    def idx(self, t: float) -> int:
        return int(max(0, min(len(self.db), round(t / self.hop))))


def compute_profile(samples: np.ndarray, sample_rate: int, hop: float = HOP) -> AudioProfile:
    frame = max(1, int(sample_rate * hop))
    n = len(samples) // frame
    if n == 0:
        empty = np.zeros(0, dtype=np.float32)
        return AudioProfile(hop, empty, empty, np.zeros(0, dtype=bool), -60.0)
    frames = samples[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    db = np.maximum(db, -80.0)
    # Light smoothing so single plosives do not register as "excitement".
    kernel = np.ones(3) / 3
    db_s = np.convolve(np.pad(db, 1, mode="edge"), kernel, mode="valid")  # edge-pad: no fake peaks at t=0
    speech = db_s[db_s > -55]
    median = float(np.median(speech)) if len(speech) else float(np.median(db_s))
    mad = float(np.median(np.abs(speech - median))) if len(speech) else 1.0
    scale = max(1.5, 1.4826 * mad)
    z = (db_s - median) / scale
    silence = db_s < (median - 18.0)
    return AudioProfile(hop, db_s.astype(np.float32), z.astype(np.float32), silence, median)


def load_profile(media: Path) -> AudioProfile:
    sr = 8000
    samples = ffmpeg.decode_pcm(media, sample_rate=sr)
    return compute_profile(samples, sr)


def find_peaks(profile: AudioProfile, min_z: float = 1.6, min_distance: float = 8.0, top: int = 40) -> list[tuple[float, float]]:
    """Loudest moments (time, z), separated by at least ``min_distance`` seconds."""
    if len(profile.z) == 0:
        return []
    order = np.argsort(-profile.z)
    picked: list[tuple[float, float]] = []
    for i in order:
        z = float(profile.z[i])
        if z < min_z or len(picked) >= top:
            break
        t = i * profile.hop
        if all(abs(t - p[0]) >= min_distance for p in picked):
            picked.append((t, z))
    return sorted(picked)


def clip_features(profile: AudioProfile | None, start: float, end: float) -> dict[str, float]:
    """Audio features for [start, end). Everything is 0 when no audio profile is available."""
    keys = ("peak_z", "mean_z", "std_db", "hook_z", "end_z", "silence_ratio", "peak_pos", "burst_count")
    if profile is None or len(profile.z) == 0 or end <= start:
        return dict.fromkeys(keys, 0.0) | {"available": 0.0}
    a, b = profile.idx(start), max(profile.idx(start) + 1, profile.idx(end))
    z = profile.z[a:b]
    db = profile.db[a:b]
    if len(z) == 0:
        return dict.fromkeys(keys, 0.0) | {"available": 0.0}
    hook_n = max(1, int(2.0 / profile.hop))
    peak_i = int(np.argmax(z))
    bursts = int(np.sum((z[1:] > 1.5) & (z[:-1] <= 1.5))) if len(z) > 1 else 0
    return {
        "peak_z": float(np.max(z)),
        "mean_z": float(np.mean(z)),
        "std_db": float(np.std(db)),
        "hook_z": float(np.mean(z[:hook_n])),
        "end_z": float(np.mean(z[-hook_n:])),
        "silence_ratio": float(np.mean(profile.silence[a:b])),
        "peak_pos": peak_i / max(1, len(z) - 1),
        "burst_count": float(bursts),
        "available": 1.0,
    }


def loud_tail(profile: AudioProfile | None, t: float, max_extend: float = 1.5, min_z: float = 0.8) -> float:
    """How far the audio stays 'excited' after ``t`` (lets a laugh or reaction land before the cut)."""
    if profile is None or len(profile.z) == 0:
        return 0.0
    i = profile.idx(t)
    steps = int(max_extend / profile.hop)
    ext = 0
    while ext < steps and i + ext < len(profile.z) and profile.z[i + ext] >= min_z:
        ext += 1
    return ext * profile.hop
