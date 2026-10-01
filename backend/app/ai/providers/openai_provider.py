"""OpenAI provider (also works with OpenAI-compatible endpoints via OPENAI_BASE_URL, e.g. a local
Ollama/vLLM server or OpenRouter, which is a good way to cut costs further)."""

from __future__ import annotations

import logging
from typing import Any

import openai

from app.ai.llm import (
    ImageInput,
    LLMError,
    LLMFatalError,
    LLMResult,
    LLMUsage,
    connection_error_message,
    estimate_cost,
    parse_json_loose,
    resolve_models,
)
from app.config import openai_base_url

log = logging.getLogger(__name__)

DEFAULT_FAST = "gpt-5-mini"
DEFAULT_SMART = "gpt-5"
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
# If a key has no access to a model (new account tier, retired model), try these instead of failing.
MODEL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "gpt-5": ("gpt-5-mini", "gpt-4.1"),
    "gpt-5-mini": ("gpt-4.1-mini", "gpt-4o-mini"),
}

QUOTA_MESSAGE = (
    "Je OpenAI-tegoed is op of je hebt nog geen betaalmethode. Ga naar platform.openai.com → Settings → "
    "Billing en voeg tegoed toe (bijv. $5). Tot die tijd gebruikt de app de gratis heuristische analyse."
)


class OpenAIProvider:
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        fast: str = "",
        smart: str = "",
        vision: str = "",
        embedding_model: str = "text-embedding-3-small",
        models: dict[str, str] | None = None,
        efforts: dict[str, str | None] | None = None,
    ):
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url or openai_base_url(), max_retries=3, timeout=300.0)
        if models is None:
            models, preset_efforts = resolve_models("openai", "balanced", fast, smart, vision)
            efforts = efforts or preset_efforts
        self.models = dict(models)
        self.efforts = dict(efforts or {})
        self.embedding_model = embedding_model
        # Degrade gracefully on compatible servers: json_schema -> json_object -> plain prompt.
        self._format_level = 0
        self._no_reasoning_effort = False
        self._tried_fallbacks: set[str] = set()

    def _response_format(self, schema: dict[str, Any], name: str) -> dict[str, Any] | None:
        if self._format_level == 0:
            return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}
        if self._format_level == 1:
            return {"type": "json_object"}
        return None

    def _switch_model(self, tier: str, model: str, error: Exception) -> bool:
        """Replace an unavailable model by the next fallback (for every tier that used it)."""
        for alt in MODEL_FALLBACKS.get(model, ()):
            if alt not in self._tried_fallbacks:
                self._tried_fallbacks.add(alt)
                log.warning("OpenAI model %s niet beschikbaar voor deze key (%s); overgeschakeld naar %s", model, error, alt)
                for t, m in list(self.models.items()):
                    if m == model:
                        self.models[t] = alt
                return True
        return False

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
        reasoning = model.startswith(_REASONING_PREFIXES)
        if images:
            user_content: Any = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:{i.media_type};base64,{i.b64}", "detail": "low"}}
                for i in images
            ]
        else:
            user_content = user
        sys_text = system if self._format_level == 0 else system + "\n\nRespond with a single JSON object only."
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": user_content}],
            # Hidden reasoning tokens count against this limit, so reasoning models get extra room.
            "max_completion_tokens": max(max_tokens, 12000) if reasoning else max_tokens,
        }
        rf = self._response_format(schema, schema_name)
        if rf:
            kwargs["response_format"] = rf
        effort = self.efforts.get(tier) or ("low" if tier == "fast" else "medium")
        if reasoning and not self._no_reasoning_effort:
            kwargs["reasoning_effort"] = effort

        def again() -> LLMResult:
            return self.complete_json(system=system, user=user, schema=schema, schema_name=schema_name,
                                      tier=tier, max_tokens=max_tokens, images=images)

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            msg = str(e).lower()
            if "reasoning_effort" in msg and not self._no_reasoning_effort:
                self._no_reasoning_effort = True
                return again()
            if ("response_format" in msg or "json_schema" in msg or "schema" in msg) and self._format_level < 2:
                self._format_level += 1
                log.warning("OpenAI endpoint rejected response format; falling back to level %s", self._format_level)
                return again()
            if "model" in msg and ("does not exist" in msg or "not supported" in msg) and self._switch_model(tier, model, e):
                return again()
            raise LLMError(f"OpenAI request geweigerd: {e}") from e
        except openai.AuthenticationError as e:
            raise LLMFatalError(
                "Ongeldige OPENAI_API_KEY. Maak een nieuwe key op platform.openai.com → API keys en zet die in "
                "Instellingen of .env."
            ) from e
        except (openai.NotFoundError, openai.PermissionDeniedError) as e:
            if self._switch_model(tier, model, e):
                return again()
            raise LLMFatalError(f"OpenAI: geen toegang tot model {model}. Kies een ander model in Instellingen → AI. ({e})") from e
        except openai.RateLimitError as e:
            if "insufficient_quota" in str(e) or "exceeded your current quota" in str(e).lower():
                raise LLMFatalError(QUOTA_MESSAGE) from e
            raise LLMError(f"OpenAI rate limit bereikt, probeer het later opnieuw: {e}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"OpenAI API fout {e.status_code}: {e}") from e
        except openai.APIConnectionError as e:
            message = connection_error_message("OpenAI", e, self.client.base_url)
            log.warning("%s", message, exc_info=True)
            raise LLMFatalError(message) from e

        choice = resp.choices[0]
        text = choice.message.content or ""
        refusal = getattr(choice.message, "refusal", None)
        if refusal and not text:
            raise LLMError(f"Model weigerde: {refusal}")
        u = resp.usage
        in_tok = int(getattr(u, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(u, "completion_tokens", 0) or 0)  # includes hidden reasoning tokens
        details = getattr(u, "prompt_tokens_details", None)
        cached = min(in_tok, int(getattr(details, "cached_tokens", 0) or 0)) if details is not None else 0
        served = getattr(resp, "model", None) or model
        usage = LLMUsage(model=served, input_tokens=in_tok, output_tokens=out_tok,
                         cost_usd=estimate_cost(served, in_tok - cached, out_tok, cached))
        if choice.finish_reason == "length" and not text.rstrip().endswith("}"):
            raise LLMError("Antwoord afgekapt (token limiet); verklein de batchgrootte")
        return LLMResult(data=parse_json_loose(text), usage=usage)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        try:
            resp = self.client.embeddings.create(model=self.embedding_model, input=texts)
        except openai.OpenAIError as e:
            log.warning("Embeddings unavailable (%s); falling back to TF-IDF", e)
            return None
        return [d.embedding for d in resp.data]
