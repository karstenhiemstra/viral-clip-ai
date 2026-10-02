"""16:9 -> 9:16 reframing that keeps the person who is SPEAKING in the middle of the frame.

1. Stream frames over the clip (25 fps, 640 px) and detect faces: YuNet (DNN, with landmarks) when its
   model is available, otherwise OpenCV's Haar cascades.
2. Track faces per shot (scene cuts reset tracks), bridge short detection gaps ("coasting": a head that
   turns away for a moment, a hand in front of the face) and merge pieces of the same person.
3. Lips per face, in face-aligned patches that hang from the eyes and are steadied over a few frames:
   lip/jaw motion minus eye-region motion (nodding or swaying does not count as talking), and how far the
   mouth is from its closed look.
4. Speech: when is anybody talking (transcript word timings checked against the audio; an energy VAD when
   there are no words) and the loudness envelope of the voice.
5. Who is talking: while somebody talks, each face gets evidence for how well its mouth opening follows the
   loudness of the voice (audio-visual sync, at the audio/video offset that fits the clip best) and for its
   share of all lip motion - so a silent person chewing or laughing loses against the real speaker. Face
   size only breaks ties.
   A Viterbi pass over each shot picks one speaker per frame with a cost per switch, so a new speaker needs
   clear evidence for a while (~0.5 s) before the camera moves, a short "ja" or a nodding listener never
   steals the frame, and without evidence (pauses, doubt) the last speaker is kept.
6. Camera: one target per speaker turn, re-centred only when the speaker leaves a dead zone; eased pans
   between targets (no jumps, no jitter); hard cuts only where the video itself cuts. The crop is always
   full height (no zoom pumping, no cut-off heads) and clamped inside the source frame (never black bars).
7. Layout: face (follow the speaker) | split (two far-apart people in very rapid back-and-forth) |
   fit_blur (no faces) | center.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.config import get_settings
from app.video import ffmpeg

log = logging.getLogger(__name__)

ANALYSIS_FPS = 25.0  # lip motion follows syllables (4-6 Hz) and audio/video can be offset by ~0.1 s
ANALYSIS_WIDTH = 640
COAST_SECONDS = 1.5  # a face stays "present" this long without detections (head turned, occluded)
SWITCH_COST = 0.6  # evidence-seconds a new speaker needs before the camera changes person
ABSENT_COST = 2.5  # per second, for "following" a face that is not in the picture
MIN_TURN = 0.8  # seconds; shorter speaker turns are interruptions and keep the previous speaker
DEADZONE = 0.14  # fraction of the crop width the speaker may drift before the camera re-centres
MIN_MOVE = 0.05  # moves smaller than this fraction of the crop width are skipped (no micro-adjustments)
PAN_LEAD = 0.12  # seconds: start a pan just before the new speaker starts talking
CORR_WEIGHT = 1.5
SHARE_WEIGHT = 1.0
SYNC_WINDOW = 1.0  # seconds either side for the audio-visual correlation
MAX_AV_LAG = 0.16  # seconds; audio and video of a file are rarely more than this out of sync
_MOUTH_SIZE = (32, 24)
_UPPER_SIZE = (32, 16)


@dataclass
class Face:
    x: float
    y: float
    w: float
    h: float
    score: float = 1.0
    mouth: tuple[float, float, float, float] | None = None  # x, y, w, h of the mouth region
    landmarks: tuple[tuple[float, float], ...] | None = None  # YuNet: r-eye, l-eye, nose, r-mouth, l-mouth
    mouth_patch: np.ndarray | None = field(default=None, repr=False, compare=False)
    upper_patch: np.ndarray | None = field(default=None, repr=False, compare=False)
    crop: np.ndarray | None = field(default=None, repr=False, compare=False)  # grey pixels around the face
    crop_xy: tuple[int, int] = (0, 0)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class CropPlan:
    layout: str  # face | center | fit_blur | split | audio
    src_w: int
    src_h: int
    crop_w: int
    crop_h: int
    # Crop x (left edge, source px) over OUTPUT time: (t, x) = cut to x at t; (t, x, d) = eased pan from the
    # previous x to x during [t, t + d].
    keyframes: list[tuple] = field(default_factory=list)
    split_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    face_top_ratio: float | None = None
    face_bottom_ratio: float | None = None
    detector: str = "none"
    stats: dict = field(default_factory=dict)

    def _kf(self) -> list[tuple[float, float, float]]:
        return [(float(k[0]), float(k[1]), float(k[2]) if len(k) > 2 else 0.0) for k in self.keyframes]

    def x_at(self, t: float) -> float:
        """Crop x at output time ``t`` (the same curve as ``x_expression``)."""
        kf = self._kf()
        if not kf:
            return float(max(0, (self.src_w - self.crop_w) // 2))
        x = kf[0][1]
        for ti, xi, di in kf[1:]:
            if t < ti:
                return x
            if di > 0 and t < ti + di:
                p = (t - ti) / di
                return x + (xi - x) * p * p * (3 - 2 * p)
            x = xi
        return x

    def x_expression(self) -> str:
        """ffmpeg expression for the crop x over output time ``t`` (use inside single quotes)."""
        kf = self._kf()
        if not kf:
            return str(max(0, (self.src_w - self.crop_w) // 2))
        expr = _num(kf[-1][1])
        for i in range(len(kf) - 1, 0, -1):
            ti, xi, di = kf[i]
            prev = kf[i - 1][1]
            if di > 0:  # smoothstep ease-in-out: starts and ends at zero speed
                p = f"((t-{ti:.3f})/{di:.3f})"
                expr = f"if(lt(t,{ti + di:.3f}),{_num(prev)}+({_num(xi - prev)})*{p}*{p}*(3-2*{p}),{expr})"
            expr = f"if(lt(t,{ti:.3f}),{_num(prev)},{expr})"
        return expr

    def to_dict(self) -> dict:
        return {
            "layout": self.layout, "crop_w": self.crop_w, "crop_h": self.crop_h, "keyframes": self.keyframes,
            "split_boxes": self.split_boxes, "detector": self.detector,
            "face_top_ratio": self.face_top_ratio, "face_bottom_ratio": self.face_bottom_ratio, "stats": self.stats,
        }


def _num(v: float) -> str:
    return str(int(round(v)))


class FaceDetector:
    _lock = threading.Lock()
    _yunet_failed = False
    _failed_at = 0.0
    RETRY_AFTER = 900.0  # seconds; a failed model download is retried later instead of never

    def __init__(self) -> None:
        self.kind = "none"
        self._yunet = None
        self._yunet_size: tuple[int, int] | None = None
        self._haar: list = []
        model = get_settings().face_model_path
        if not model.exists() and self._may_download():
            self._try_download(model)
        if model.exists() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)
                self.kind = "yunet"
            except cv2.error as e:
                log.warning("YuNet model could not be loaded (%s); using Haar cascades", e)
        if self._yunet is None and hasattr(cv2, "CascadeClassifier"):
            base = getattr(cv2.data, "haarcascades", "")
            for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
                clf = cv2.CascadeClassifier(base + name)
                if not clf.empty():
                    self._haar.append(clf)
            if self._haar:
                self.kind = "haar"

    @classmethod
    def _may_download(cls) -> bool:
        return not cls._yunet_failed or time.monotonic() - cls._failed_at > cls.RETRY_AFTER

    @classmethod
    def _try_download(cls, target: Path) -> None:
        url = get_settings().face_model_url
        with cls._lock:
            if target.exists() or not cls._may_download():
                return
            if not url:
                cls._yunet_failed, cls._failed_at = True, time.monotonic()
                return
            try:
                import httpx

                resp = httpx.get(url, follow_redirects=True, timeout=20)
                if resp.status_code == 200 and len(resp.content) > 100_000:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(f".{os.getpid()}.part")  # API and worker may download at once
                    tmp.write_bytes(resp.content)
                    tmp.replace(target)
                    log.info("Downloaded YuNet face model to %s", target)
                    cls._yunet_failed = False
                    return
                log.info("YuNet download failed: HTTP %s", resp.status_code)
            except Exception as e:  # pragma: no cover - network specific
                log.info("YuNet download failed: %s", e)
            cls._yunet_failed, cls._failed_at = True, time.monotonic()

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if self._yunet is not None:
            if self._yunet_size != (w, h):
                self._yunet.setInputSize((w, h))
                self._yunet_size = (w, h)
            _, faces = self._yunet.detect(frame)
            out = []
            for f in faces if faces is not None else []:
                x, y, fw, fh = map(float, f[:4])
                landmarks = tuple((float(f[i]), float(f[i + 1])) for i in range(4, 14, 2))
                rmx, rmy, lmx, lmy = map(float, f[10:14])
                mw = max(4.0, abs(lmx - rmx) * 1.4)
                mouth = (min(rmx, lmx) - mw * 0.15, min(rmy, lmy) - fh * 0.12, mw, fh * 0.28)
                out.append(Face(x, y, fw, fh, float(f[14]), mouth, landmarks))
            return [f for f in out if f.w > w * 0.03]
        if self._haar:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            f = min(1.0, _HAAR_WIDTH / w)  # cascades are slow; faces stay large enough at 480 px
            small = cv2.resize(gray, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA) if f < 1 else gray
            sh, sw = small.shape[:2]
            boxes = self._haar_boxes(small, sh, sw)
            if not boxes:
                # Dark / low-contrast footage: retry with LOCAL contrast enhancement. (Global histogram
                # equalisation is avoided on purpose: a flat background dominates the histogram and
                # washes out the face - measured 0/40 vs 40/40 detections on a test clip.)
                boxes = self._haar_boxes(_CLAHE.apply(small), sh, sw)
            boxes = [(x / f, y / f, bw / f, bh / f) for x, y, bw, bh in boxes]
            faces = [Face(x, y, bw, bh, 1.0, (x + bw * 0.25, y + bh * 0.62, bw * 0.5, bh * 0.3)) for x, y, bw, bh in boxes]
            return _nms(faces)
        return []

    def _haar_boxes(self, gray: np.ndarray, h: int, w: int) -> list[tuple[int, int, int, int]]:
        side = max(18, int(h * 0.06))
        boxes: list[tuple[int, int, int, int]] = []
        for i, clf in enumerate(self._haar):
            found = clf.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(side, side))
            boxes += [tuple(map(int, b)) for b in found]
            if i == 1:  # profile cascade only detects one orientation; also run it mirrored
                flipped = clf.detectMultiScale(cv2.flip(gray, 1), scaleFactor=1.1, minNeighbors=6, minSize=(side, side))
                boxes += [(w - x - bw, y, bw, bh) for (x, y, bw, bh) in map(lambda b: tuple(map(int, b)), flipped)]
        return boxes


_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
_HAAR_WIDTH = 480


def _nms(faces: list[Face], thr: float = 0.3) -> list[Face]:
    faces = sorted(faces, key=lambda f: f.w * f.h, reverse=True)
    keep: list[Face] = []
    for f in faces:
        if all(_iou(f, k) < thr for k in keep):
            keep.append(f)
    return keep


def _iou(a: Face, b: Face) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


# --- lip motion ------------------------------------------------------------------------------------


def _cut_patch(
    gray: np.ndarray, origin: tuple[float, float], ux: tuple[float, float], uy: tuple[float, float],
    u: tuple[float, float], v: tuple[float, float], size: tuple[int, int],
) -> np.ndarray | None:
    """Sample the face-aligned rectangle u x v (face units along ``ux``/``uy`` from ``origin``) into a
    normalised ``size`` patch; None when it lies mostly outside the frame."""
    H, W = gray.shape[:2]
    corners = [(origin[0] + a * ux[0] + c * uy[0], origin[1] + a * ux[1] + c * uy[1]) for a in u for c in v]
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    bw, bh = max(xs) - min(xs), max(ys) - min(ys)
    if bw < 4 or bh < 4 or min(xs) < -0.25 * bw or min(ys) < -0.25 * bh or max(xs) > W + 0.25 * bw or max(ys) > H + 0.25 * bh:
        return None
    pw, ph = size
    sa, sb = (u[1] - u[0]) / pw, (v[1] - v[0]) / ph
    m = np.array([
        [ux[0] * sa, uy[0] * sb, origin[0] + ux[0] * (u[0] + 0.5 * sa) + uy[0] * (v[0] + 0.5 * sb)],
        [ux[1] * sa, uy[1] * sb, origin[1] + ux[1] * (u[0] + 0.5 * sa) + uy[1] * (v[0] + 0.5 * sb)],
    ], dtype=np.float32)
    p = cv2.warpAffine(gray, m, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
    p = p.astype(np.float32)
    return (p - p.mean()) / (p.std() + 8.0)


def face_geometry(f: Face) -> tuple[float, float, float, float]:
    """(x, y, ux, uy): the point between the eyes and the eye axis, one "face unit" long. The lip patches
    hang from the EYES, which stay put when the jaw moves (placing them on the detected mouth would follow
    the jaw and hide exactly the motion we want to see)."""
    if f.landmarks:
        (rex, rey), (lex, ley) = f.landmarks[0], f.landmarks[1]
        ex, ey = lex - rex, ley - rey
        d = math.hypot(ex, ey) or 1.0
        scale = max(d, 0.38 * f.w)  # turned heads: the eyes get closer together, the face does not shrink
        return (rex + lex) / 2, (rey + ley) / 2, ex / d * scale, ey / d * scale
    return f.cx, f.y + 0.38 * f.h, 0.4 * f.w, 0.0


def face_patches(
    gray: np.ndarray, geometry: tuple[float, float, float, float], offset: tuple[int, int] = (0, 0)
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Normalised grey patches in face-aligned coordinates: mouth + chin (moves when talking) and eyes
    (moves with the head only). ``offset`` = position of ``gray`` in the frame when it is a crop."""
    x, y, uxx, uxy = geometry
    origin = (x - offset[0], y - offset[1])
    ux, uy = (uxx, uxy), (-uxy, uxx)  # uy: perpendicular, pointing down the face
    mouth = _cut_patch(gray, origin, ux, uy, (-0.8, 0.8), (0.75, 1.95), _MOUTH_SIZE)
    upper = _cut_patch(gray, origin, ux, uy, (-0.95, 0.95), (-0.45, 0.35), _UPPER_SIZE)
    return mouth, upper


def keep_face_crop(gray: np.ndarray, f: Face) -> None:
    """Remember the pixels around a face (eyes to below the chin) so lip patches can be cut after tracking,
    with a steadied face position."""
    H, W = gray.shape[:2]
    x0, y0 = int(max(0, f.x - 0.4 * f.w)), int(max(0, f.y - 0.2 * f.h))
    x1, y1 = int(min(W, f.x + 1.4 * f.w)), int(min(H, f.y + 1.45 * f.h))
    if x1 - x0 >= 8 and y1 - y0 >= 8:
        f.crop, f.crop_xy = gray[y0:y1, x0:x1].copy(), (x0, y0)


def _cut_lip_patches(t: Track) -> None:
    """Cut the lip/eye patches of a track with a steadied face position: detector jitter of a pixel or two
    would otherwise look like lip motion (worst on tilted or turned heads). Landmarks: mean over 5 frames.
    Haar boxes flip between sizes, so they get a median over ~0.7 s."""
    frames = sorted(fi for fi, f in t.faces.items() if f.crop is not None and f.mouth_patch is None)
    if not frames:
        return
    geo = np.array([face_geometry(t.faces[fi]) for fi in frames])
    for j, fi in enumerate(frames):
        r = 2 if t.faces[fi].landmarks else 8
        near = [k for k in range(max(0, j - r), min(len(frames), j + r + 1)) if abs(frames[k] - fi) <= r]
        g = tuple(float(v) for v in (geo[near].mean(axis=0) if r == 2 else np.median(geo[near], axis=0)))
        face = t.faces[fi]
        face.mouth_patch, face.upper_patch = face_patches(face.crop, g, face.crop_xy)
        face.crop = None


