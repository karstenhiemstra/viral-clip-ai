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

from app.ai.signals import (
    VideoContext,
    hook_features,
    punchline_hits,
    reaction_hits,
    signal_score,
    window_features,
)
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
# A clip is a complete mini conversation: HOOK (in the first 2 seconds) -> context -> REACTION / outcome.
HOOK_WINDOW = 2.0  # the hook sentence starts at most this long after the start of the clip
LOOKAHEAD_SENTENCES = 3  # how far past the end to look for a payoff that comes later (punchline, reaction)
LOOKAHEAD_SECONDS = 10.0
CONTEXT_LEAD_SECONDS = HOOK_WINDOW  # setup before a clip that opens right on its climax (keeps the hook early)
REACTION_MAX_WORDS = 8  # a short line right after a statement ("Wat?! Echt?") is the reaction to it
EDGE = 0.4  # room for the lead-in before the first word and the tail after the last word
# Flags that reject a clip: it is not a complete mini conversation within the limits.
REJECT_FLAGS = ("hook_late", "incomplete_story", "too_short")


@dataclass
class DurationRules:
    min_seconds: float = 10.0
    max_seconds: float = 15.0
    target_seconds: float = 13.0
    remove_silences: bool = True
    silence_min_gap: float = 0.6
    tolerance: float = 0.0  # may exceed max by this much (0: the maximum is a hard limit)


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
        score -= 8  # never stop in the middle of a thought
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


def _limits(rules: DurationRules) -> tuple[float, float]:
    """(soft, hard) maximum length of the sentence span, leaving room for the lead-in and the tail so the
    whole clip stays within the maximum."""
    hard = rules.max_seconds + rules.tolerance - EDGE
    return min(rules.max_seconds + 1.0, hard), hard


def _hook_ok(ctx: VideoContext, a: int, hook_s: int | None) -> bool:
    """Starting on sentence ``a`` keeps the hook within the first seconds of the clip."""
    return hook_s is None or (a <= hook_s and ctx.sentences[hook_s].start - ctx.sentences[a].start <= HOOK_WINDOW)


def _joins(ctx: VideoContext, a: int, b: int, max_gap: float = 2.0) -> bool:
    """Sentence b directly continues sentence a (same story, no long pause, no outro)."""
    return (
        ctx.sentences[b].start - ctx.sentences[a].end < max_gap
        and ctx.break_between(min(a, b), max(a, b)) < TOPIC_BREAK
        and not contains_outro(ctx.sentences[b].text)
    )


def _is_reaction(ctx: VideoContext, k: int) -> bool:
    """Sentence k reacts to the one before it: a short reply, a question or exclamation, a laugh or a loud
    moment, said right after it (someone says something -> the other person reacts)."""
    sk = ctx.sentences[k]
    words = ctx.words[sk.w0 : sk.w1]
    if not words or not _joins(ctx, k - 1, k, max_gap=1.5) or is_filler_sentence(sk, ctx.words):
        return False
    if punchline_hits(words) or (len(words) <= REACTION_MAX_WORDS and ("?" in sk.text or "!" in sk.text or reaction_hits(words))):
        return True
    if ctx.audio is not None:
        f = clip_features(ctx.audio, sk.start, sk.end)
        return bool(f.get("available")) and f["peak_z"] >= 1.8
    return False


def _strong_opener(ctx: VideoContext, k: int) -> bool:
    """Sentence k works as a hook on its own: a striking statement, question or exclamation."""
    sk = ctx.sentences[k]
    words = ctx.words[sk.w0 : sk.w1]
    if not words or is_filler_sentence(sk, ctx.words) or contains_outro(sk.text) or starts_with_context_opener(sk.text):
        return False
    f = hook_features(words, sk.start)
    return f["first_strength"] >= 0.6 or "?" in sk.text or "!" in sk.text


def _start_hook(ctx: VideoContext, a: int, hook_s: int, heuristic: bool) -> int | None:
    """The hook when the clip starts on sentence ``a``: the current one if it stays in the first seconds,
    or (heuristic mode) sentence ``a`` itself when it is a hook on its own; None = cannot start there."""
    if _hook_ok(ctx, a, hook_s):
        return hook_s
    return a if heuristic and _strong_opener(ctx, a) else None


