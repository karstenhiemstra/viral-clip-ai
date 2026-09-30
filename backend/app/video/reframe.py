"""16:9 -> 9:16 reframing: keep the relevant (speaking) person in frame.

1. Sample frames across the clip (4 fps, 480px) and detect faces
   (YuNet DNN when its model file is available, otherwise OpenCV's bundled Haar cascades).
2. Track faces over time and measure mouth-region motion per track -> "who is talking" heuristic.
3. Pick a subject per frame with stickiness (no ping-ponging) and build a piecewise-constant crop:
   the frame only moves on a speaker switch, a camera cut, or when the subject clearly moves -
   which looks like deliberate editing instead of a jittery auto-pan.
4. Choose a layout: face crop, split screen (two active speakers), or blurred-background fit when
   there are no faces (gaming, landscapes, screen recordings).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.config import get_settings
from app.video import ffmpeg

log = logging.getLogger(__name__)

SAMPLE_FPS = 4.0
ANALYSIS_WIDTH = 640


@dataclass
class Face:
    x: float
    y: float
    w: float
    h: float
    score: float = 1.0
    mouth: tuple[float, float, float, float] | None = None  # x, y, w, h of the mouth region

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
    # piecewise constant crop x (source px) over OUTPUT time: [(t_start, x), ...]
    keyframes: list[tuple[float, int]] = field(default_factory=list)
    split_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    face_top_ratio: float | None = None
    face_bottom_ratio: float | None = None
    detector: str = "none"

    def x_expression(self) -> str:
        """ffmpeg expression for the crop x over output time ``t`` (use inside single quotes)."""
        if not self.keyframes:
            return str(max(0, (self.src_w - self.crop_w) // 2))
        expr = str(self.keyframes[-1][1])
        for i in range(len(self.keyframes) - 2, -1, -1):
            nxt_t = self.keyframes[i + 1][0]
            expr = f"if(lt(t,{nxt_t:.3f}),{self.keyframes[i][1]},{expr})"
        return expr

    def to_dict(self) -> dict:
        return {
            "layout": self.layout, "crop_w": self.crop_w, "crop_h": self.crop_h, "keyframes": self.keyframes,
            "split_boxes": self.split_boxes, "detector": self.detector,
            "face_top_ratio": self.face_top_ratio, "face_bottom_ratio": self.face_bottom_ratio,
        }


class FaceDetector:
    _lock = threading.Lock()
    _yunet_failed = False

    def __init__(self) -> None:
        self.kind = "none"
        self._yunet = None
        self._haar: list = []
        model = get_settings().face_model_path
        if not model.exists() and not FaceDetector._yunet_failed:
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
    def _try_download(cls, target: Path) -> None:
        url = get_settings().face_model_url
        with cls._lock:
            if target.exists() or cls._yunet_failed or not url:
                cls._yunet_failed = cls._yunet_failed or not url
                return
            try:
                import httpx

                resp = httpx.get(url, follow_redirects=True, timeout=20)
                if resp.status_code == 200 and len(resp.content) > 100_000:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(resp.content)
                    log.info("Downloaded YuNet face model to %s", target)
                    return
            except Exception as e:  # pragma: no cover - network specific
                log.info("YuNet download failed: %s", e)
            cls._yunet_failed = True

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if self._yunet is not None:
            self._yunet.setInputSize((w, h))
            _, faces = self._yunet.detect(frame)
            out = []
            for f in faces if faces is not None else []:
                x, y, fw, fh = map(float, f[:4])
                rmx, rmy, lmx, lmy = map(float, f[10:14])
                mw = max(4.0, abs(lmx - rmx) * 1.4)
                mouth = (min(rmx, lmx) - mw * 0.15, min(rmy, lmy) - fh * 0.12, mw, fh * 0.28)
                out.append(Face(x, y, fw, fh, float(f[14]), mouth))
            return [f for f in out if f.w > w * 0.03]
        if self._haar:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            boxes = self._haar_boxes(gray, h, w)
            if not boxes:
                # Dark / low-contrast footage: retry with LOCAL contrast enhancement. (Global histogram
                # equalisation is avoided on purpose: a flat background dominates the histogram and
                # washes out the face - measured 0/40 vs 40/40 detections on a test clip.)
                boxes = self._haar_boxes(_CLAHE.apply(gray), h, w)
            faces = [Face(x, y, bw, bh, 1.0, (x + bw * 0.25, y + bh * 0.62, bw * 0.5, bh * 0.3)) for x, y, bw, bh in boxes]
            return _nms(faces)
        return []

    def _haar_boxes(self, gray: np.ndarray, h: int, w: int) -> list[tuple[int, int, int, int]]:
        side = max(20, int(h * 0.06))
        boxes: list[tuple[int, int, int, int]] = []
        for i, clf in enumerate(self._haar):
            found = clf.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(side, side))
            boxes += [tuple(map(int, b)) for b in found]
            if i == 1:  # profile cascade only detects one orientation; also run it mirrored
                flipped = clf.detectMultiScale(cv2.flip(gray, 1), scaleFactor=1.1, minNeighbors=6, minSize=(side, side))
                boxes += [(w - x - bw, y, bw, bh) for (x, y, bw, bh) in map(lambda b: tuple(map(int, b)), flipped)]
        return boxes


_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


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


@dataclass
class Track:
    tid: int
    faces: dict[int, Face] = field(default_factory=dict)
    activity: dict[int, float] = field(default_factory=dict)

    def last(self) -> tuple[int, Face]:
        k = max(self.faces)
        return k, self.faces[k]


def build_tracks(frames_faces: list[list[Face]], frame_w: int) -> list[Track]:
    tracks: list[Track] = []
    for fi, faces in enumerate(frames_faces):
        used: set[int] = set()
        for f in sorted(faces, key=lambda f: f.w * f.h, reverse=True):
            best, best_d = None, frame_w * 0.12
            for t in tracks:
                if t.tid in used:
                    continue
                last_i, last_f = t.last()
                if fi - last_i > 6:
                    continue
                d = abs(last_f.cx - f.cx) + 0.5 * abs(last_f.cy - f.cy)
                if d < best_d and 0.5 < f.w / max(1.0, last_f.w) < 2.0:
                    best, best_d = t, d
            if best is None:
                best = Track(tid=len(tracks))
                tracks.append(best)
            best.faces[fi] = f
            used.add(best.tid)
    return tracks


def mouth_activity(tracks: list[Track], frames: np.ndarray) -> None:
    """Mean absolute frame difference in each track's mouth region (talking faces move their mouth)."""
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    for t in tracks:
        for fi, face in t.faces.items():
            if fi - 1 not in t.faces or face.mouth is None:
                continue
            x, y, w, h = (int(round(v)) for v in face.mouth)
            x, y = max(0, x), max(0, y)
            a = gray[fi][y : y + max(2, h), x : x + max(2, w)]
            b = gray[fi - 1][y : y + max(2, h), x : x + max(2, w)]
            if a.size == 0 or a.shape != b.shape:
                continue
            t.activity[fi] = float(np.mean(cv2.absdiff(a, b))) / 255.0


