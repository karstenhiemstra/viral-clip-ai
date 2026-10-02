"""Regression: Docker passes the empty ``OPENAI_BASE_URL=`` line of .env as "". The OpenAI SDK then used ""
as its URL and every call failed with "Connection error." before leaving the container."""

import httpx
import pytest

from app.ai.llm import LLMFatalError
from app.ai.providers.openai_provider import OpenAIProvider
from app.ai.transcription import OpenAITranscriber
from app.config import get_settings, openai_base_url

CHAT_OK = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5-mini-2025-08-07",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok": true}'}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


@pytest.fixture()
def docker_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "")  # exactly what the container gets from .env
    monkeypatch.setattr(get_settings(), "openai_base_url", "")


def _with_transport(llm: OpenAIProvider, handler) -> None:
    llm.client = llm.client.with_options(max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_empty_base_url_env_still_calls_openai_with_the_key(docker_env):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=CHAT_OK)

    llm = OpenAIProvider(api_key="sk-test-123")
    _with_transport(llm, handler)
    res = llm.complete_json(system="s", user="u", schema={"type": "object"}, schema_name="health", tier="fast")
    assert res.data == {"ok": True}
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-123"
    assert str(OpenAITranscriber("sk-test-123").client.base_url) == "https://api.openai.com/v1/"


def test_connection_error_names_the_url_and_real_cause_but_not_the_key(docker_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)

    llm = OpenAIProvider(api_key="sk-secret-999")
    _with_transport(llm, handler)
    with pytest.raises(LLMFatalError) as exc:
        llm.complete_json(system="s", user="u", schema={"type": "object"}, schema_name="health", tier="fast")
    msg = str(exc.value)
    assert "https://api.openai.com/v1" in msg and "ConnectError" in msg and "Name or service not known" in msg
    assert "sk-secret-999" not in msg


@pytest.mark.parametrize(("configured", "expected"), [
    ("", "https://api.openai.com/v1"),
    ("  ", "https://api.openai.com/v1"),
    ('""', "https://api.openai.com/v1"),
    ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/v1"),
    ("localhost:11434/v1", "https://localhost:11434/v1"),
])
def test_openai_base_url_is_always_explicit(monkeypatch, configured, expected):
    monkeypatch.setattr(get_settings(), "openai_base_url", configured)
    assert openai_base_url() == expected
