"""Optional pass 5 - vision check for the best candidates only (cost control).

Architecture hook for richer multimodal analysis: ``VisionAnalyzer.analyze`` receives the media file
and a time span and returns visual scores that the pipeline blends into hook/emotion. Swap in a
different implementation (face-expression model, object detector, video-LLM) without touching the
rest of the pipeline.
"""

from __future__ import annotations

import base64
import logging
import tempfile
from pathlib import Path
from typing import Any, Protocol

from app.ai.llm import ImageInput, LLMClient, LLMError, UsageMeter
from app.ai.prompts import VISION_SCHEMA, VISION_SYSTEM
from app.video import ffmpeg

log = logging.getLogger(__name__)


class VisionAnalyzer(Protocol):
    def analyze(self, media: Path, start: float, end: float, transcript: str) -> dict[str, Any] | None: ...


class NullVisionAnalyzer:
    def analyze(self, media: Path, start: float, end: float, transcript: str) -> dict[str, Any] | None:
        return None


class LLMVisionAnalyzer:
    def __init__(self, llm: LLMClient, meter: UsageMeter, frames: int = 4):
        self.llm = llm
        self.meter = meter
        self.frames = frames

    def analyze(self, media: Path, start: float, end: float, transcript: str) -> dict[str, Any] | None:
        images: list[ImageInput] = []
        with tempfile.TemporaryDirectory(prefix="vc-vis-") as tmp:
            for i in range(self.frames):
                t = start + (end - start) * (0.08 + 0.84 * i / max(1, self.frames - 1))
                out = Path(tmp) / f"f{i}.jpg"
                try:
                    ffmpeg.extract_frame(media, t, out, width=512)
                    images.append(ImageInput("image/jpeg", base64.b64encode(out.read_bytes()).decode()))
                except ffmpeg.FFmpegError:
                    continue
        if not images:
            return None
        try:
            res = self.llm.complete_json(
                system=VISION_SYSTEM,
                user=f"Transcript of the clip:\n{transcript[:1500]}\n\nFrames are in chronological order.",
                schema=VISION_SCHEMA,
                schema_name="vision",
                tier="vision",
                max_tokens=1500,
                images=images,
            )
        except LLMError as e:
            log.warning("Vision analysis failed: %s", e)
            return None
        self.meter.add(res.usage, "vision")
        d = res.data
        return {
            "visual_interest": max(0, min(100, int(d.get("visual_interest", 50)))),
            "visual_hook": max(0, min(100, int(d.get("visual_hook", 50)))),
            "faces_visible": bool(d.get("faces_visible", False)),
            "notes": str(d.get("notes", ""))[:300],
        }


def blend_vision(scores: dict[str, float], vision: dict[str, Any] | None) -> dict[str, float]:
    if not vision:
        return scores
    out = dict(scores)
    out["hook"] = round(0.85 * out.get("hook", 50) + 0.15 * vision["visual_hook"], 1)
    out["emotion"] = round(0.9 * out.get("emotion", 50) + 0.1 * vision["visual_interest"], 1)
    out["retention"] = round(0.9 * out.get("retention", 50) + 0.1 * vision["visual_interest"], 1)
    return out