def _choose_subjects(tracks: list[Track], n_frames: int, frame_w: int) -> list[int | None]:
    """Per-frame track id with hysteresis: switch only when another face is clearly more active."""
    if not tracks:
        return [None] * n_frames
    sizes = {t.tid: np.mean([f.w for f in t.faces.values()]) for t in tracks}
    max_size = max(sizes.values()) or 1.0
    chosen: list[int | None] = []
    current: int | None = None
    challenger: int | None = None
    streak = 0
    for fi in range(n_frames):
        present = [t for t in tracks if fi in t.faces]
        if not present:
            chosen.append(current)
            continue
        scores = {}
        for t in present:
            act = np.mean([t.activity.get(j, 0.0) for j in range(fi - 3, fi + 4)])
            scores[t.tid] = 0.65 * act * 20 + 0.35 * sizes[t.tid] / max_size
        best = max(scores, key=scores.get)
        if current is None or current not in scores:
            current, challenger, streak = best, None, 0
        elif best != current and scores[best] > scores[current] * 1.25:
            if challenger == best:
                streak += 1
            else:
                challenger, streak = best, 1
            if streak >= 5:  # ~1.25 s at 4 fps
                current, challenger, streak = best, None, 0
        else:
            challenger, streak = None, 0
        chosen.append(current)
    return chosen


def _segments_from_targets(targets: list[float | None], times: list[float], cuts: list[float], min_move: float, min_len: float) -> list[tuple[float, float]]:
    """Piecewise-constant crop centres from noisy per-frame targets."""
    filled: list[float] = []
    last = next((t for t in targets if t is not None), None)
    for t in targets:
        if t is not None:
            last = t
        filled.append(last if last is not None else 0.5)
    segs: list[tuple[float, list[float]]] = []
    for i, (t, x) in enumerate(zip(times, filled, strict=False)):
        if not segs:
            segs.append((t, [x]))
            continue
        seg_t, vals = segs[-1]
        centre = float(np.median(vals))
        cut_here = any(times[i - 1] < c <= t for c in cuts)
        window = filled[i : i + 3]
        sustained = len(window) >= 2 and all(abs(v - centre) > min_move for v in window)
        if (cut_here and abs(x - centre) > min_move * 0.5) or (sustained and t - seg_t >= min_len):
            segs.append((t, [x]))
        else:
            vals.append(x)
    return [(t, float(np.median(v))) for t, v in segs]