def choose_span(
    ctx: VideoContext,
    s0: int,
    s1: int,
    rules: DurationRules,
    hook_s: int | None,
    climax_s: int | None = None,
    *,
    reaction_s: int | None = None,
    heuristic: bool | None = None,
) -> tuple[int, int]:
    """Best contiguous sentence span inside [s0, s1], extending outward when the span is too short.

    ``hook_s``: the sentence with the hook - it starts within the first ``HOOK_WINDOW`` seconds of the clip.
    ``climax_s`` / ``reaction_s`` (from the AI evaluation): the payoff and the reaction to it - the clip
    always contains them when they fit. ``heuristic``: no AI chose this span (default: ``hook_s`` given), so
    the signal-based repairs apply; an AI-chosen span is otherwise trusted."""
    n = len(ctx.sentences)
    heuristic = hook_s is not None if heuristic is None else heuristic
    s0, s1 = max(0, min(s0, n - 1)), max(0, min(s1, n - 1))
    if s1 < s0:
        s0, s1 = s1, s0
    clamp = lambda i: None if i is None else max(0, min(i, n - 1))  # noqa: E731
    hook_s, climax_s, reaction_s = clamp(hook_s), clamp(climax_s), clamp(reaction_s)
    must_end = max((i for i in (climax_s, reaction_s) if i is not None), default=None)
    if must_end is not None:
        s1 = max(s1, must_end)
    if hook_s is not None:
        s0 = min(s0, hook_s)
        s1 = max(s1, hook_s)
    keep_end = max(i for i in (s0, hook_s, must_end) if i is not None)  # never strip the hook/climax/reaction
    soft_max, hard_max = _limits(rules)
    # 0. one story per clip (heuristic mode): a span that contains a clear topic change is cut there,
    #    keeping the part with the strongest signals; the clip then starts on that part's first sentence.
    #    (An AI-chosen span is trusted; it only gets the "mixes_topics" flag.)
    if heuristic and ctx.break_between(s0, s1) >= TOPIC_BREAK:
        s0, s1 = _strongest_part(ctx, s0, s1)
        hook_s = s0
    # 1. strip filler/outro edges
    while s0 < s1 and (is_filler_sentence(ctx.sentences[s0], ctx.words) or contains_outro(ctx.sentences[s0].text)):
        s0 += 1
    while s1 > max(s0, keep_end) and (is_filler_sentence(ctx.sentences[s1], ctx.words) or contains_outro(ctx.sentences[s1].text)):
        s1 -= 1
    # 1a. the hook comes first: drop setup that would push the hook past the first seconds
    hook_s = s0 if hook_s is None or hook_s < s0 else hook_s
    while (h := _start_hook(ctx, s0, hook_s, heuristic)) is None:
        s0 += 1
    hook_s = h
    # 1b. context repair (heuristic mode): a clip opening on a reference word ("Ze heeft...",
    #     "Hij zei...") gets the sentence that introduces the reference, if it directly precedes it and
    #     the hook stays in the first seconds.
    if heuristic and s0 > 0 and starts_with_context_opener(ctx.sentences[s0].text):
        prev = ctx.sentences[s0 - 1]
        h = _start_hook(ctx, s0 - 1, hook_s, heuristic)
        if (
            ctx.sentences[s0].start - prev.end < 1.5
            and h is not None
            and ctx.break_between(s0 - 1, s0) < TOPIC_BREAK
            and not is_filler_sentence(prev, ctx.words)
            and not contains_outro(prev.text)
            and not starts_with_context_opener(prev.text)
        ):
            s0, hook_s = s0 - 1, h
    # 2. extend if too short: continue forward (context, payoff, reaction); setup before the hook only
    #    while the hook stays in the first seconds.
    while _span_duration(ctx, s0, s1) < rules.min_seconds:
        grew = False
        if s1 + 1 < n and _joins(ctx, s1, s1 + 1) and _span_duration(ctx, s0, s1 + 1) <= hard_max:
            s1 += 1
            grew = True
        if _span_duration(ctx, s0, s1) >= rules.min_seconds:
            break
        h = _start_hook(ctx, s0 - 1, hook_s, heuristic) if s0 > 0 else None
        if (
            h is not None
            and _joins(ctx, s0, s0 - 1)
            and not is_filler_sentence(ctx.sentences[s0 - 1], ctx.words)
            and _span_duration(ctx, s0 - 1, s1) <= hard_max
        ):
            s0, hook_s = s0 - 1, h
            grew = True
        if not grew:
            break
    # 2b. payoff-aware: look ahead - when the climax (the strongest reaction, or in heuristic mode a punchline
    #     in the transcript) comes a few sentences after the span, in the same story, extend to it (dropping
    #     setup before the hook if needed to stay within the limit). Never stop just before the payoff.
    target = _payoff_after(ctx, s0, s1, lexical=heuristic)
    if target is not None:
        a = s0
        while _span_duration(ctx, a, target) > hard_max and a < hook_s:
            a += 1
        if _span_duration(ctx, a, target) <= hard_max:
            s0, s1 = a, target
        elif heuristic:  # start later, on a line that is a hook itself, so the payoff fits
            later = next((k for k in range(hook_s + 1, target) if _strong_opener(ctx, k)
                          and _span_duration(ctx, k, target) <= hard_max), None)
            if later is not None:
                s0, s1, hook_s = later, target, later
    # 2b'. context before the climax (heuristic mode): a clip that opens right on its climax (the loudest
    #     moment is in its first sentence) gets a short setup before it - only as much as keeps that opening
    #     line within the first seconds.
    if heuristic and ctx.audio is not None and s1 > s0:
        first = clip_features(ctx.audio, ctx.sentences[s0].start, ctx.sentences[s0].end)
        rest = clip_features(ctx.audio, ctx.sentences[s0 + 1].start, ctx.sentences[s1].end)
        if first.get("available") and first["peak_z"] >= 1.8 and first["peak_z"] > rest["peak_z"] + 0.4:
            opening = s0
            while (
                s0 > 0
                and ctx.sentences[opening].start - ctx.sentences[s0 - 1].start <= CONTEXT_LEAD_SECONDS
                and _joins(ctx, s0, s0 - 1, max_gap=1.5)
                and not is_filler_sentence(ctx.sentences[s0 - 1], ctx.words)
                and _span_duration(ctx, s0 - 1, s1) <= soft_max
            ):
                s0 -= 1
    # 2c. escalation: while the same story keeps its energy (the next sentence is about as loud as the
    #     loudest moment so far), keep going: stories peak at the end (the reveal, the punchline).
    while ctx.audio is not None and s1 + 1 < n:
        inside = clip_features(ctx.audio, ctx.sentences[s0].start, ctx.sentences[s1].end)
        nxt = ctx.sentences[s1 + 1]
        after = clip_features(ctx.audio, nxt.start, nxt.end)
        if not (
            after.get("available")
            and after["peak_z"] >= max(1.2, inside["peak_z"] - 0.3)
            and _joins(ctx, s1, s1 + 1, max_gap=1.5)
            and _span_duration(ctx, s0, s1 + 1) <= soft_max
        ):
            break
        s1 += 1
    # 2d. the reaction: when someone reacts right after the last line ("Wat?! Een drieling?"), that reply
    #     belongs to the mini conversation - include it (up to two short replies) if it fits.
    for _ in range(2):
        if s1 + 1 < n and _is_reaction(ctx, s1 + 1) and _span_duration(ctx, s0, s1 + 1) <= hard_max:
            s1 += 1
        else:
            break
    # 3. if too long, search the best sub-span: it starts within the hook window and keeps the climax and
    #    the reaction.
    if _span_duration(ctx, s0, s1) > (soft_max if must_end is None else hard_max):
        key_times = _key_times(ctx, s0, s1)
        best, best_score = None, float("-inf")
        for a in range(s0, s1 + 1):
            if a > hook_s or (must_end is not None and a > must_end):
                break
            if not _hook_ok(ctx, a, hook_s):
                continue
            for b in range(max(a, must_end if must_end is not None else a), s1 + 1):
                if _span_duration(ctx, a, b) > hard_max:
                    break
                sc = _subspan_score(ctx, a, b, rules, hook_s, key_times)
                if sc > best_score:
                    best, best_score = (a, b), sc
        if best is None:  # hook -> payoff does not fit: keep the hook; the clip is flagged incomplete
            best = (hook_s, hook_s)
            while best[1] + 1 <= s1 and _span_duration(ctx, best[0], best[1] + 1) <= hard_max:
                best = (best[0], best[1] + 1)
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
        if dur > rules.max_seconds + rules.tolerance - EDGE:
            break
        w = ctx.words[i - 1]
        gap = (ctx.words[i].start - w.end) if i < len(ctx.words) else 1.0
        cost = abs(dur - rules.target_seconds) - (3 if _END_PUNCT.search(w.text) else 0) - (1.5 if w.text.endswith(",") else 0) - min(2.0, gap * 3)
        if cost < best_cost:
            best_i, best_cost = i, cost
    return best_i