# --- tracking --------------------------------------------------------------------------------------


@dataclass
class Track:
    tid: int
    shot: int = 0
    faces: dict[int, Face] = field(default_factory=dict)  # frame -> face (detected or bridged)
    detected: set[int] = field(default_factory=set)
    activity: dict[int, float] = field(default_factory=dict)
    openness: dict[int, float] = field(default_factory=dict)

    def last(self) -> tuple[int, Face]:
        k = max(self.faces)
        return k, self.faces[k]

    def first(self) -> tuple[int, Face]:
        k = min(self.faces)
        return k, self.faces[k]

    @property
    def median_cx(self) -> float:
        return float(np.median([f.cx for f in self.faces.values()]))

    @property
    def median_w(self) -> float:
        return float(np.median([f.w for f in self.faces.values()]))


def build_tracks(
    frames_faces: list[list[Face]], frame_w: int, shots: list[int] | None = None, fps: float = ANALYSIS_FPS
) -> list[Track]:
    """Associate detections over time (per shot), merge pieces of the same person and bridge gaps."""
    n = len(frames_faces)
    shots = shots or [0] * n
    coast = max(1, int(round(COAST_SECONDS * fps)))
    tracks: list[Track] = []
    for fi, faces in enumerate(frames_faces):
        pairs = []
        for di, f in enumerate(faces):
            for t in tracks:
                if t.shot != shots[fi]:
                    continue
                last_i, lf = t.last()
                gap = fi - last_i
                if gap <= 0 or gap > coast or not 0.6 < f.w / max(1.0, lf.w) < 1.65:
                    continue
                dist = math.hypot(lf.cx - f.cx, 0.6 * (lf.cy - f.cy)) / max(lf.w, f.w)
                if dist < 0.8 + 0.35 * gap / fps:
                    pairs.append((dist, di, t))
        pairs.sort(key=lambda p: p[0])
        matched: set[int] = set()
        used: set[int] = set()
        for _, di, t in pairs:
            if di in matched or t.tid in used:
                continue
            t.faces[fi] = faces[di]
            t.detected.add(fi)
            matched.add(di)
            used.add(t.tid)
        for di, f in enumerate(faces):
            if di not in matched:
                t = Track(tid=len(tracks), shot=shots[fi], faces={fi: f}, detected={fi})
                tracks.append(t)
    tracks = _merge_tracklets(tracks, fps)
    shot_end = {s: max(i for i in range(n) if shots[i] == s) for s in set(shots)}
    for i, t in enumerate(tracks):
        t.tid = i
        _fill_gaps(t, coast, shot_end.get(t.shot, n - 1))
    return tracks


