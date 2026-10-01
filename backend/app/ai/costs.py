"""Rough AI cost estimate per video.

The token counts below were measured by running this pipeline's real prompts over Dutch transcripts
(~150 spoken words per minute):

* Pass 1 (fast model) reads the WHOLE transcript in chunks: ~460 input and ~170 visible output
  tokens per minute of video (10 min: 4.7k in / 1.4k out; 60 min: 27.6k in / 10.2k out).
* Pass 2 is local and free.
* Pass 3 (smart model) only sees the shortlist, so it does not grow with video length: ~350 input and
  ~150 visible output tokens per candidate (30 candidates: ~10.5k in / ~4.5k out).

Not measurable offline: hidden reasoning ("thinking") tokens, which are billed as output. They depend
on the model and effort, so the estimate is a low-high range using the multipliers below. Real usage
per analysis is recorded from the provider's own token counts and shown on the video page.
"""

from __future__ import annotations

from typing import Any

from app.ai.llm import WHISPER_USD_PER_MINUTE, estimate_cost, resolve_models

PASS1_IN_PER_MIN = 460
PASS1_OUT_PER_MIN = 170
PASS3_IN_PER_CANDIDATE = 350
PASS3_OUT_PER_CANDIDATE = 150

# Extra hidden reasoning output as a multiple of the visible output: (low, high). Assumptions.
REASONING_MULTIPLIER: dict[str | None, tuple[float, float]] = {
    None: (0.0, 0.0),  # no extended thinking (e.g. claude-haiku-4-5)
    "minimal": (0.0, 0.3),
    "low": (0.5, 1.5),
    "medium": (1.5, 4.0),
    "high": (3.0, 8.0),
}


def _reasoning(model: str, effort: str | None) -> tuple[float, float]:
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return REASONING_MULTIPLIER.get(effort or "medium", (0.5, 1.5))
    if model.startswith(("claude-opus-5", "claude-sonnet-5", "claude-fable-5")):
        return REASONING_MULTIPLIER.get(effort, (0.0, 0.0))
    return (0.0, 0.0)


def estimate_video_cost(
    minutes: float,
    provider: str,
    quality: str = "balanced",
    *,
    candidates: int = 30,
    transcribe_with_whisper_api: bool = True,
    model_fast: str = "",
    model_smart: str = "",
) -> dict[str, Any]:
    """Return a low/high USD range for one video plus a breakdown per pass."""
    out: dict[str, Any] = {"minutes": minutes, "provider": provider, "quality": quality}
    whisper = round(minutes * WHISPER_USD_PER_MINUTE, 4) if transcribe_with_whisper_api else 0.0
    if provider not in ("openai", "anthropic"):
        out.update(models={}, pass1=[0.0, 0.0], pass3=[0.0, 0.0], transcription=whisper, total=[whisper, whisper])
        return out
    models, efforts = resolve_models(provider, quality, model_fast, model_smart)

    def span(model: str, effort: str | None, tin: float, tout: float) -> list[float]:
        lo, hi = _reasoning(model, effort)
        return [round(estimate_cost(model, int(tin), int(tout * (1 + lo))), 4),
                round(estimate_cost(model, int(tin), int(tout * (1 + hi))), 4)]

    p1 = span(models["fast"], efforts["fast"], minutes * PASS1_IN_PER_MIN, minutes * PASS1_OUT_PER_MIN)
    p3 = span(models["smart"], efforts["smart"], candidates * PASS3_IN_PER_CANDIDATE, candidates * PASS3_OUT_PER_CANDIDATE)
    out.update(
        models={"pass1": models["fast"], "pass3": models["smart"]},
        pass1=p1,
        pass3=p3,
        transcription=whisper,
        total=[round(p1[0] + p3[0] + whisper, 3), round(p1[1] + p3[1] + whisper, 3)],
    )
    return out
