"""Pass 3 (stages 2-7) - detailed evaluation of ONLY the shortlisted candidates.

LLM mode: candidates are judged in small batches (default 6) by the "smart" model with the rubric in
``prompts.EVALUATOR_SYSTEM``: viewer simulation, 12 calibrated dimension scores (hook, context,
retention, emotion, share/comment potential, ...), flags, verdict and an improved edit (sentence ids).
Batching lets the model compare candidates, which improves calibration, and shares the cached rubric.

Heuristic mode: the same output shape is produced from deterministic signals, so the rest of the
pipeline does not care which mode ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.ai.candidates import Candidate
from app.ai.llm import LLMClient, LLMError, LLMFatalError, UsageMeter
from app.ai.prompts import (
    CATEGORIES,
    EVALUATION_SCHEMA,
    EVALUATOR_SYSTEM,
    FLAGS,
    VERDICTS,
    candidate_block,
    evaluator_user_prompt,
)
from app.ai.scoring import DIMENSIONS
from app.ai.signals import LEXICON, VideoContext, heuristic_dimension_scores, window_features
from app.ai.transcript import fmt_ts, normalize

log = logging.getLogger(__name__)


def _signal_notes(ctx: VideoContext, c: Candidate) -> list[str]:
    a, b = c.span(ctx)
    f = window_features(ctx, a, b, ctx.words[ctx.sentences[c.s0].w0 : ctx.sentences[c.s1].w1])
    notes = []
    if f.get("audio_available") and f.get("audio_peak_z", 0) >= 1.8:
        t = a + f.get("audio_peak_pos", 0) * (b - a)
        notes.append(f"loud reaction/laughter around {fmt_ts(t)}")
    if f.get("audio_available") and f.get("audio_silence_ratio", 0) > 0.25:
        notes.append("contains long silences")
    if f.get("crowd", 0) >= 0.2:
        notes.append("viewers reference this moment in the YouTube comments")
    return notes


def evaluate_llm(
    llm: LLMClient,
    ctx: VideoContext,
    candidates: list[Candidate],
    *,
    min_s: float,
    max_s: float,
    target_s: float,
    output_language: str,
    batch_size: int,
    meter: UsageMeter,
    progress: Callable[[float], None] | None = None,
    warnings: list[str] | None = None,
) -> int:
    """Fills ``candidate.evaluation``. Returns how many candidates got an LLM evaluation."""
    n = len(ctx.sentences)
    done = 0
    batches = [candidates[i : i + batch_size] for i in range(0, len(candidates), batch_size)]
    for bi, batch in enumerate(batches):
        blocks = []
        for c in batch:
            before = ctx.sentences[max(0, c.s0 - 3) : c.s0]
            after = ctx.sentences[c.s1 + 1 : min(n, c.s1 + 4)]
            note = c.reason or c.description or None
            blocks.append(candidate_block(c.cid, before, ctx.sentences[c.s0 : c.s1 + 1], after, note, _signal_notes(ctx, c)))
        prompt = evaluator_user_prompt(
            title=ctx.title, creator=ctx.creator, output_language=output_language, min_seconds=min_s,
            max_seconds=max_s, target_seconds=target_s, blocks=blocks,
        )
        try:
            res = llm.complete_json(
                system=EVALUATOR_SYSTEM, user=prompt, schema=EVALUATION_SCHEMA, schema_name="evaluations",
                tier="smart", max_tokens=1200 * len(batch) + 1000,
            )
        except LLMFatalError:
            raise
        except LLMError as e:
            log.warning("Evaluation batch %s failed: %s", bi, e)
            if warnings is not None:
                warnings.append(f"Pass 3 batch {bi + 1}: {e}")
            continue
        meter.add(res.usage, "evaluation")
        by_id = {str(ev.get("id", "")).strip(): ev for ev in res.data.get("evaluations", []) or []}
        for c in batch:
            ev = by_id.get(c.cid)
            if ev is None:
                continue
            clean = sanitize_evaluation(ev, c, n)
            if clean:
                c.evaluation = clean
                done += 1
        if progress:
            progress((bi + 1) / len(batches))
    return done


def sanitize_evaluation(ev: dict[str, Any], c: Candidate, n_sentences: int) -> dict[str, Any] | None:
    scores_in = ev.get("scores") or {}
    scores: dict[str, float] = {}
    for d in DIMENSIONS:
        try:
            scores[d] = float(max(0, min(100, int(round(float(scores_in.get(d, 50)))))))
        except (TypeError, ValueError):
            scores[d] = 50.0
    try:
        s0 = int(ev.get("start_sentence", c.s0))
        s1 = int(ev.get("end_sentence", c.s1))
    except (TypeError, ValueError):
        s0, s1 = c.s0, c.s1
    # The edit may move at most 3 sentences outside the shown span (that is all the context it saw).
    s0 = max(0, c.s0 - 3, min(s0, c.s1))
    s1 = min(n_sentences - 1, c.s1 + 3, max(s1, s0))
    verdict = str(ev.get("verdict", "maybe")).lower()
    category = str(ev.get("category") or c.category or "other").lower()
    return {
        "scores": scores,
        "flags": [f for f in (ev.get("flags") or []) if f in FLAGS],
        "verdict": verdict if verdict in VERDICTS else "maybe",
        "s0": s0,
        "s1": s1,
        "category": category if category in CATEGORIES else "other",
        "title": str(ev.get("title", ""))[:120].strip(),
        "why": str(ev.get("why", ""))[:600].strip(),
        "hook_line": str(ev.get("hook_line", ""))[:300].strip(),
        "first_seconds": str(ev.get("first_seconds", ""))[:300].strip(),
        "viewer_reaction": str(ev.get("viewer_reaction", ""))[:300].strip(),
        "emphasis_words": [str(w)[:40] for w in (ev.get("emphasis_words") or [])][:6],
        "mode": "llm",
    }


# --- heuristic mode ---------------------------------------------------------------------------------

_CATEGORY_BY_LEX = (
    ("lex_humor", "humor"),
    ("lex_controversy", "controversy"),
    ("lex_surprise", "reaction"),
    ("lex_story", "story"),
    ("lex_question", "question"),
    ("lex_intensity", "emotional"),
)

_WHY_NL = {
    "humor": "Grappig moment met hoorbare reactie",
    "controversy": "Uitgesproken mening die discussie kan uitlokken",
    "reaction": "Verrassende reactie die direct aandacht trekt",
    "story": "Verhaal met een duidelijke wending",
    "question": "Opent met een vraag die nieuwsgierig maakt",
    "emotional": "Emotioneel en intens moment",
    "other": "Opvallend moment volgens audio- en tekstsignalen",
}


def heuristic_packaging(ctx: VideoContext, words: list, feats: dict[str, float], category_hint: str | None = None) -> dict[str, Any]:
    """Title / hook line / explanation / category for a window, derived from its own words."""
    category = category_hint or next((cat for key, cat in _CATEGORY_BY_LEX if feats.get(key, 0) >= 0.8), "other")
    first = ""
    for w in words:
        first = (first + " " + w.text).strip()
        if w.text.endswith((".", "!", "?", "…")) and len(first) > 12:
            break
    why = _WHY_NL.get(category, _WHY_NL["other"])
    if feats.get("crowd", 0) >= 0.2:
        why += "; kijkers noemen dit moment in de comments"
    if feats.get("audio_available") and feats.get("audio_peak_z", 0) >= 2.5:
        why += "; duidelijke luide reactie in de audio"
    return {
        "category": category,
        "title": first[:70],
        "hook_line": first[:200],
        "first_seconds": first[:200],
        "why": why + ".",
        "emphasis_words": [w.text.strip(".,!?") for w in words if _is_emphasis(w.norm)][:6],
    }


def heuristic_evaluation(ctx: VideoContext, c: Candidate) -> dict[str, Any]:
    a, b = c.span(ctx)
    words = ctx.words[ctx.sentences[c.s0].w0 : ctx.sentences[c.s1].w1]
    f = window_features(ctx, a, b, words)
    pack = heuristic_packaging(ctx, words, f, c.category)
    return {
        "scores": heuristic_dimension_scores(f),
        "flags": heuristic_flags(f),
        "verdict": None,
        "s0": c.s0,
        "s1": c.s1,
        "viewer_reaction": "",
        "mode": "heuristic",
        **pack,
        "why": c.reason or pack["why"],
    }


def heuristic_flags(f: dict[str, float]) -> list[str]:
    flags = []
    if f.get("hook_context_opener"):
        flags.append("needs_context")
    if f.get("outro") or f.get("intro"):
        flags.append("intro_or_outro")
    if f.get("payoff_after_end"):
        flags.append("weak_payoff")
    if f.get("audio_available") and f.get("audio_silence_ratio", 0) > 0.3:
        flags.append("low_energy")
    return flags


def _is_emphasis(tok: str) -> bool:
    return any(tok in LEXICON[k] for k in ("intensity", "surprise", "stakes", "controversy")) or any(
        ch.isdigit() for ch in tok
    )


def evaluate_heuristic(ctx: VideoContext, candidates: list[Candidate]) -> None:
    for c in candidates:
        if c.evaluation is None:
            c.evaluation = heuristic_evaluation(ctx, c)


def emphasis_from_text(text: str, emphasis: list[str]) -> list[str]:
    """Keep only emphasis words that actually occur in the clip text (normalised)."""
    present = {normalize(t) for t in text.split()}
    return [w for w in emphasis if normalize(w) in present]
