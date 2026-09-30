"""Claude (Anthropic) provider.

* Structured outputs via ``output_config.format`` (guaranteed-valid JSON).
* The static system prompt is marked for prompt caching (repeated rubric = ~90% cheaper input).
* Newer models: no sampling params; depth is controlled with ``output_config.effort``.
* On models that support it, server-side refusal fallbacks (``fallbacks="default"``) are enabled so a
  rare safety decline on e.g. a controversial transcript is retried on a fallback model automatically.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from app.ai.llm import ImageInput, LLMError, LLMRefusal, LLMResult, LLMUsage, estimate_cost, parse_json_loose

log = logging.getLogger(__name__)

DEFAULT_FAST = "claude-haiku-4-5"
DEFAULT_SMART = "claude-opus-5-5"

# Models that accept output_config.effort (Haiku 4.5 does not).
_EFFORT_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7")
# Models that accept server-side fallbacks="default" (beta).
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    provider = "anthropic"

    def __init__(self, api_key: str, fast: str = "", smart: str = "", vision: str = ""):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=300.0)
        self.models = {
            "fast": fast or DEFAULT_FAST,
            "smart": smart or DEFAULT_SMART,
            "vision": vision or smart or DEFAULT_SMART,
        }
        self._disabled: set[str] = set()  # features the API rejected for this key/model

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
    ) -> LLMResult:
        model = self.models.get(tier, self.models["smart"])
        content: list[dict[str, Any]] = []
        for img in images or []:
            content.append({"type": "image", "source": {"type": "base64", "media_type": img.media_type, "data": img.b64}})
        content.append({"type": "text", "text": user})

        # Thinking tokens count against max_tokens on the newer models; leave generous room.
        budget = max(max_tokens, 16000) if model.startswith(_EFFORT_PREFIXES) else max_tokens
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": budget,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }
        output_config: dict[str, Any] = {}
        if "format" not in self._disabled:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if model.startswith(_EFFORT_PREFIXES) and "effort" not in self._disabled:
            output_config["effort"] = "low" if tier == "fast" else "medium"
        if output_config:
            kwargs["output_config"] = output_config
        if "format" in self._disabled:
            kwargs["system"][0]["text"] = system + "\n\nRespond with a single JSON object only."

        use_fallbacks = model.startswith(_FALLBACK_MODELS) and "fallbacks" not in self._disabled
        try:
            if use_fallbacks:
                resp = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            for feature, needle in (("fallbacks", "fallback"), ("format", "output_config.format"), ("effort", "effort")):
                if feature not in self._disabled and needle in msg:
                    log.warning("Anthropic rejected %s for %s; retrying without it", feature, model)
                    self._disabled.add(feature)
                    return self.complete_json(
                        system=system, user=user, schema=schema, schema_name=schema_name, tier=tier,
                        max_tokens=max_tokens, images=images,
                    )
            if "format" not in self._disabled and ("json_schema" in msg or "schema" in msg):
                self._disabled.add("format")
                return self.complete_json(
                    system=system, user=user, schema=schema, schema_name=schema_name, tier=tier,
                    max_tokens=max_tokens, images=images,
                )
            raise LLMError(f"Anthropic request geweigerd: {e}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Ongeldige ANTHROPIC_API_KEY") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API fout {e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Anthropic API niet bereikbaar: {e}") from e

        usage = resp.usage
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        served_model = getattr(resp, "model", model) or model
        # Cache reads bill at ~0.1x, cache writes at ~1.25x of the input price.
        cost = estimate_cost(served_model, in_tok, out_tok) + estimate_cost(served_model, int(cache_read * 0.1 + cache_write * 1.25), 0)
        llm_usage = LLMUsage(model=served_model, input_tokens=in_tok + cache_read + cache_write, output_tokens=out_tok, cost_usd=cost)

        if resp.stop_reason == "refusal":
            raise LLMRefusal("Het model weigerde deze aanvraag (safety classifier)")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if resp.stop_reason == "max_tokens" and not text.rstrip().endswith("}"):
            raise LLMError("Antwoord afgekapt (max_tokens bereikt); verklein de batchgrootte")
        return LLMResult(data=parse_json_loose(text), usage=llm_usage)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None  # Anthropic has no embeddings endpoint; dedupe falls back to TF-IDF.
