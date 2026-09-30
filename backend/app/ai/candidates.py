"""Stage 1 - candidate generation (recall).

Two independent generators are merged:
* signal windows - sliding windows scored on audio peaks, audience hotspots and lexical hooks
  (free, always available, catches loud reactions an LLM cannot hear);
* LLM pass 1 - a cheap model reads the transcript in chunks and proposes 30-50 moments.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai.llm import LLMClient, LLMError, UsageMeter
from app.ai.prompts import CANDIDATE_SCHEMA, CANDIDATE_SYSTEM, candidate_user_prompt
from app.ai.signals import VideoContext, signal_score, window_features
from app.ai.transcript import contains_outro, is_filler_sentence
from app.video.audio_features import find_peaks

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    cid: str
    s0: int
    s1: int
    hook_s: int | None = None
    sources: set[str] = field(default_factory=set)
    category: str | None = None
    description: str = ""
    reason: str = ""
    strength: float = 0.0  # LLM gut feeling 1-10
    signal: float = 0.0
    evaluation: dict[str, Any] | None = None

    def span(self, ctx: VideoContext) -> tuple[float, float]:
        return ctx.sentences[self.s0].start, ctx.sentences[self.s1].end

    @property
    def priority(self) -> float:
        if "llm" in self.sources:
            return 0.6 * self.strength * 10 + 0.4 * self.signal + (5 if "signal" in self.sources else 0)
        return 0.85 * self.signal

    def to_log(self, ctx: VideoContext) -> dict[str, Any]:
        a, b = self.span(ctx)
        return {
            "id": self.cid, "start": round(a, 2), "end": round(b, 2), "sources": sorted(self.sources),
            "category": self.category, "strength": self.strength, "signal": self.signal,
            "description": self.description, "reason": self.reason,
        }


def iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def signal_candidates(ctx: VideoContext, count: int, min_s: float, max_s: float, target_s: float) -> list[Candidate]:
    """Sliding windows starting at every sentence; for each start the best-scoring length within the
    duration range is kept, then non-maximum suppression picks diverse, high-signal windows."""
    sents = ctx.sentences
    windows: list[tuple[float, int, int]] = []
    lo, hi = min_s * 0.8, max_s * 1.3
    for i, s in enumerate(sents):
        if is_filler_sentence(s, ctx.words) or contains_outro(s.text):
            continue
        best: tuple[float, int] | None = None
        for j in range(i, len(sents)):
            dur = sents[j].end - s.start
            if dur > hi:
                break
            if dur < lo:
                continue
            feats = window_features(ctx, s.start, sents[j].end, ctx.words[s.w0 : sents[j].w1])
            score = signal_score(feats) - 0.6 * abs(dur - target_s)
            if best is None or score > best[0]:
                best = (score, j)
        if best is None:
            # a single long sentence: still consider it
            j = i
            if sents[j].end - s.start > hi or sents[j].end - s.start < min_s * 0.5:
                continue
            feats = window_features(ctx, s.start, sents[j].end, ctx.words[s.w0 : sents[j].w1])
            best = (signal_score(feats), j)
        windows.append((best[0], i, best[1]))
    windows.sort(reverse=True)
    picked: list[Candidate] = []
    for score, a, b in windows:
        span = (sents[a].start, sents[b].end)
        if any(iou(span, c.span(ctx)) > 0.3 for c in picked):
            continue
        picked.append(Candidate(cid="", s0=a, s1=b, hook_s=a, sources={"signal"}, signal=round(score, 1)))
        if len(picked) >= count:
            break
    return picked


def chunk_sentences(ctx: VideoContext, chunk_seconds: float = 360.0) -> list[tuple[int, int]]:
    """Consecutive sentence ranges (inclusive) of about ``chunk_seconds`` with a small overlap."""
    sents = ctx.sentences
    if not sents:
        return []
    if sents[-1].end - sents[0].start <= chunk_seconds * 1.6:
        return [(0, len(sents) - 1)]
    chunks = []
    a = 0
    while a < len(sents):
        b = a
        while b + 1 < len(sents) and sents[b + 1].end - sents[a].start <= chunk_seconds:
            b += 1
        chunks.append((a, b))
        if b >= len(sents) - 1:
            break
        # overlap ~30s so moments on a chunk border are not lost
        nxt = b
        while nxt > a and sents[b].end - sents[nxt].start < 30:
            nxt -= 1
        a = max(a + 1, nxt)
    return chunks


def llm_candidates(
    llm: LLMClient,
    ctx: VideoContext,
    *,
    target_count: int,
    min_s: float,
    max_s: float,
    output_language: str,
    meter: UsageMeter,
    progress: Callable[[float], None] | None = None,
    warnings: list[str] | None = None,
) -> list[Candidate]:
    chunks = chunk_sentences(ctx)
    peaks = find_peaks(ctx.audio) if ctx.audio is not None else []
    total = max(1.0, ctx.duration)
    out: list[Candidate] = []
    for ci, (a, b) in enumerate(chunks):
        chunk = ctx.sentences[a : b + 1]
        span = chunk[-1].end - chunk[0].start
        n = max(3, min(12, math.ceil(target_count * span / total) + 2))
        prompt = candidate_user_prompt(
            title=ctx.title, creator=ctx.creator, duration=ctx.duration, chunk=chunk, max_moments=n,
            min_seconds=min_s, max_seconds=max_s, output_language=output_language, hotspots=ctx.hotspots,
            audio_peaks=peaks,
        )
        try:
            res = llm.complete_json(
                system=CANDIDATE_SYSTEM, user=prompt, schema=CANDIDATE_SCHEMA, schema_name="moments", tier="fast",
                max_tokens=4000,
            )
        except LLMError as e:
            log.warning("Candidate pass failed for chunk %s: %s", ci, e)
            if warnings is not None:
                warnings.append(f"Pass 1 chunk {ci + 1}: {e}")
            continue
        meter.add(res.usage, "candidates")
        for m in res.data.get("moments", []) or []:
            try:
                s0, s1 = int(m["start_sentence"]), int(m["end_sentence"])
                hook = int(m.get("hook_sentence", s0))
            except (KeyError, TypeError, ValueError):
                continue
            if s1 < s0:
                s0, s1 = s1, s0
            if s0 < a - 2 or s1 > b + 2 or s0 < 0 or s1 >= len(ctx.sentences):
                continue
            if ctx.sentences[s1].end - ctx.sentences[s0].start > max_s * 3:
                continue
            out.append(
                Candidate(
                    cid="",
                    s0=s0,
                    s1=s1,
                    hook_s=hook if s0 <= hook <= s1 else s0,
                    sources={"llm"},
                    category=m.get("category"),
                    description=str(m.get("description", ""))[:300],
                    reason=str(m.get("reason", ""))[:300],
                    strength=float(max(1, min(10, int(m.get("strength", 5) or 5)))),
                )
            )
        if progress:
            progress((ci + 1) / len(chunks))
    return out


def merge_candidates(ctx: VideoContext, groups: list[list[Candidate]], iou_threshold: float = 0.5) -> list[Candidate]:
    merged: list[Candidate] = []
    for group in groups:
        for c in group:
            twin = next((m for m in merged if iou(m.span(ctx), c.span(ctx)) >= iou_threshold), None)
            if twin is None:
                merged.append(c)
                continue
            # Prefer LLM boundaries/text; keep the strongest evidence of both.
            if "llm" in c.sources and "llm" not in twin.sources:
                c.sources |= twin.sources
                c.signal = max(c.signal, twin.signal)
                merged[merged.index(twin)] = c
            else:
                twin.sources |= c.sources
                twin.signal = max(twin.signal, c.signal)
                twin.strength = max(twin.strength, c.strength)
    # compute signal scores for LLM-only candidates so priorities are comparable
    for c in merged:
        if c.signal == 0.0:
            a, b = c.span(ctx)
            c.signal = signal_score(window_features(ctx, a, b, ctx.words[ctx.sentences[c.s0].w0 : ctx.sentences[c.s1].w1]))
    merged.sort(key=lambda c: c.priority, reverse=True)
    for i, c in enumerate(merged):
        c.cid = f"c{i + 1}"
    return merged
