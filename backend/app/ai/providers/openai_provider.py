"""OpenAI provider (also works with OpenAI-compatible endpoints via OPENAI_BASE_URL, e.g. a local
Ollama/vLLM server or OpenRouter, which is a good way to cut costs further)."""

from __future__ import annotations

import logging
from typing import Any

import openai

from app.ai.llm import ImageInput, LLMError, LLMResult, LLMUsage, estimate_cost, parse_json_loose

log = logging.getLogger(__name__)

DEFAULT_FAST = "gpt-5-mini"
DEFAULT_SMART = "gpt-5"
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


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
    ):
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=3, timeout=300.0)
        self.models = {
            "fast": fast or DEFAULT_FAST,
            "smart": smart or DEFAULT_SMART,
            "vision": vision or smart or DEFAULT_SMART,
        }
        self.embedding_model = embedding_model
        # Degrade gracefully on compatible servers: json_schema -> json_object -> plain prompt.
        self._format_level = 0
        self._no_reasoning_effort = False

    def _response_format(self, schema: dict[str, Any], name: str) -> dict[str, Any] | None:
        if self._format_level == 0:
            return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}
        if self._format_level == 1:
            return {"type": "json_object"}
        return None

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
            "max_completion_tokens": max(max_tokens, 12000) if model.startswith(_REASONING_PREFIXES) else max_tokens,
        }
        rf = self._response_format(schema, schema_name)
        if rf:
            kwargs["response_format"] = rf
        if model.startswith(_REASONING_PREFIXES) and not self._no_reasoning_effort:
            kwargs["reasoning_effort"] = "low" if tier == "fast" else "medium"
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            msg = str(e).lower()
            if "reasoning_effort" in msg and not self._no_reasoning_effort:
                self._no_reasoning_effort = True
                return self.complete_json(system=system, user=user, schema=schema, schema_name=schema_name,
                                          tier=tier, max_tokens=max_tokens, images=images)
            if ("response_format" in msg or "json_schema" in msg or "schema" in msg) and self._format_level < 2:
                self._format_level += 1
                log.warning("OpenAI endpoint rejected response format; falling back to level %s", self._format_level)
                return self.complete_json(system=system, user=user, schema=schema, schema_name=schema_name,
                                          tier=tier, max_tokens=max_tokens, images=images)
            raise LLMError(f"OpenAI request geweigerd: {e}") from e
        except openai.AuthenticationError as e:
            raise LLMError("Ongeldige OPENAI_API_KEY") from e
        except openai.APIStatusError as e:
            raise LLMError(f"OpenAI API fout {e.status_code}: {e}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"OpenAI API niet bereikbaar: {e}") from e

        choice = resp.choices[0]
        text = choice.message.content or ""
        refusal = getattr(choice.message, "refusal", None)
        if refusal and not text:
            raise LLMError(f"Model weigerde: {refusal}")
        u = resp.usage
        in_tok = int(getattr(u, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(u, "completion_tokens", 0) or 0)
        usage = LLMUsage(model=model, input_tokens=in_tok, output_tokens=out_tok, cost_usd=estimate_cost(model, in_tok, out_tok))
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
