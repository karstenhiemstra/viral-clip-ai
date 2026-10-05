"""Stage 10 - clip optimisation: exact, hook-first start/end times.

Rules (in order):
1. Drop leading sentences that are pure filler/greetings and trailing outro sentences, and never
   grow a clip across a clear topic change (``VideoContext.topic_breaks``).
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

from app.ai.signals import VideoContext, punchline_hits, signal_score, window_features
from app.ai.transcript import (
    Sentence,
    contains_outro,
    is_filler_sentence,
    leading_filler_count,
    starts_with_context_opener,
)
from app.video.audio_features import clip_features, loud_tail

_END_PUNCT = re.compile(r"[.!?…]+[\"'”’)]*$")
TOPIC_BREAK = 0.6  # a topic change at least this strong is not crossed when growing a clip
# A clip is a complete mini story: build-up -> tension -> CLIMAX/PAYOFF -> (short reaction).
LOOKAHEAD_SENTENCES = 3  # how far past the end to look for a payoff that comes later (punchline, reaction)
LOOKAHEAD_SECONDS = 10.0
CONTEXT_LEAD_SECONDS = 5.0  # a clip that opens right on its climax gets up to this much setup before it


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
    score -= 14 * ctx.break_between(a, b)
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


def _strongest_part(ctx: VideoContext, s0: int, s1: int) -> tuple[int, int]:
    parts: list[tuple[int, int]] = []
    a = s0
    for i in range(s0, s1):
        if ctx.topic_breaks[i] >= TOPIC_BREAK:
            parts.append((a, i))
            a = i + 1
    parts.append((a, s1))

    def strength(part: tuple[int, int]) -> float:
        a, b = part
        lo, hi = ctx.sentences[a].start, ctx.sentences[b].end
        feats = window_features(ctx, lo, hi, ctx.words[ctx.sentences[a].w0 : ctx.sentences[b].w1])
        filler = all(is_filler_sentence(ctx.sentences[k], ctx.words) or contains_outro(ctx.sentences[k].text) for k in range(a, b + 1))
        return signal_score(feats) - (40 if filler else 0) + 0.5 * (hi - lo)

    return max(parts, key=strength)


def _payoff_after(ctx: VideoContext, s0: int, s1: int, lexical: bool) -> int | None:
    """A payoff that comes AFTER the span, in the same story: the sentence (within a few sentences/seconds)
    with a clearly stronger reaction in the audio than anything inside the span or, when ``lexical``, a
    punchline/laugh in the transcript while the span itself does not end on one. None if there is none."""
    n = len(ctx.sentences)
    end = ctx.sentences[s1].end
    inside_peak = 0.0
    if ctx.audio is not None:
        inside = clip_features(ctx.audio, ctx.sentences[s0].start, end)
        inside_peak = inside["peak_z"] if inside.get("available") else 0.0
    last = ctx.sentences[s1]
    ends_on_punchline = punchline_hits(ctx.words[last.w0 : last.w1]) > 0
    best, best_gain, prev = None, 0.0, s1
    for k in range(s1 + 1, min(n, s1 + 1 + LOOKAHEAD_SENTENCES)):
        sk = ctx.sentences[k]
        if (
            sk.start - ctx.sentences[prev].end >= 2.0
            or sk.end - end > LOOKAHEAD_SECONDS
            or contains_outro(sk.text)
            or ctx.break_between(prev, k) >= TOPIC_BREAK
        ):
            break
        gain = 0.0
        if ctx.audio is not None:
            f = clip_features(ctx.audio, sk.start, sk.end)
            if f.get("available") and f["peak_z"] > max(1.8, inside_peak + 0.4):
                gain = f["peak_z"] - inside_peak
        if not gain and lexical and not ends_on_punchline and punchline_hits(ctx.words[sk.w0 : sk.w1]) > 0:
            gain = 0.5
        if gain > best_gain:
            best, best_gain = k, gain
        prev = k
    return best


def choose_span(
    ctx: VideoContext, s0: int, s1: int, rules: DurationRules, hook_s: int | None, climax_s: int | None = None
) -> tuple[int, int]:
    """Best contiguous sentence span inside [s0, s1], extending outward when the span is too short.
    ``climax_s``: the sentence with the climax/payoff (from the AI evaluation): the clip always contains it."""
    n = len(ctx.sentences)
    s0, s1 = max(0, min(s0, n - 1)), max(0, min(s1, n - 1))
    if s1 < s0:
        s0, s1 = s1, s0
    if climax_s is not None:
        climax_s = max(0, min(climax_s, n - 1))
        s0, s1 = min(s0, climax_s), max(s1, climax_s)
    keep_end = s0 if climax_s is None else climax_s  # never strip the climax away
    # 0. one story per clip (heuristic mode): a span that contains a clear topic change is cut there,
    #    keeping the part with the strongest signals; the clip then starts on that part's first sentence.
    #    (An LLM-chosen span, hook_s=None, is trusted; it only gets the "mixes_topics" flag.)
    if hook_s is not None and ctx.break_between(s0, s1) >= TOPIC_BREAK:
        s0, s1 = _strongest_part(ctx, s0, s1)
        hook_s = s0
    # 1. strip filler/outro edges
    while s0 < s1 and (is_filler_sentence(ctx.sentences[s0], ctx.words) or contains_outro(ctx.sentences[s0].text)):
        s0 += 1
    while s1 > max(s0, keep_end) and (is_filler_sentence(ctx.sentences[s1], ctx.words) or contains_outro(ctx.sentences[s1].text)):
        s1 -= 1
    # 1b. context repair (heuristic mode): a clip opening on a reference word ("Ze heeft...",
    #     "Hij zei...") gets the sentence that introduces the reference, if it directly precedes it.
    if hook_s is not None and s0 > 0 and starts_with_context_opener(ctx.sentences[s0].text):
        prev = ctx.sentences[s0 - 1]
        if (
            ctx.sentences[s0].start - prev.end < 1.5
            and ctx.break_between(s0 - 1, s0) < TOPIC_BREAK
            and not is_filler_sentence(prev, ctx.words)
            and not contains_outro(prev.text)
            and not starts_with_context_opener(prev.text)
        ):
            s0 -= 1
            hook_s = s0
    # 2. extend if too short: prefer continuing forward (payoff), then include setup before.
    hard_max = rules.max_seconds + rules.tolerance
    while _span_duration(ctx, s0, s1) < rules.min_seconds:
        grew = False
        if s1 + 1 < n:
            gap = ctx.sentences[s1 + 1].start - ctx.sentences[s1].end
            if (
                gap < 2.0
                and _span_duration(ctx, s0, s1 + 1) <= hard_max
                and not contains_outro(ctx.sentences[s1 + 1].text)
                and ctx.break_between(s1, s1 + 1) < TOPIC_BREAK
            ):
                s1 += 1
                grew = True
        if _span_duration(ctx, s0, s1) >= rules.min_seconds:
            break
        if s0 - 1 >= 0:
            prev = ctx.sentences[s0 - 1]
            gap = ctx.sentences[s0].start - prev.end
            if (
                gap < 2.0
                and not is_filler_sentence(prev, ctx.words)
                and _span_duration(ctx, s0 - 1, s1) <= hard_max
                and ctx.break_between(s0 - 1, s0) < TOPIC_BREAK
            ):
                s0 -= 1
                grew = True
        if not grew:
            break
    # 2b. payoff-aware: look ahead - when the climax (the strongest reaction, or in heuristic mode a punchline
    #     in the transcript) comes a few sentences after the span, in the same story, extend to it (dropping
    #     setup before the hook if needed to stay within the limit). Never stop just before the payoff.
    target = _payoff_after(ctx, s0, s1, lexical=hook_s is not None)
    if target is not None:
        a = s0
        while _span_duration(ctx, a, target) > hard_max and a < target and (hook_s is None or a < hook_s):
            a += 1
        if _span_duration(ctx, a, target) <= hard_max:
            s0, s1 = a, target
    # 2b'. context before the climax (heuristic mode): a clip that opens right on its climax (the loudest
    #     moment is in its first sentence) shows a reaction without the reason; start up to a few seconds
    #     earlier on the setup of the same story.
    if hook_s is not None and ctx.audio is not None and s1 > s0:
        first = clip_features(ctx.audio, ctx.sentences[s0].start, ctx.sentences[s0].end)
        rest = clip_features(ctx.audio, ctx.sentences[s0 + 1].start, ctx.sentences[s1].end)
        if first.get("available") and first["peak_z"] >= 1.8 and first["peak_z"] > rest["peak_z"] + 0.4:
            start0 = ctx.sentences[s0].start
            while s0 > 0:
                prev = ctx.sentences[s0 - 1]
                if (
                    start0 - prev.start > CONTEXT_LEAD_SECONDS
                    or ctx.sentences[s0].start - prev.end >= 1.5
                    or ctx.break_between(s0 - 1, s0) >= TOPIC_BREAK
                    or is_filler_sentence(prev, ctx.words)
                    or contains_outro(prev.text)
                    or _span_duration(ctx, s0 - 1, s1) > soft_max_of(rules)
                ):
                    break
                s0 -= 1
                hook_s = s0
    # 2c. escalation: while the same story keeps its energy (the next sentence is about as loud as the
    #     loudest moment so far), keep going: stories peak at the end (the reveal, the punchline).
    soft_max = soft_max_of(rules)
    while ctx.audio is not None and s1 + 1 < n:
        inside = clip_features(ctx.audio, ctx.sentences[s0].start, ctx.sentences[s1].end)
        nxt = ctx.sentences[s1 + 1]
        after = clip_features(ctx.audio, nxt.start, nxt.end)
        if not (
            after.get("available")
            and after["peak_z"] >= max(1.2, inside["peak_z"] - 0.3)
            and nxt.start - ctx.sentences[s1].end < 1.5
            and _span_duration(ctx, s0, s1 + 1) <= soft_max
            and not contains_outro(nxt.text)
            and ctx.break_between(s1, s1 + 1) < TOPIC_BREAK
        ):
            break
        s1 += 1
    # 3. if too long, search the best sub-span (one that keeps the climax: a complete story may use the
    #    full tolerance instead of being cut before its payoff)
    if _span_duration(ctx, s0, s1) > (soft_max if climax_s is None else hard_max):
        key_times = _key_times(ctx, s0, s1)
        best, best_score = ((s0, s1) if climax_s is None else (climax_s, climax_s)), float("-inf")
        for a in range(s0, s1 + 1 if climax_s is None else climax_s + 1):
            for b in range(a if climax_s is None else max(a, climax_s), s1 + 1):
                if _span_duration(ctx, a, b) > hard_max:
                    break
                sc = _subspan_score(ctx, a, b, rules, hook_s, key_times)
                if sc > best_score:
                    best, best_score = (a, b), sc
        s0, s1 = best
    return s0, s1


def soft_max_of(rules: DurationRules) -> float:
    return rules.max_seconds + 1.0


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
    ctx: VideoContext, s0: int, s1: int, rules: DurationRules, hook_s: int | None = None, climax_s: int | None = None
) -> ClipWindow:
    s0, s1 = choose_span(ctx, s0, s1, rules, hook_s, climax_s)
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
    if ctx.break_between(s0, s1) >= 0.8:
        flags.append("mixes_topics")

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
