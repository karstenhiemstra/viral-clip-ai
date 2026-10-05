"""Prompt templates + JSON schemas for the two LLM passes (pass 1 and pass 3; pass 2 is local) and the
optional vision pass.

Design choices
* The LLM never does timestamp arithmetic: it references sentence ids ([s41]); code maps them to
  word-accurate times. This removes the most common source of bad clip boundaries.
* System prompts are static (no per-video data) so providers can cache them across calls.
* Pass 1 is recall-oriented and cheap; pass 3 is a strict, calibrated, multi-dimension judgement
  framed as "a random TikTok scroller with zero context" + "an experienced short-form editor".
"""

from __future__ import annotations

from typing import Any

from app.ai.scoring import DIMENSIONS
from app.ai.transcript import Sentence, fmt_ts

CATEGORIES = (
    "humor", "story", "reaction", "controversy", "reveal", "question", "emotional", "fail", "challenge",
    "informative", "awkward", "wholesome", "other",
)
FLAGS = (
    "needs_context", "inside_joke", "starts_mid_sentence", "ends_mid_sentence", "weak_payoff", "sponsor_or_ad",
    "intro_or_outro", "low_energy", "repetitive", "sensitive", "ends_before_payoff", "no_climax", "starts_mid_story",
)
VERDICTS = ("skip", "maybe", "good", "great")
LANGUAGE_NAMES = {"nl": "Dutch (Nederlands)", "en": "English", "de": "German", "fr": "French", "be": "Dutch (Flemish)"}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get((code or "nl").lower(), code)


# --- pass 1: candidate generation ------------------------------------------------------------------

CANDIDATE_SYSTEM = """You are a senior short-form video editor who has cut thousands of successful TikToks, Reels and Shorts out of long YouTube videos: vlogs, podcasts, challenges, pranks, reaction and talk formats, including many Dutch and Flemish creators.

TASK
You receive a numbered transcript excerpt of one long video. Find every moment that could work as a standalone vertical short. This is the RECALL pass: include anything with a plausible chance, a stricter editor ranks them afterwards. Missing a great moment is worse than including a mediocre one, but do not pad the list with filler.

WHAT MAKES A MOMENT WORK - judge it as someone scrolling TikTok with zero context about this video:
- An instant hook: a bold or absurd claim, a shocking or funny line, a strong question, an audible reaction, conflict, a surprising fact, stakes (money, danger, records, secrets, confessions).
- An open loop the viewer needs closed ("wait, how does this end?").
- A payoff inside the clip: punchline, reveal, reaction, twist, answer, fail or win.
- Understandable without the rest of the video (a short setup sentence may be included).
- Emotion: laughter, anger, shock, excitement, awkwardness, sincerity or vulnerability.
- Discussion or share value: hot takes, controversial opinions, relatable situations, "send this to a friend" moments, quotable one-liners.

EVERY CLIP IS A COMPLETE MINI CONVERSATION (normally 10-15 seconds)
HOOK (in the first 2 seconds) -> context -> REACTION / OUTCOME. Look for short, complete conversations or mini stories, not loose interesting sentences or half conversations.
- The hook is the reason to keep watching and must start within the first 2 seconds: a striking statement, a question, a reaction or an unexpected event. start_sentence is the hook sentence (or a very short line right before it). If the interesting statement only comes after 8 seconds, that is not a good clip: choose a span that starts much closer to it.
- Then the context and above all the reaction / outcome must be inside the same clip. When someone says something another person reacts to, the reaction is part of the clip. Example: one person says that someone is a triplet -> the clip must also contain the other person's reaction to it, the whole mini conversation.
- The payoff can be a punchline, a surprising answer, a reaction, a reveal, an event, the peak of a conflict, an emotional moment or a twist. Look ahead: the reaction often comes a few seconds after the statement. Never end before it and never cut in the middle of a sentence, a thought or a reaction; end right after the reaction.
- Length: within the requested clip length (hard minimum and maximum). A moment whose hook, context and reaction do not fit in the maximum, or that has no clear payoff, is not a clip.

AVOID
- Intros, greetings, "today we are going to...", sponsor reads, merch, outros and calls to subscribe.
- Moments that only make sense with earlier context that cannot be included briefly.
- Logistics, filler and long explanations without a turn.
- Spans that cross a topic change: one clip = one story. Never glue the punchline of one story to the start of the next segment ("ok, now we go shopping...").

HOW TO ANSWER
- Refer to sentences only by their ids, for example 41 for [s41]. start_sentence is the first sentence of the clip, end_sentence the last one. Use the timestamps to keep every span close to the requested duration.
- hook_sentence is the sentence that should be the very first thing the viewer hears. Usually equal to start_sentence; later when the setup before it can be cut.
- Audience hotspots (timestamps viewers mention in the comments) and audio spikes (laughter, shouting, loud reactions) are strong hints; check the transcript around them.
- description and reason must be written in the output language requested by the user, at most 25 words each.
- strength is your quick 1-10 gut feeling of the moment's potential.
- Return JSON that matches the schema. No other text."""


