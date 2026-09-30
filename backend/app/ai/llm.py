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

from app.config import get_settings
from app.services.settings_store import RuntimeSettings, get_secret

# USD per 1M tokens (input, output). Prefix match; unknown models are costed at 0 and flagged.
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # OpenAI (update if your pricing differs; used for the cost dashboard only)
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.0),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
}


def price_for(model: str) -> tuple[float, float] | None:
    best = None
    for prefix, price in PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = price_for(model)
    if not price:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


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
    if choice == "openai" or (choice == "auto" and openai_key):
        if not openai_key:
            raise LLMError("LLM provider 'openai' gekozen maar OPENAI_API_KEY ontbreekt")
        from app.ai.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=openai_key,
            base_url=get_settings().openai_base_url or None,
            fast=rs.ai.model_fast,
            smart=rs.ai.model_smart,
            vision=rs.ai.vision_model,
            embedding_model=get_settings().embedding_model,
        )
    if choice == "anthropic" or (choice == "auto" and anthropic_key):
        if not anthropic_key:
            raise LLMError("LLM provider 'anthropic' gekozen maar ANTHROPIC_API_KEY ontbreekt")
        from app.ai.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=anthropic_key, fast=rs.ai.model_fast, smart=rs.ai.model_smart, vision=rs.ai.vision_model
        )
    return None