def optimize_boundaries(
    ctx: VideoContext,
    s0: int,
    s1: int,
    rules: DurationRules,
    hook_s: int | None = None,
    climax_s: int | None = None,
    *,
    reaction_s: int | None = None,
    heuristic: bool | None = None,
) -> ClipWindow:
    """The final clip window. Its flags include ``REJECT_FLAGS`` when it cannot be a complete mini
    conversation within the limits: the hook later than the first seconds, the climax or reaction left
    out, or shorter than the minimum."""
    s0, s1 = choose_span(ctx, s0, s1, rules, hook_s, climax_s, reaction_s=reaction_s, heuristic=heuristic)
    sa: Sentence = ctx.sentences[s0]
    sb: Sentence = ctx.sentences[s1]
    w0, w1 = sa.w0, sb.w1
    flags: list[str] = []
    max_len = rules.max_seconds + rules.tolerance

    lead = leading_filler_count(ctx.words[w0:w1])
    if lead and (w1 - w0 - lead) >= 3:
        w0 += lead
    if ctx.words[w1 - 1].end - ctx.words[w0].start > max_len - EDGE:
        w1 = _cut_long_sentence(ctx, w0, w1, rules)

    first, last = ctx.words[w0], ctx.words[w1 - 1]
    prev_end = ctx.words[w0 - 1].end if w0 > 0 else 0.0
    next_start = ctx.words[w1].start if w1 < len(ctx.words) else ctx.duration
    start = max(prev_end + 0.02, first.start - 0.12, 0.0)
    tail = 0.35 + loud_tail(ctx.audio, last.end, max_extend=1.2)
    end = min(last.end + tail, next_start - 0.03, ctx.duration or last.end + tail, start + max_len)
    end = max(end, last.end + 0.08)

    if w0 > 0 and not _END_PUNCT.search(ctx.words[w0 - 1].text) and first.start - prev_end < 0.35 and lead == 0:
        flags.append("starts_mid_sentence")
    if not _END_PUNCT.search(last.text) and next_start - last.end < 0.3:
        flags.append("ends_mid_sentence")
    if ctx.break_between(s0, s1) >= 0.8:
        flags.append("mixes_topics")
    # a complete mini conversation: the hook in the first seconds, the climax and the reaction inside
    hook = s0 if hook_s is None else max(0, min(hook_s, len(ctx.sentences) - 1))
    if not s0 <= hook <= s1 or ctx.sentences[hook].start - start > HOOK_WINDOW + 0.25:
        flags.append("hook_late")
    if any(i is not None and i > s1 for i in (climax_s, reaction_s)) or _payoff_after(
        ctx, s0, s1, lexical=hook_s is not None if heuristic is None else heuristic
    ) is not None:
        flags.append("incomplete_story")  # the payoff or the reaction comes after the clip

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
        if sum(b - a for a, b in segments) < rules.min_seconds <= end - start:
            segments = [(round(start, 3), round(end, 3))]  # keep the pauses rather than fall below the minimum
    if sum(b - a for a, b in segments) < rules.min_seconds - 0.05:
        flags.append("too_short")

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
