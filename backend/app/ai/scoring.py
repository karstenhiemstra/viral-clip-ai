"""Viral Score (0-100): a funnel model instead of a plain average.

A short video only "works" if a viewer (1) STOPS scrolling, (2) HOLDS until the payoff and (3) ENGAGES
(shares, comments, rewatches). Those are sequential probabilities, so they multiply: a clip with a dead
first two seconds does not get rescued by great shareability, because nobody reaches the good part.

    stage_k       = weighted arithmetic mean of the dimensions in stage k   (configurable weights)
    content_score = weighted GEOMETRIC mean of the three stages             (configurable stage weights)
    raw           = (1 - a) * content_score + a * signal_score              (a = signal_blend, LLM mode)
    final         = raw * penalties + crowd_bonus + personal_adjustment, then verdict caps, clamped 0-100

The score is a prediction of relative potential, never a guarantee ("High viral potential").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

DIMENSIONS: tuple[str, ...] = (
    "hook",
    "hook_strength",
    "curiosity",
    "emotion",
    "surprise",
    "humor",
    "shareability",
    "comment_potential",
    "retention",
    "context",
    "payoff",
    "rewatch",
)

STAGES: dict[str, tuple[str, ...]] = {
    "stop": ("hook", "hook_strength", "curiosity"),
    "hold": ("retention", "payoff", "context", "surprise", "emotion"),
    "engage": ("shareability", "comment_potential", "humor", "rewatch"),
}

# Multiplicative penalties for structural problems (flags come from the LLM and/or heuristics).
FLAG_PENALTIES: dict[str, float] = {
    "needs_context": 0.85,
    "inside_joke": 0.9,
    "starts_mid_sentence": 0.93,
    "ends_mid_sentence": 0.93,
    "weak_payoff": 0.92,
    "sponsor_or_ad": 0.55,
    "intro_or_outro": 0.6,
    "low_energy": 0.93,
    "repetitive": 0.93,
    "sensitive": 0.85,
    "mixes_topics": 0.85,  # two unrelated parts of the video glued together
}

VERDICT_CAPS: dict[str, float] = {"skip": 45.0, "maybe": 76.0, "good": 92.0, "great": 100.0}

LABELS_NL: dict[str, str] = {
    "hook": "Hook",
    "hook_strength": "Hook Strength",
    "curiosity": "Curiosity",
    "emotion": "Emotion",
    "surprise": "Surprise",
    "humor": "Humor",
    "shareability": "Shareability",
    "comment_potential": "Comment Potential",
    "retention": "Retention",
    "context": "Context",
    "payoff": "Payoff",
    "rewatch": "Rewatch",
}


@dataclass
class ScoreResult:
    viral_score: float
    stage_scores: dict[str, float]
    content_score: float
    breakdown: dict[str, Any] = field(default_factory=dict)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def stage_scores(dims: dict[str, float], weights: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for stage, members in STAGES.items():
        num = sum(weights.get(d, 1.0) * _clamp(float(dims.get(d, 50.0))) for d in members)
        den = sum(weights.get(d, 1.0) for d in members)
        out[stage] = round(num / den if den > 0 else 50.0, 2)
    return out


def geometric_funnel(stages: dict[str, float], stage_weights: dict[str, float]) -> float:
    total = sum(max(0.0, w) for w in stage_weights.values()) or 1.0
    log_sum = 0.0
    for stage, value in stages.items():
        w = max(0.0, stage_weights.get(stage, 0.0)) / total
        log_sum += w * math.log(max(1.0, value) / 100.0)
    return 100.0 * math.exp(log_sum)


def compute_viral_score(
    dims: dict[str, float],
    *,
    weights: dict[str, float],
    stage_weights: dict[str, float],
    signal: float | None = None,
    signal_blend: float = 0.0,
    flags: list[str] | None = None,
    verdict: str | None = None,
    crowd: float = 0.0,
    personal_adjustment: float = 0.0,
) -> ScoreResult:
    stages = stage_scores(dims, weights)
    content = geometric_funnel(stages, stage_weights)
    raw = content
    if signal is not None and signal_blend > 0:
        raw = (1 - signal_blend) * content + signal_blend * signal
    penalty = 1.0
    applied: list[str] = []
    for fl in flags or []:
        if fl in FLAG_PENALTIES:
            penalty *= FLAG_PENALTIES[fl]
            applied.append(fl)
    penalty = max(0.35, penalty)
    # Audience comments that reference this exact moment are real-world evidence -> bounded bonus.
    crowd_bonus = 6.0 * max(0.0, min(1.0, crowd))
    score = raw * penalty + crowd_bonus + personal_adjustment
    cap = VERDICT_CAPS.get((verdict or "").lower(), 100.0)
    score = _clamp(min(score, cap))
    return ScoreResult(
        viral_score=round(score, 1),
        stage_scores=stages,
        content_score=round(content, 2),
        breakdown={
            "content_score": round(content, 2),
            "signal_score": None if signal is None else round(signal, 2),
            "signal_blend": signal_blend,
            "penalty_multiplier": round(penalty, 3),
            "penalties": applied,
            "crowd_bonus": round(crowd_bonus, 2),
            "personal_adjustment": round(personal_adjustment, 2),
            "verdict": verdict,
            "verdict_cap": cap if cap < 100 else None,
        },
    )


def potential_label(score: float) -> str:
    """Honest wording: potential, never a promise."""
    if score >= 85:
        return "High viral potential"
    if score >= 70:
        return "Good potential"
    if score >= 55:
        return "Moderate potential"
    return "Low potential"