def plan_crop(
    media: Path,
    info: ffmpeg.MediaInfo,
    segments: list[tuple[float, float]],
    layout: str = "auto",
    scene_cuts: list[float] | None = None,
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

    start, end = segments[0][0], segments[-1][1]
    try:
        frames, fw, fh = ffmpeg.read_frames(media, start, end, SAMPLE_FPS, ANALYSIS_WIDTH)
    except ffmpeg.FFmpegError as e:
        log.warning("Frame sampling failed: %s", e)
        return CropPlan("fit_blur" if layout == "auto" else "center", src_w, src_h, crop_w, crop_h)
    detector = FaceDetector()
    if detector.kind == "none" or len(frames) == 0:
        return CropPlan("fit_blur" if layout == "auto" else "center", src_w, src_h, crop_w, crop_h, detector=detector.kind)

    frames_faces = [detector.detect(f) for f in frames]
    tracks = build_tracks(frames_faces, fw)
    mouth_activity(tracks, frames)
    n = len(frames)
    strong = [t for t in tracks if len(t.faces) / n >= 0.2]
    scale = src_w / fw
    src_times = [start + i / SAMPLE_FPS for i in range(n)]
    out_times = [map_to_output_time(segments, t) for t in src_times]
    out_cuts = [map_to_output_time(segments, c) for c in (scene_cuts or []) if start < c < end]

    if not strong:
        return CropPlan("fit_blur" if layout == "auto" else "center", src_w, src_h, crop_w, crop_h, detector=detector.kind)

    all_faces = [f for t in strong for f in t.faces.values()]
    face_top = float(np.median([f.y / fh for f in all_faces]))
    face_bottom = float(np.median([(f.y + f.h) / fh for f in all_faces]))

    # Split screen: two faces that are both on screen most of the time, far apart, and both talk.
    if layout in ("auto", "split") and len(strong) >= 2:
        a, b = sorted(strong, key=lambda t: len(t.faces), reverse=True)[:2]
        both = sum(1 for i in range(n) if i in a.faces and i in b.faces) / n
        dist = abs(np.median([f.cx for f in a.faces.values()]) - np.median([f.cx for f in b.faces.values()])) * scale
        act_a, act_b = sum(a.activity.values()), sum(b.activity.values())
        balance = min(act_a, act_b) / max(1e-6, max(act_a, act_b))
        if layout == "split" or (both >= 0.7 and dist > crop_w * 0.9 and balance >= 0.45):
            boxes = []
            for t in sorted((a, b), key=lambda t: np.median([f.cx for f in t.faces.values()])):
                fx = float(np.median([f.cx for f in t.faces.values()])) * scale
                fy = float(np.median([f.cy for f in t.faces.values()])) * scale
                fwid = float(np.median([f.w for f in t.faces.values()])) * scale
                bw = int(min(src_w, max(fwid * 3.2, src_h * 0.6)))
                bh = int(bw * 960 / 1080)
                bh = min(bh, src_h)
                bw = int(bh * 1080 / 960)
                x = int(max(0, min(src_w - bw, fx - bw / 2)))
                y = int(max(0, min(src_h - bh, fy - bh * 0.45)))
                boxes.append((x - x % 2, y - y % 2, bw - bw % 2, bh - bh % 2))
            return CropPlan("split", src_w, src_h, crop_w, crop_h, split_boxes=boxes, detector=detector.kind)

    chosen = _choose_subjects(strong, n, fw)
    by_id = {t.tid: t for t in strong}
    targets: list[float | None] = []
    for fi, tid in enumerate(chosen):
        if tid is None:
            targets.append(None)
            continue
        t = by_id[tid]
        face = t.faces.get(fi)
        if face is None:
            near = min(t.faces, key=lambda k: abs(k - fi))
            face = t.faces[near]
        targets.append(face.cx / fw)
    segs = _segments_from_targets(targets, out_times, out_cuts, min_move=0.08, min_len=1.2)
    keyframes: list[tuple[float, int]] = []
    for t, cx in segs:
        x = int(round(cx * src_w - crop_w / 2))
        x = max(0, min(src_w - crop_w, x))
        x -= x % 2
        if keyframes and abs(keyframes[-1][1] - x) < 4:
            continue
        keyframes.append((0.0 if not keyframes else round(t, 3), x))
    return CropPlan(
        "face", src_w, src_h, crop_w, crop_h, keyframes=keyframes, face_top_ratio=face_top,
        face_bottom_ratio=face_bottom, detector=detector.kind,
    )