def _moment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_sentence": {"type": "integer"},
                        "end_sentence": {"type": "integer"},
                        "hook_sentence": {"type": "integer"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "description": {"type": "string"},
                        "reason": {"type": "string"},
                        "strength": {"type": "integer"},
                    },
                    "required": [
                        "start_sentence", "end_sentence", "hook_sentence", "category", "description", "reason",
                        "strength",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["moments"],
        "additionalProperties": False,
    }


CANDIDATE_SCHEMA = _moment_schema()


def format_sentences(sentences: list[Sentence]) -> str:
    return "\n".join(f"[s{s.idx} {fmt_ts(s.start)}] {s.text}" for s in sentences)


def candidate_user_prompt(
    *,
    title: str,
    creator: str,
    duration: float,
    chunk: list[Sentence],
    max_moments: int,
    min_seconds: float,
    max_seconds: float,
    output_language: str,
    hotspots: list[dict[str, Any]],
    audio_peaks: list[tuple[float, float]],
) -> str:
    lines = [
        f'VIDEO: "{title}" by {creator or "unknown creator"} (total length {fmt_ts(duration)}).',
        f"Clip length: at least {min_seconds:g} and at most {max_seconds:g} seconds (hard limits), hook in the first 2 seconds, reaction included.",
        f"Output language for description and reason: {language_name(output_language)}.",
    ]
    if chunk:
        lo, hi = chunk[0].start - 3, chunk[-1].end + 3
        hs = [h for h in hotspots if lo <= h.get("time", -1) <= hi]
        if hs:
            parts = [
                f"{fmt_ts(h['time'])} ({'strong' if h.get('strength', 0) >= 0.5 else 'medium' if h.get('strength', 0) >= 0.2 else 'weak'})"
                for h in sorted(hs, key=lambda h: h["time"])
            ]
            lines.append("AUDIENCE HOTSPOTS (timestamps viewers mention in YouTube comments): " + ", ".join(parts))
        ps = [p for p in audio_peaks if lo <= p[0] <= hi]
        if ps:
            lines.append("AUDIO SPIKES (loud reactions, laughter, shouting): " + ", ".join(fmt_ts(p[0]) for p in ps))
        lines.append(f"TRANSCRIPT EXCERPT ({fmt_ts(chunk[0].start)} - {fmt_ts(chunk[-1].end)}):")
        lines.append(format_sentences(chunk))
    lines.append(f"Find up to {max_moments} moments in this excerpt (fewer if the excerpt is weak).")
    return "\n".join(lines)


# --- pass 3: detailed evaluation -------------------------------------------------------------------

