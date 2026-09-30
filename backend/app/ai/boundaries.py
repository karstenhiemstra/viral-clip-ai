"""Stage 10 - clip optimisation: exact, hook-first start/end times.

Rules (in order):
1. Drop leading sentences that are pure filler/greetings and trailing outro sentences.
2. Within the allowed span, pick the contiguous sub-span that best fits the duration range, prefers
   starting on the hook sentence, keeps the loudest reaction / audience hotspot and avoids starting on
   filler or a context-dependent opener ("Hij...", "Maar...").
3. Trim filler words off the first sentence ("Nou, eh, dus ...").
4. Pad: a hair before the first word, and let a laugh/reaction land after the last word
   (never bleeding into the next word).
5. Optional dead-air removal: pauses longer than ``silence_min_gap`` are shortened to ~0.25s
   (jump cuts keep retention up). Returned as source-time segments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.signals import VideoContext, window_features
from app.ai.transcript import (
    Sentence,
    contains_outro,
    is_filler_sentence,
    leading_filler_count,
    starts_with_context_opener,
)
from app.video.audio_features import loud_tail

_END_PUNCT = re.compile(r"[.!?…]+[\"'”’)]*$")


@dataclass
class DurationRules:
    min_seconds: float = 12.0
    max_seconds: float = 18.0
    target_seconds: float = 15.0
    remove_silences: bool = True
    silence_min_gap: float = 0.6
    tolerance: float = 3.0  # may exceed max by this much to finish a sentence


@dataclass
class ClipWindow:
    s0: int
    s1: int
    w0: int
    w1: int  # exclusive
    start: float
    end: float
    segments: list[tuple[float, float]]
    flags: list[str] = field(default_factory=list)

    @property
    def source_duration(self) -> float:
        return self.end - self.start

    @property
    def duration(self) -> float:
        return sum(b - a for a, b in self.segments)


def _span_duration(ctx: VideoContext, a: int, b: int) -> float:
    return ctx.sentences[b].end - ctx.sentences[a].start


def _subspan_score(ctx: VideoContext, a: int, b: int, rules: DurationRules, hook_s: int | None, key_times: list[float]) -> float:
    s_a, s_b = ctx.sentences[a], ctx.sentences[b]
    dur = s_b.end - s_a.start
    score = 0.0
    # duration fit
    if dur < rules.min_seconds:
        score -= (rules.min_seconds - dur) * 4
    elif dur > rules.max_seconds:
        score -= (dur - rules.max_seconds) * 5
    score -= abs(dur - rules.target_seconds) * 0.8
    # hook-first
    if hook_s is not None:
        if a == hook_s:
            score += 12
        elif a < hook_s <= b:
            score += 4 - 2.5 * (ctx.sentences[hook_s].start - s_a.start)
        else:
            score -= 15
    # keep the key moments (audio peak, audience hotspot)
    for t in key_times:
        if s_a.start - 0.5 <= t <= s_b.end + 0.5:
            score += 6
    first_words = ctx.words[s_a.w0 : s_a.w1]
    if leading_filler_count(first_words) >= len(first_words):
        score -= 12
    if starts_with_context_opener(s_a.text):
        score -= 5
    if not _END_PUNCT.search(s_b.text.strip()):
        score -= 2
    if contains_outro(s_a.text) or contains_outro(s_b.text):
        score -= 10
    return score


def _key_times(ctx: VideoContext, a: int, b: int) -> list[float]:
    lo, hi = ctx.sentences[a].start, ctx.sentences[b].end
    out = []
    feats = window_features(ctx, lo, hi, ctx.words[ctx.sentences[a].w0 : ctx.sentences[b].w1])
    if ctx.audio is not None and feats.get("audio_peak_z", 0) > 1.5:
        rel = feats.get("audio_peak_pos", 0.5)
        out.append(lo + rel * (hi - lo))
    for h in ctx.hotspots:
        if lo <= h.get("time", -1) <= hi and h.get("strength", 0) >= 0.2:
            out.append(float(h["time"]))
    return out


def choose_span(ctx: VideoContext, s0: int, s1: int, rules: DurationRules, hook_s: int | None) -> tuple[int, int]:
    """Best contiguous sentence span inside [s0, s1], extending outward when the span is too short."""
    n = len(ctx.sentences)
    s0, s1 = max(0, min(s0, n - 1)), max(0, min(s1, n - 1))
    if s1 < s0:
        s0, s1 = s1, s0
    # 1. strip filler/outro edges
    while s0 < s1 and (is_filler_sentence(ctx.sentences[s0], ctx.words) or contains_outro(ctx.sentences[s0].text)):
        s0 += 1
    while s1 > s0 and (is_filler_sentence(ctx.sentences[s1], ctx.words) or contains_outro(ctx.sentences[s1].text)):
        s1 -= 1
    # 2. extend if too short: prefer continuing forward (payoff), then include setup before.
    hard_max = rules.max_seconds + rules.tolerance
    while _span_duration(ctx, s0, s1) < rules.min_seconds:
        grew = False
        if s1 + 1 < n:
            gap = ctx.sentences[s1 + 1].start - ctx.sentences[s1].end
            if gap < 2.0 and _span_duration(ctx, s0, s1 + 1) <= hard_max and not contains_outro(ctx.sentences[s1 + 1].text):
                s1 += 1
                grew = True
        if _span_duration(ctx, s0, s1) >= rules.min_seconds:
            break
        if s0 - 1 >= 0:
            prev = ctx.sentences[s0 - 1]
            gap = ctx.sentences[s0].start - prev.end
            if gap < 2.0 and not is_filler_sentence(prev, ctx.words) and _span_duration(ctx, s0 - 1, s1) <= hard_max:
                s0 -= 1
                grew = True
        if not grew:
            break
    # 3. if too long, search the best sub-span
    if _span_duration(ctx, s0, s1) > rules.max_seconds:
        key_times = _key_times(ctx, s0, s1)
        best, best_score = (s0, s1), float("-inf")
        for a in range(s0, s1 + 1):
            for b in range(a, s1 + 1):
                if _span_duration(ctx, a, b) > hard_max:
                    break
                sc = _subspan_score(ctx, a, b, rules, hook_s, key_times)
                if sc > best_score:
                    best, best_score = (a, b), sc
        s0, s1 = best
    return s0, s1


def _cut_long_sentence(ctx: VideoContext, w0: int, w1: int, rules: DurationRules) -> int:
    """For a single run-on sentence longer than allowed: cut at the best pause/comma near the target."""
    start = ctx.words[w0].start
    best_i, best_cost = w1, float("inf")
    for i in range(w0 + 1, w1 + 1):
        dur = ctx.words[i - 1].end - start
        if dur < rules.min_seconds * 0.8:
            continue
        if dur > rules.max_seconds + rules.tolerance:
            break
        w = ctx.words[i - 1]
        gap = (ctx.words[i].start - w.end) if i < len(ctx.words) else 1.0
        cost = abs(dur - rules.target_seconds) - (3 if _END_PUNCT.search(w.text) else 0) - (1.5 if w.text.endswith(",") else 0) - min(2.0, gap * 3)
        if cost < best_cost:
            best_i, best_cost = i, cost
    return best_i


def optimize_boundaries(
    ctx: VideoContext, s0: int, s1: int, rules: DurationRules, hook_s: int | None = None
) -> ClipWindow:
    s0, s1 = choose_span(ctx, s0, s1, rules, hook_s)
    sa: Sentence = ctx.sentences[s0]
    sb: Sentence = ctx.sentences[s1]
    w0, w1 = sa.w0, sb.w1
    flags: list[str] = []

    lead = leading_filler_count(ctx.words[w0:w1])
    if lead and (w1 - w0 - lead) >= 3:
        w0 += lead
    if ctx.words[w1 - 1].end - ctx.words[w0].start > rules.max_seconds + rules.tolerance:
        w1 = _cut_long_sentence(ctx, w0, w1, rules)

    first, last = ctx.words[w0], ctx.words[w1 - 1]
    prev_end = ctx.words[w0 - 1].end if w0 > 0 else 0.0
    next_start = ctx.words[w1].start if w1 < len(ctx.words) else ctx.duration
    start = max(prev_end + 0.02, first.start - 0.12, 0.0)
    tail = 0.35 + loud_tail(ctx.audio, last.end, max_extend=1.2)
    end = min(last.end + tail, next_start - 0.03, ctx.duration or last.end + tail)
    end = max(end, last.end + 0.08)

    if w0 > 0 and not _END_PUNCT.search(ctx.words[w0 - 1].text) and first.start - prev_end < 0.35 and lead == 0:
        flags.append("starts_mid_sentence")
    if not _END_PUNCT.search(last.text) and next_start - last.end < 0.3:
        flags.append("ends_mid_sentence")

    segments: list[tuple[float, float]] = [(start, end)]
    if rules.remove_silences:
        segments = []
        seg_start = start
        for i in range(w0, w1 - 1):
            gap = ctx.words[i + 1].start - ctx.words[i].end
            if gap > rules.silence_min_gap:
                segments.append((seg_start, ctx.words[i].end + 0.13))
                seg_start = ctx.words[i + 1].start - 0.12
        segments.append((seg_start, end))
        segments = [(round(a, 3), round(b, 3)) for a, b in segments if b - a > 0.05]

    # Map sentence indices to the (possibly word-trimmed) window for reporting.
    return ClipWindow(s0=s0, s1=s1, w0=w0, w1=w1, start=round(start, 3), end=round(end, 3), segments=segments, flags=flags)


def map_to_output_time(segments: list[tuple[float, float]], t: float) -> float:
    """Source time -> time in the rendered clip (after dead-air removal). Times that fall inside a
    removed gap clamp to the cut point."""
    acc = 0.0
    for a, b in segments:
        if t <= a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc
