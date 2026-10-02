"""Provider-neutral LLM interface.

Every call goes through ``complete_json`` with a JSON schema, a tier ("fast" for cheap recall passes,
"smart" for the careful ranking pass) and returns parsed JSON plus token usage and estimated cost.
Providers live in ``app.ai.providers``; ``get_llm`` picks one from settings/API keys, or returns None
for the fully local heuristic mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.config import get_settings, openai_base_url
from app.services.settings_store import RuntimeSettings, get_secret

# USD per 1M tokens: (input, output, cached input). Prefix match; unknown models are costed at 0.
# Source: public list prices as mirrored in the LiteLLM price table (checked September 2026). Used for
# the cost dashboard and the estimator only; your invoice from the provider is always leading.
PRICING: dict[str, tuple[float, float, float]] = {
    # Anthropic
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.40),  # not in the LiteLLM table yet: check anthropic.com/pricing
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),  # assumed equal to claude-sonnet-5
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    # OpenAI
    "gpt-5-nano": (0.05, 0.40, 0.005),
    "gpt-5-mini": (0.25, 2.00, 0.025),
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-5.1": (1.25, 10.0, 0.125),
    "gpt-5.2": (1.75, 14.0, 0.175),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4": (2.50, 15.0, 0.25),
    "gpt-5.5": (5.0, 30.0, 0.50),
    "gpt-4.1-nano": (0.10, 0.40, 0.025),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "gpt-4.1": (2.00, 8.00, 0.50),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4o": (2.50, 10.0, 1.25),
}
WHISPER_USD_PER_MINUTE = 0.006  # OpenAI whisper-1

# Quality presets: (model, reasoning effort) per tier. "fast" = pass 1 (read the whole transcript
# cheaply), "smart" = pass 3 (judge only the shortlisted candidates). An explicit model in the
# settings/.env always wins over the preset model.
QUALITY_PRESETS: dict[str, dict[str, dict[str, tuple[str, str | None]]]] = {
    "openai": {
        "budget": {"fast": ("gpt-5-mini", "low"), "smart": ("gpt-5-mini", "low")},
        "balanced": {"fast": ("gpt-5-mini", "low"), "smart": ("gpt-5", "low")},
        "best": {"fast": ("gpt-5-mini", "low"), "smart": ("gpt-5", "medium")},
    },
    "anthropic": {
        "budget": {"fast": ("claude-haiku-4-5", None), "smart": ("claude-haiku-4-5", None)},
        "balanced": {"fast": ("claude-haiku-4-5", None), "smart": ("claude-sonnet-5-5", "low")},
        "best": {"fast": ("claude-haiku-4-5", None), "smart": ("claude-opus-5-5", "medium")},
    },
}


def resolve_models(provider: str, quality: str, fast: str = "", smart: str = "", vision: str = "") -> tuple[dict[str, str], dict[str, str | None]]:
    """Models and reasoning efforts per tier for a provider + quality preset (+ explicit overrides)."""
    preset = QUALITY_PRESETS[provider].get(quality) or QUALITY_PRESETS[provider]["balanced"]
    models = {"fast": fast or preset["fast"][0], "smart": smart or preset["smart"][0]}
    models["vision"] = vision or models["smart"]
    efforts = {"fast": preset["fast"][1], "smart": preset["smart"][1], "vision": "low" if preset["smart"][1] else None}
    return models, efforts


def price_for(model: str) -> tuple[float, float, float] | None:
    best = None
    for prefix, price in PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """``input_tokens`` excludes ``cached_input_tokens`` (those are billed at the cached rate)."""
    price = price_for(model)
    if not price:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1] + cached_input_tokens * price[2]) / 1_000_000


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


def connection_error_message(service: str, error: BaseException, base_url: object) -> str:
    """'Connection error.' from the SDK hides the real reason; dig out the underlying network error."""
    root: BaseException = error
    while (root.__cause__ or root.__context__) is not None and len(str(root.__cause__ or root.__context__)) > 0:
        root = root.__cause__ or root.__context__  # type: ignore[assignment]
    detail = f"{type(root).__name__}: {root}" if root is not error else str(error)
    return (
        f"{service} API niet bereikbaar via {base_url} ({detail}). Controleer je internetverbinding, VPN/firewall "
        "en of OPENAI_BASE_URL in .env leeg of correct is."
    )


class LLMFatalError(LLMError):
    """Retrying other chunks is pointless (invalid key, no credit, API unreachable): stop the AI passes."""


@dataclass
class LLMUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class LLMResult:
    data: dict[str, Any]
    usage: LLMUsage


@dataclass
class ImageInput:
    media_type: str  # image/jpeg
    b64: str


class LLMClient(Protocol):
    provider: str
    models: dict[str, str]

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        tier: str = "smart",
        max_tokens: int = 8000,
        images: list[ImageInput] | None = None,
    ) -> LLMResult: ...

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


def parse_json_loose(text: str) -> dict[str, Any]:
    """Parse JSON even when wrapped in code fences or surrounded by stray text."""
    text = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Model gaf geen JSON terug: {text[:200]}") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"Ongeldige JSON van model: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("JSON-antwoord is geen object")
    return data


@dataclass
class UsageMeter:
    """Accumulates usage over an analysis run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, usage: LLMUsage, stage: str) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cost_usd += usage.cost_usd
        self.calls.append(
            {"stage": stage, "model": usage.model, "in": usage.input_tokens, "out": usage.output_tokens,
             "cost": round(usage.cost_usd, 5)}
        )


def get_llm(db: Session | None, rs: RuntimeSettings) -> LLMClient | None:
    choice = rs.ai.llm_provider
    if choice == "heuristic":
        return None
    openai_key = get_secret(db, "openai_api_key")
    anthropic_key = get_secret(db, "anthropic_api_key")
    ai = rs.ai
    if choice == "openai" or (choice == "auto" and openai_key):
        if not openai_key:
            raise LLMError("LLM provider 'openai' gekozen maar OPENAI_API_KEY ontbreekt")
        from app.ai.providers.openai_provider import OpenAIProvider

        models, efforts = resolve_models("openai", ai.quality, ai.model_fast, ai.model_smart, ai.vision_model)
        return OpenAIProvider(
            api_key=openai_key,
            base_url=openai_base_url(),
            models=models,
            efforts=efforts,
            embedding_model=get_settings().embedding_model,
        )
    if choice == "anthropic" or (choice == "auto" and anthropic_key):
        if not anthropic_key:
            raise LLMError("LLM provider 'anthropic' gekozen maar ANTHROPIC_API_KEY ontbreekt")
        from app.ai.providers.anthropic_provider import AnthropicProvider

        models, efforts = resolve_models("anthropic", ai.quality, ai.model_fast, ai.model_smart, ai.vision_model)
        return AnthropicProvider(api_key=anthropic_key, models=models, efforts=efforts)
    return None