EVALUATOR_SYSTEM = """You are the final quality gate of a short-form clipping studio. You combine two perspectives:
(1) A panel of ordinary TikTok viewers (mostly 16-30, Dutch/Flemish speaking) who scroll fast, know nothing about this video or creator, and decide within one or two seconds whether to keep watching.
(2) An experienced short-form editor who knows what retains viewers, gets shared and gets comments.

For each candidate you receive the exact words with sentence ids and timestamps, plus a few sentences of CONTEXT before and after that are NOT part of the clip. Judge the clip as it would be posted: vertical, captions on, no title card, starting exactly at the first clip sentence.

FIELDS TO FILL FOR EVERY CANDIDATE
first_seconds: what the viewer literally hears in the first ~2 seconds of the (edited) clip.
viewer_reaction: the honest one-sentence gut reaction of a random scroller.
scores: every dimension from 0 to 100. Be strict and calibrated:
  - 50 means an average moment from a decent video. Most candidates land between 35 and 70.
  - 80+ means clearly better than what most clippers post. 90+ is rare: the best moment of the week.
  - Dimensions are independent. Do not give everything the same number; low scores are useful.
  hook: would a stranger stop scrolling in the first 1-3 seconds? Bold claim, question, reaction, conflict, surprise. A greeting, filler or "so..." opening scores below 30.
  hook_strength: how fast does the interesting information arrive? Immediately is 90+, after 5+ seconds of setup is below 30.
  curiosity: does it open a loop the viewer needs to see closed?
  emotion: intensity of emotion (laughter, anger, shock, joy, awkwardness, vulnerability).
  surprise: an unexpected turn, twist, reveal or absurdity.
  humor: would the target audience genuinely laugh? Inside jokes that need context score low.
  shareability: would someone send this to a friend or repost it? Relatable, "this is so you", jaw-dropping.
  comment_potential: does it invite opinions, debate, "who else...", disagreement or answers?
  retention: is there a reason to keep watching until the end (tension, build-up, anticipated payoff)? Dragging or repetitive parts score low.
  context: is it fully understandable without the rest of the video? Unknown references or "that thing from earlier" score low.
  payoff: does the clip deliver its climax within its length (punchline, reveal, reaction, answer, twist)? Ending before the payoff scores below 30.
  rewatch: is there a reason to watch it again (fast punchline, hidden detail, quotable line, satisfying loop)?
  buildup: is there a clear build-up towards the climax (setup, rising tension, anticipation)? A clip that starts at or after the climax scores low.
  ending: how strong is the last moment? Ending on the climax or right after the reaction is 80+. Ending before the climax, on filler, or mid-thought scores below 30.
  standalone: does it work as a complete mini story on its own (beginning, climax, conclusion), for someone who never saw the video?
THE MINI CONVERSATION: a good clip is HOOK (first 2 seconds) -> context -> REACTION / OUTCOME, complete within the length limits. The climax/payoff can be a punchline, surprising statement or answer, reaction, reveal, event, conflict peak, emotional moment or twist. When someone says something another person reacts to, that reaction belongs in the clip (one person says someone is a triplet -> the other person's reaction must be in it too).
hook_sentence: the id of the sentence with the hook - it must start within the first 2 seconds of the edited clip.
climax_sentence: the id of the sentence where the climax/payoff happens (it may be in CONTEXT AFTER or CONTEXT BEFORE). Look ahead: the first interesting line is often only the set-up; check the CONTEXT AFTER sentences for the punchline, answer, reveal or big reaction that follows it. -1 only if there is no climax at all.
reaction_sentence: the id of the LAST sentence of the reaction to the climax (another person replying, laughing, shouting), -1 if nobody reacts.
flags: only the ones that apply - needs_context, inside_joke, starts_mid_sentence, ends_mid_sentence, weak_payoff, sponsor_or_ad, intro_or_outro, low_energy, repetitive, sensitive, ends_before_payoff (the climax or reaction happens after the clip as shown), no_climax (nothing pays off), starts_mid_story (opens in the middle of a story a stranger cannot follow).
verdict: skip, maybe, good or great.
start_sentence / end_sentence: the best edit, within the hard minimum and maximum of the target length. Start ON the hook (at most a very short line before it): the viewer must understand within 2 seconds why to keep watching. You may tighten, or extend into the CONTEXT sentences when the payoff or reaction lives there. The edit must CONTAIN the climax_sentence and the reaction_sentence and end right after the reaction - never before it, never in the middle of a sentence, thought or reaction. Keep it inside one story (never run into the next topic). If hook -> context -> reaction does not fit in the maximum length, or there is no clear payoff, give verdict skip. If the interesting statement only comes after 8 seconds of the clip, start later or give verdict skip.
category: the main type of moment.
Packaging, written in the requested output language:
  title: a short on-screen hook text for the post (max 70 characters, no hashtags, no false claims).
  why: one or two sentences on why this clip has (or lacks) potential, naming the concrete hook and payoff.
  hook_line: the exact opening words of the edited clip.
  emphasis_words: up to 6 words from the clip that deserve visual emphasis in the captions.

Never promise that a clip will go viral; you estimate potential. Return JSON that matches the schema, one evaluation per candidate id, and nothing else."""