def _merge_tracklets(tracks: list[Track], fps: float) -> list[Track]:
    """Same shot, no overlap in time, the next piece starts near where the previous one ended -> same person."""
    out: list[Track] = []
    for b in sorted(tracks, key=lambda t: t.first()[0]):
        b_start, fb = b.first()
        best, best_d = None, 0.75
        for a in out:
            a_end, fa = a.last()
            if a.shot != b.shot or a_end >= b_start or b_start - a_end > 4 * fps:
                continue
            if not 0.6 < fb.w / max(1.0, fa.w) < 1.65:
                continue
            d = math.hypot(fa.cx - fb.cx, fa.cy - fb.cy) / max(fa.w, fb.w)
            if d < best_d:
                best, best_d = a, d
        if best is None:
            out.append(b)
        else:
            best.faces.update(b.faces)
            best.detected |= b.detected
    return out


def _fill_gaps(t: Track, coast: int, last_frame: int) -> None:
    """Interpolate short gaps between detections and hold the last position briefly (within the shot)."""
    idx = sorted(t.faces)
    for a, b in zip(idx, idx[1:], strict=False):
        if 1 < b - a <= coast + 1:
            fa, fb = t.faces[a], t.faces[b]
            for k in range(a + 1, b):
                r = (k - a) / (b - a)
                t.faces[k] = Face(fa.x + (fb.x - fa.x) * r, fa.y + (fb.y - fa.y) * r, fa.w + (fb.w - fa.w) * r,
                                  fa.h + (fb.h - fa.h) * r, 0.0)
    last_i, lf = t.last()
    for k in range(last_i + 1, min(last_frame, last_i + coast // 2) + 1):
        t.faces[k] = Face(lf.x, lf.y, lf.w, lf.h, 0.0)


def mouth_activity(tracks: list[Track], env: np.ndarray | None = None, fps: float = ANALYSIS_FPS) -> None:
    """Per track and frame:
    - ``activity``: lip/jaw motion = motion of the mouth patch minus the motion of the eye patch (both cut
      relative to the tracked face), so nodding, swaying or a jittery box do not count;
    - ``openness``: how far the mouth differs from its closed look, which rises and falls with the loudness
      of the voice when this face is the one talking. The closed look is the median patch in the quietest
      moments (pauses between words and sentences); without audio, the median patch."""
    quiet_level = None
    if env is not None and len(env) and np.ptp(env) > 1.0:
        k = max(1, int(round(0.12 * fps)))  # a mouth is still closing/opening right around a sound
        quiet_level = np.array([env[max(0, i - k): i + k + 1].max() for i in range(len(env))])
        silence = float(np.percentile(env, 5)) + 10.0
    for t in tracks:
        _cut_lip_patches(t)
        patches = {fi: f.mouth_patch for fi, f in t.faces.items() if f.mouth_patch is not None}
        if not patches:
            continue
        rest = list(patches.values())
        if quiet_level is not None:
            frames = [fi for fi in patches if fi < len(quiet_level)]
            if len(frames) >= 10:
                q = quiet_level[frames]
                # real silence (pauses); a person who talks non-stop has few of those, so take at least 5
                cutoff = max(min(float(np.percentile(q, 20)), silence), float(np.sort(q)[4]))
                rest = [patches[fi] for fi in frames if quiet_level[fi] <= cutoff] or rest
        template = np.median(np.stack(rest), axis=0)
        t.openness = {fi: float(np.mean(np.abs(p - template))) for fi, p in patches.items()}
        for fi, face in t.faces.items():
            prev = t.faces.get(fi - 1)
            if prev is None or face.mouth_patch is None or prev.mouth_patch is None:
                continue
            dm = float(np.mean(np.abs(face.mouth_patch - prev.mouth_patch)))
            du = 0.0
            if face.upper_patch is not None and prev.upper_patch is not None:
                du = float(np.mean(np.abs(face.upper_patch - prev.upper_patch)))
            t.activity[fi] = max(0.0, dm - 0.8 * du)


# --- who is talking --------------------------------------------------------------------------------


def speech_signals(
    media: Path | None, start: float, n: int, fps: float, words: list[tuple[float, float, str]] | None,
    has_audio: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Per analysis frame: (is somebody talking, loudness envelope in dB)."""
    env = np.full(n, -80.0, dtype=np.float32)
    vad = np.zeros(n, dtype=bool)
    if media is not None and has_audio and n:
        try:
            sr = 8000
            pcm = ffmpeg.decode_pcm(media, sr, start=start, duration=n / fps + 0.2)
            hop = sr / fps
            for i in range(n):
                seg = pcm[int(i * hop): int((i + 1) * hop)]
                if len(seg):
                    env[i] = max(-80.0, 20 * math.log10(float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) + 1e-9))
            vad = env > max(float(np.percentile(env, 10)) + 9.0, -52.0)
        except ffmpeg.FFmpegError as e:
            log.info("No audio for speaker detection: %s", e)
    if not words:
        return vad, env
    times = start + np.arange(n) / fps
    mask = np.zeros(n, dtype=bool)
    for s, e, _ in words:
        mask |= (times >= s - 0.12) & (times <= e + 0.12)
    if vad.any():  # word timings (esp. from subtitles) can span pauses: trust them where the audio is not silent
        mask &= env > float(np.percentile(env, 10)) + 4.0
    return mask, env


def _series(values: dict[int, float], n: int) -> np.ndarray:
    x = np.full(n, np.nan)
    for fi, v in values.items():
        if 0 <= fi < n:
            x[fi] = v
    return x


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    """Mean over k frames ignoring gaps; stays NaN where ``x`` is NaN."""
    ok = np.isfinite(x)
    kernel = np.ones(k)
    s = np.convolve(np.where(ok, x, 0.0), kernel, mode="same")
    c = np.convolve(ok.astype(float), kernel, mode="same")
    out = np.full(len(x), np.nan)
    np.divide(s, c, out=out, where=ok & (c > 0))
    return out


def _windowed_corr(x: np.ndarray, y: np.ndarray, half: int, min_n: int) -> np.ndarray:
    """Pearson correlation of x and y over a sliding window of 2*half+1 frames (gaps ignored, 0 if too few)."""
    ok = np.isfinite(x) & np.isfinite(y)
    xv, yv = np.where(ok, x, 0.0), np.where(ok, y, 0.0)
    kernel = np.ones(2 * half + 1)

    def win(v: np.ndarray) -> np.ndarray:
        return np.convolve(v, kernel, mode="same")

    cnt, sx, sy = win(ok.astype(float)), win(xv), win(yv)
    vx = cnt * win(xv * xv) - sx * sx
    vy = cnt * win(yv * yv) - sy * sy
    cov = cnt * win(xv * yv) - sx * sy
    good = (cnt >= min_n) & (vx > 1e-10 * cnt * cnt) & (vy > 1e-10 * cnt * cnt)
    out = np.zeros(len(x))
    out[good] = cov[good] / np.sqrt(vx[good] * vy[good])
    return out


def _shift(y: np.ndarray, lag: int) -> np.ndarray:
    """y delayed by ``lag`` frames (out[f] = y[f - lag]), NaN where undefined."""
    out = np.full(len(y), np.nan)
    if lag >= 0:
        out[lag:] = y[: len(y) - lag]
    else:
        out[:lag] = y[-lag:]
    return out


def speaker_evidence(tracks: list[Track], n: int, speech: np.ndarray, env: np.ndarray, fps: float) -> np.ndarray:
    """[tracks, frames] evidence that a face is the one talking (only while somebody talks, 0 otherwise):
    how well its mouth opening follows the loudness of the voice (audio-visual sync, at the audio/video
    offset that fits this clip best) plus its share of all lip motion."""
    k = len(tracks)
    ev = np.zeros((k, n), dtype=np.float32)
    if k == 0 or n == 0:
        return ev
    act = np.stack([_smooth(_series(t.activity, n), max(3, int(round(0.2 * fps)))) for t in tracks])
    opening = np.stack([_series(t.openness, n) for t in tracks])
    visible = np.isfinite(act)
    finite = act[visible]
    eps = 0.5 * float(np.median(finite)) + 1e-6 if finite.size else 1e-6  # damps noise of near-still mouths
    n_vis = visible.sum(axis=0)
    act0 = np.where(visible, act, 0.0)
    share = np.where(visible, (act0 + eps) / (act0.sum(axis=0) + eps * np.maximum(n_vis, 1)) - 1.0 / np.maximum(n_vis, 1), 0.0)

    corr = np.zeros((k, n))
    if np.ptp(env) > 1.0:
        half = max(3, int(round(SYNC_WINDOW * fps)))
        y = np.convolve(np.pad(env, 1, mode="edge"), np.ones(3) / 3, mode="valid")
        frames = speech & (n_vis >= 1)
        best = -np.inf
        max_lag = int(round(MAX_AV_LAG * fps))
        for lag in range(-max_lag, max_lag + 1):
            c = np.stack([_windowed_corr(opening[i], _shift(y, lag), half, max(6, half // 2)) for i in range(k)])
            score = float(np.mean(np.max(np.where(visible, c, -1.0), axis=0)[frames])) if frames.any() else 0.0
            score -= 0.01 * abs(lag)  # prefer "in sync" when lags tie
            if score > best:
                best, corr = score, c
    ev = CORR_WEIGHT * corr + SHARE_WEIGHT * share
    ev[:, ~(speech & (n_vis >= 2))] = 0.0  # nothing to decide when nobody talks or only one face is visible
    ev[~visible] = 0.0
    return ev.astype(np.float32)


def choose_speakers(
    tracks: list[Track], evidence: np.ndarray, shots: list[int], fps: float, frame_w: int
) -> list[int | None]:
    """One track id per frame: a Viterbi pass over each shot with a cost per switch (see module docstring)."""
    n = len(shots)
    chosen: list[int | None] = [None] * n
    dt = 1.0 / fps
    min_len = int(round(MIN_TURN * fps))
    for shot in sorted(set(shots)):
        cand = [i for i, t in enumerate(tracks) if t.shot == shot]
        frames = [i for i in range(n) if shots[i] == shot]
        if not cand or not frames:
            continue
        f0, f1 = frames[0], frames[-1] + 1
        m = f1 - f0
        score = np.zeros((len(cand), m), dtype=np.float64)
        for j, ti in enumerate(cand):
            present = np.array([f in tracks[ti].faces for f in range(f0, f1)])
            score[j] = np.where(present, evidence[ti, f0:f1], -ABSENT_COST) * dt
        # tie-break only (it does not accumulate over time): bigger, more central faces first
        max_w = max(tracks[ti].median_w for ti in cand) or 1.0
        prior = np.array([
            0.02 * tracks[ti].median_w / max_w + 0.02 * (1 - abs(tracks[ti].median_cx / frame_w - 0.5) * 2) for ti in cand
        ])
        dp = score[:, 0] + prior
        back = np.zeros((len(cand), m), dtype=np.int32)
        stay_idx = np.arange(len(cand))
        for f in range(1, m):
            best = int(np.argmax(dp))
            switch = dp[best] - SWITCH_COST
            back[:, f] = np.where(dp >= switch, stay_idx, best)
            dp = np.maximum(dp, switch) + score[:, f]
        path = [int(np.argmax(dp))]
        for f in range(m - 1, 0, -1):
            path.append(int(back[path[-1], f]))
        seq = [cand[j] for j in reversed(path)]
        # turns shorter than MIN_TURN (an interruption, a back-channel "ja") keep the previous speaker
        runs = _runs(seq)
        for ri, (a, b, _tid) in enumerate(runs):
            if b - a < min_len and 0 < ri < len(runs) - 1:
                prev_tid = runs[ri - 1][2]
                if all(f0 + f in tracks[prev_tid].faces for f in range(a, b)):
                    seq[a:b] = [prev_tid] * (b - a)
        chosen[f0:f1] = seq
    return chosen


def _runs(seq: list) -> list[tuple[int, int, object]]:
    """[(start, end, value)] of consecutive equal values."""
    runs = []
    a = 0
    for i in range(1, len(seq) + 1):
        if i == len(seq) or seq[i] != seq[a]:
            runs.append((a, i, seq[a]))
            a = i
    return runs


# --- camera ----------------------------------------------------------------------------------------


def camera_targets(
    tracks: list[Track], chosen: list[int | None], shots: list[int], fps: float, crop_w: float, frame_w: int
) -> list[tuple[int, float, bool]]:
    """Waypoints (frame, face centre x, is_cut) in analysis px: one per speaker turn, one when the speaker
    leaves the dead zone (walks, leans), and a cut at the first frame of every shot."""
    out: list[tuple[int, float, bool]] = []
    dz = DEADZONE * crop_w
    settle = max(2, int(round(0.4 * fps)))
    look = max(2, int(round(0.6 * fps)))
    for a, b, (shot, tid) in _runs([(shots[i], chosen[i]) for i in range(len(chosen))]):
        new_shot = a == 0 or shots[a - 1] != shot
        xs = [tracks[tid].faces[f].cx for f in range(a, b) if f in tracks[tid].faces] if tid is not None else []
        if not xs:
            if new_shot:
                out.append((a, frame_w / 2, True))
            continue
        t = tracks[tid]
        target = float(np.median(xs[:look]))
        out.append((a, target, new_shot))
        off = 0
        for f in range(a, b):
            face = t.faces.get(f)
            if face is None:
                continue
            off = off + 1 if abs(face.cx - target) > dz else 0
            if off >= settle:
                target = float(np.median([t.faces[g].cx for g in range(f, min(b, f + look)) if g in t.faces]))
                out.append((f - settle + 1, target, False))
                off = 0
    return out


def build_keyframes(
    targets: list[tuple[int, float, bool]], times_out: list[float], scale: float, src_w: int, crop_w: int,
    joins: list[float] | None = None,
) -> list[tuple]:
    """Waypoints -> crop keyframes in output time: hard cuts at shot changes (and at jump cuts between
    clip segments when a move is due there anyway), eased pans otherwise. Pans never overlap."""
    kf: list[tuple] = []
    max_x = max(0, src_w - crop_w)
    for f, cx, is_cut in targets:
        x = int(min(max_x, max(0, round(cx * scale - crop_w / 2))))
        x -= x % 2
        t = times_out[min(f, len(times_out) - 1)]
        if not kf:
            kf.append((0.0, x))
            continue
        if is_cut:  # the picture cuts: moves still pending for the old shot are cut short, the camera cuts too
            while len(kf) > 1 and kf[-1][0] >= t:
                kf.pop()
            if len(kf) > 1 and len(kf[-1]) > 2 and kf[-1][0] + kf[-1][2] > t:
                pt, px, pd = kf[-1]
                x0 = kf[-2][1]
                p = (t - pt) / pd
                kf[-1] = (pt, int(round(x0 + (px - x0) * p * p * (3 - 2 * p))), round(t - pt, 3))
            if abs(x - kf[-1][1]) >= 2:
                kf.append((round(t, 3), x))
            continue
        prev_t, prev_x = kf[-1][0], kf[-1][1]
        prev_end = prev_t + (kf[-1][2] if len(kf[-1]) > 2 else 0.0)
        if abs(x - prev_x) < MIN_MOVE * crop_w:
            continue
        join = next((j for j in joins or [] if t - 0.6 <= j <= t + 0.25 and j >= prev_end), None)
        if join is not None:  # the picture jumps there anyway: move with the jump instead of panning
            kf.append((round(join, 3), x))
            continue
        start = max(prev_end, t - PAN_LEAD)
        dur = min(1.1, max(0.45, 0.45 + 0.45 * abs(x - prev_x) / crop_w))
        kf.append((round(start, 3), x, round(dur, 3)))
    return kf


# --- main ------------------------------------------------------------------------------------------


def _analyse(media: Path, start: float, end: float, cuts: list[float]) -> tuple[list[list[Face]], list[int], int, int, str]:
    """Stream the clip once: faces (with the pixels around them) and the shot index per analysis frame."""
    detector = FaceDetector()
    if detector.kind == "none":
        return [], [], 0, 0, "none"
    frames_iter, fw, fh = ffmpeg.frame_reader(media, start, end, ANALYSIS_FPS, ANALYSIS_WIDTH)
    every = 1 if detector.kind == "yunet" else 3  # Haar is slow: detect on every third frame, reuse the boxes
    frames_faces: list[list[Face]] = []
    shots: list[int] = []
    held: list[Face] = []
    for fi, frame in enumerate(frames_iter):
        t = start + fi / ANALYSIS_FPS
        shot = sum(1 for c in cuts if c <= t + 0.5 / ANALYSIS_FPS)
        if fi % every == 0 or (shots and shot != shots[-1]):
            held = detector.detect(frame)
            faces = held
        else:
            faces = [Face(f.x, f.y, f.w, f.h, f.score, f.mouth, f.landmarks) for f in held]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for f in faces:
            keep_face_crop(gray, f)
        frames_faces.append(faces)
        shots.append(shot)
    return frames_faces, shots, fw, fh, detector.kind


def plan_crop(
    media: Path,
    info: ffmpeg.MediaInfo,
    segments: list[tuple[float, float]],
    layout: str = "auto",
    scene_cuts: list[float] | None = None,
    words: list[tuple[float, float, str]] | None = None,
) -> CropPlan:
    from app.ai.boundaries import map_to_output_time

    if not info.has_video:
        return CropPlan("audio", 1080, 1920, 1080, 1920)
    size = info.display_size or (1920, 1080)
    src_w, src_h = size
    target_ratio = 9 / 16
    crop_h = src_h
    crop_w = int(round(src_h * target_ratio / 2) * 2)
    if src_w / src_h <= target_ratio + 0.02 or crop_w >= src_w:
        return CropPlan("center", src_w, src_h, min(src_w, crop_w), crop_h)
    if layout in ("center", "fit_blur"):
        return CropPlan(layout, src_w, src_h, crop_w, crop_h)
    fallback = "fit_blur" if layout == "auto" else "center"

    start, end = segments[0][0], segments[-1][1]
    cuts = sorted(c for c in (scene_cuts or []) if start < c < end)
    try:
        frames_faces, shots, fw, fh, kind = _analyse(media, start, end, cuts)
    except ffmpeg.FFmpegError as e:
        log.warning("Frame sampling failed: %s", e)
        return CropPlan(fallback, src_w, src_h, crop_w, crop_h)
    n = len(frames_faces)
    if kind == "none" or n == 0:
        return CropPlan(fallback, src_w, src_h, crop_w, crop_h, detector=kind)

    tracks = build_tracks(frames_faces, fw, shots)
    # drop flickering false positives and tiny background faces
    shot_len = {s: shots.count(s) for s in set(shots)}
    tracks = [
        t for t in tracks
        if len(t.detected) >= max(3, min(0.15 * shot_len[t.shot], 2.0 * ANALYSIS_FPS))
        and float(np.median([f.h for f in t.faces.values()])) >= fh * 0.04
    ]
    for i, t in enumerate(tracks):
        t.tid = i
    if not tracks:
        return CropPlan(fallback, src_w, src_h, crop_w, crop_h, detector=kind)

    speech, env = speech_signals(media, start, n, ANALYSIS_FPS, words, info.has_audio)
    if not words and not info.has_audio:
        speech[:] = True  # no sound at all: go by lip motion alone
    mouth_activity(tracks, env=env)
    evidence = speaker_evidence(tracks, n, speech, env, ANALYSIS_FPS)
    chosen = choose_speakers(tracks, evidence, shots, ANALYSIS_FPS, fw)

    scale = src_w / fw
    out_times = [map_to_output_time(segments, start + i / ANALYSIS_FPS) for i in range(n)]
    for i in range(1, n):  # the camera cuts exactly with the picture, not up to a frame late
        if shots[i] != shots[i - 1]:
            out_times[i] = map_to_output_time(segments, cuts[shots[i] - 1])
    joins = [map_to_output_time(segments, b) for _a, b in segments[:-1]]

    speaker_faces = [tracks[c].faces[i] for i, c in enumerate(chosen) if c is not None and i in tracks[c].faces]
    face_top = float(np.median([f.y / fh for f in speaker_faces])) if speaker_faces else None
    face_bottom = float(np.median([(f.y + f.h) / fh for f in speaker_faces])) if speaker_faces else None
    turns = [r for r in _runs(chosen) if r[2] is not None]
    switches = sum(1 for (_, _, a), (_, _, b) in zip(turns, turns[1:], strict=False) if a != b)
    stats: dict = {
        "tracks": len(tracks), "shots": len(set(shots)), "speech_frames": int(speech.sum()), "frames": n,
        "speaker_switches": switches, "speakers": len({c for c in chosen if c is not None}),
    }

    # Split screen only for two far-apart people in very rapid back-and-forth (following them would whip
    # the camera around).
    if layout in ("auto", "split"):
        talkers = sorted({c for c in chosen if c is not None}, key=lambda c: -chosen.count(c))
        pair = [tracks[c] for c in talkers[:2]]
        if len(pair) < 2:
            pair = sorted(tracks, key=lambda t: -len(t.faces))[:2]
        if len(pair) == 2:
            a, b = pair
            apart = (abs(a.median_cx - b.median_cx) + max(a.median_w, b.median_w)) * scale > 0.92 * crop_w
            rapid = switches >= 4 and (n / ANALYSIS_FPS) / max(1, len(turns)) < 1.6
            both_visible = all(len(t.faces) >= 0.8 * n for t in (a, b))
            if layout == "split" or (apart and rapid and both_visible):
                boxes = []
                for t in sorted((a, b), key=lambda t: t.median_cx):
                    fx = t.median_cx * scale
                    fy = float(np.median([f.cy for f in t.faces.values()])) * scale
                    fwid = t.median_w * scale
                    bw = int(min(src_w, max(fwid * 3.2, src_h * 0.6)))
                    bh = min(int(bw * 960 / 1080), src_h)
                    bw = int(bh * 1080 / 960)
                    x = int(max(0, min(src_w - bw, fx - bw / 2)))
                    y = int(max(0, min(src_h - bh, fy - bh * 0.45)))
                    boxes.append((x - x % 2, y - y % 2, bw - bw % 2, bh - bh % 2))
                log.info("Reframe (%s): split screen, %s", kind, stats)
                return CropPlan("split", src_w, src_h, crop_w, crop_h, split_boxes=boxes, detector=kind,
                                face_top_ratio=face_top, face_bottom_ratio=face_bottom, stats=stats)

    targets = camera_targets(tracks, chosen, shots, ANALYSIS_FPS, crop_w / scale, fw)
    keyframes = build_keyframes(targets, out_times, scale, src_w, crop_w, joins)
    plan = CropPlan("face", src_w, src_h, crop_w, crop_h, keyframes=keyframes, face_top_ratio=face_top,
                    face_bottom_ratio=face_bottom, detector=kind, stats=stats)
    inside = []
    for i, c in enumerate(chosen):
        face = tracks[c].faces.get(i) if c is not None else None
        if face is not None:
            x = plan.x_at(out_times[i])
            inside.append(face.x * scale >= x - 2 and (face.x + face.w) * scale <= x + crop_w + 2)
    stats["speaker_in_frame"] = round(sum(inside) / len(inside), 3) if inside else None
    stats["camera_moves"] = len(keyframes) - 1 if keyframes else 0
    log.info("Reframe (%s): %s", kind, stats)
    return plan