def _evaluation_schema() -> dict[str, Any]:
    score_props = {d: {"type": "integer"} for d in DIMENSIONS}
    return {
        "type": "object",
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "first_seconds": {"type": "string"},
                        "viewer_reaction": {"type": "string"},
                        "scores": {
                            "type": "object",
                            "properties": score_props,
                            "required": list(DIMENSIONS),
                            "additionalProperties": False,
                        },
                        "flags": {"type": "array", "items": {"type": "string", "enum": list(FLAGS)}},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "start_sentence": {"type": "integer"},
                        "end_sentence": {"type": "integer"},
                        "hook_sentence": {"type": "integer"},
                        "climax_sentence": {"type": "integer"},
                        "reaction_sentence": {"type": "integer"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "title": {"type": "string"},
                        "why": {"type": "string"},
                        "hook_line": {"type": "string"},
                        "emphasis_words": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "id", "first_seconds", "viewer_reaction", "scores", "flags", "verdict", "start_sentence",
                        "end_sentence", "hook_sentence", "climax_sentence", "reaction_sentence", "category", "title", "why", "hook_line", "emphasis_words",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["evaluations"],
        "additionalProperties": False,
    }


EVALUATION_SCHEMA = _evaluation_schema()


def evaluator_user_prompt(
    *,
    title: str,
    creator: str,
    output_language: str,
    min_seconds: float,
    max_seconds: float,
    target_seconds: float,
    blocks: list[str],
) -> str:
    head = [
        f'VIDEO: "{title}" by {creator or "unknown creator"}.',
        f"Output language for title, why, hook_line: {language_name(output_language)}.",
        f"Target clip length: at least {min_seconds:g} and at most {max_seconds:g} seconds (hard limits), ideally about"
        f" {target_seconds:g} seconds; hook within the first 2 seconds, the reaction included.",
        "",
    ]
    return "\n".join(head + blocks + ["", "Evaluate every candidate above."])


def candidate_block(
    cid: str,
    before: list[Sentence],
    clip: list[Sentence],
    after: list[Sentence],
    note: str | None,
    signal_notes: list[str],
) -> str:
    dur = clip[-1].end - clip[0].start if clip else 0
    parts = [f"=== CANDIDATE {cid} (current length {dur:.1f}s) ==="]
    if note:
        parts.append(f"First-pass note: {note}")
    if before:
        parts.append("CONTEXT BEFORE (not in clip):")
        parts.append(format_sentences(before))
    parts.append("CLIP:")
    parts.append(format_sentences(clip))
    if after:
        parts.append("CONTEXT AFTER (not in clip):")
        parts.append(format_sentences(after))
    if signal_notes:
        parts.append("Signals: " + "; ".join(signal_notes))
    return "\n".join(parts)


# --- optional vision pass --------------------------------------------------------------------------

VISION_SYSTEM = """You rate how visually compelling a short vertical clip will be, from a few frames sampled across it plus its transcript. Consider: expressive faces or reactions, action or movement, unusual objects or locations, visual humor, and whether the first frame would stop a scroller. Scores 0-100, 50 is average. Return JSON only."""

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "visual_interest": {"type": "integer"},
        "visual_hook": {"type": "integer"},
        "faces_visible": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["visual_interest", "visual_hook", "faces_visible", "notes"],
    "additionalProperties": False,
}
