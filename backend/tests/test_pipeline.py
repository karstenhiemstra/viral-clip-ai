import pytest

from app.ai.llm import LLMError, LLMFatalError, estimate_cost, get_llm, parse_json_loose, resolve_models
from app.ai.pipeline import analyze_video
from app.models import Transcript, Video
from app.services.queue import JobWaiting
from app.services.settings_store import RuntimeSettings, load_settings, set_secret, update_settings
from tests.fakes import FakeLLM


def _video_with_transcript(db, words, title="Supermarkt vlog"):
    v = Video(title=title, source="manual", duration_seconds=words[-1].end + 2)
    db.add(v)
    db.commit()
    db.add(Transcript(video_id=v.id, source="test", words=[w.to_row() for w in words], full_text=""))
    db.commit()
    db.refresh(v)
    return v


def _assert_valid_output(out, rs):
    assert 1 <= len(out.plans) <= rs.clips.max_per_video
    spans = sorted((p.window.start, p.window.end) for p in out.plans)
    for (a0, a1), (b0, b1) in zip(spans, spans[1:], strict=False):
        overlap = max(0.0, min(a1, b1) - max(a0, b0))
        assert overlap <= 0.35 * min(a1 - a0, b1 - b0) + 1e-6
    for p in out.plans:
        assert 0 <= p.viral_score <= 100
        assert p.window.duration <= rs.clips.max_seconds + 4.5
        assert p.words and p.text
        assert set(p.scores) >= {"hook", "retention", "shareability"}
    scores = [p.viral_score for p in out.plans]
    assert scores[0] == max(scores)


def test_heuristic_pipeline_end_to_end(db, sample_words):
    v = _video_with_transcript(db, sample_words)
    rs = load_settings(db)
    out = analyze_video(db, v, None, rs, llm=None)
    _assert_valid_output(out, rs)
    assert out.provider == "heuristic"
    assert all("abonneren" not in p.text for p in out.plans[:2])
    assert any(c.get("selected") for c in out.candidates_log)
    assert {s["stage"] for s in out.stage_log} >= {"transcript", "pass2_shortlist", "pass3_evaluation", "scoring", "selection"}


def test_llm_pipeline_uses_both_passes(db, sample_words):
    v = _video_with_transcript(db, sample_words)
    rs = load_settings(db)
    llm = FakeLLM()
    out = analyze_video(db, v, None, rs, llm=llm)
    _assert_valid_output(out, rs)
    assert "moments" in llm.calls and "evaluations" in llm.calls
    top = out.plans[0]
    assert any(k in top.text.lower() for k in ("wacht", "banaan", "miljoen", "loterij", "fake"))
    assert top.breakdown["mode"] == "llm"
    assert top.title == "Dit geloof je niet"
    assert "nietbestaand" not in top.emphasis_words  # hallucinated emphasis words are dropped
    assert out.meter.cost_usd > 0 and out.meter.input_tokens > 0


class _NoCreditLLM(FakeLLM):
    def complete_json(self, **kw):
        self.calls.append(kw["schema_name"])
        raise LLMFatalError("Je OpenAI-tegoed is op")


def test_fatal_llm_error_falls_back_to_heuristics_after_one_call(db, sample_words):
    v = _video_with_transcript(db, sample_words)
    rs = load_settings(db)
    llm = _NoCreditLLM()
    out = analyze_video(db, v, None, rs, llm=llm)
    _assert_valid_output(out, rs)
    assert llm.calls == ["moments"]  # no retries on every chunk, no pass 3 on a dead key
    assert out.provider == "heuristic"
    assert any("tegoed" in w for w in out.warnings)


def test_pipeline_waits_for_media_when_no_transcript(db):
    v = Video(title="Zonder bron", source="manual")
    db.add(v)
    db.commit()
    with pytest.raises(JobWaiting):
        analyze_video(db, v, None, RuntimeSettings(), llm=None)


def test_max_clips_setting_is_respected(db, sample_words):
    update_settings(db, {"clips": {"max_per_video": 2}})
    v = _video_with_transcript(db, sample_words)
    out = analyze_video(db, v, None, load_settings(db), llm=None)
    assert len(out.plans) <= 2


def test_parse_json_loose_and_costs():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        parse_json_loose("no json here")
    assert estimate_cost("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0)
    assert estimate_cost("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.0)
    assert estimate_cost("gpt-5-mini-2025-08-07", 1_000_000, 0) == pytest.approx(0.25)
    assert estimate_cost("gpt-5", 0, 0, 1_000_000) == pytest.approx(0.125)  # cached input
    assert estimate_cost("gpt-5.4-mini", 1_000_000, 0) == pytest.approx(0.75)  # longest prefix wins
    assert estimate_cost("unknown-model", 1000, 1000) == 0.0


def test_get_llm_selection(db):
    rs = load_settings(db)
    assert get_llm(db, rs) is None  # no keys -> heuristic
    set_secret(db, "anthropic_api_key", "sk-ant-test")
    llm = get_llm(db, rs)
    assert llm.provider == "anthropic"
    assert llm.models["fast"] == "claude-haiku-4-5" and llm.models["smart"] == "claude-sonnet-5-5"
    set_secret(db, "openai_api_key", "sk-test")
    llm = get_llm(db, rs)
    assert llm.provider == "openai"
    assert llm.models == {"fast": "gpt-5-mini", "smart": "gpt-5", "vision": "gpt-5"}
    assert llm.efforts["smart"] == "low"
    rs.ai.quality = "best"
    assert get_llm(db, rs).efforts["smart"] == "medium"
    rs.ai.quality = "budget"
    assert get_llm(db, rs).models["smart"] == "gpt-5-mini"
    rs.ai.model_smart = "gpt-5.4"  # explicit model wins over the preset
    assert get_llm(db, rs).models["smart"] == "gpt-5.4"
    rs.ai.llm_provider = "heuristic"
    assert get_llm(db, rs) is None


def test_resolve_models_presets():
    models, efforts = resolve_models("anthropic", "best")
    assert models["smart"] == "claude-opus-5-5" and efforts["smart"] == "medium" and efforts["fast"] is None
    models, _ = resolve_models("openai", "nonsense")
    assert models["smart"] == "gpt-5"


def test_openai_provider_maps_quota_and_model_errors(monkeypatch):
    import httpx
    import openai

    from app.ai.providers.openai_provider import OpenAIProvider

    def err(cls, status, body):
        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        return cls(body["error"]["message"], response=httpx.Response(status, request=req, json=body), body=body)

    llm = OpenAIProvider(api_key="sk-test")
    seen: list[str] = []

    def create(**kw):
        seen.append(kw["model"])
        if kw["model"] == "gpt-5":
            raise err(openai.NotFoundError, 404, {"error": {"message": "The model `gpt-5` does not exist", "code": "model_not_found"}})
        raise err(openai.RateLimitError, 429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}})

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    with pytest.raises(LLMFatalError, match="tegoed"):
        llm.complete_json(system="s", user="u", schema={"type": "object"}, schema_name="x", tier="smart")
    assert seen[:2] == ["gpt-5", "gpt-5-mini"]  # unavailable model -> automatic fallback
    assert llm.models["smart"] == "gpt-5-mini"


def test_cost_estimate_grows_with_length_and_quality():
    from app.ai.costs import estimate_video_cost

    ten = estimate_video_cost(10, "openai", "balanced")
    sixty = estimate_video_cost(60, "openai", "balanced")
    assert ten["models"] == {"pass1": "gpt-5-mini", "pass3": "gpt-5"}
    assert 0 < ten["total"][0] <= ten["total"][1] < sixty["total"][0]
    assert ten["transcription"] == pytest.approx(0.06)
    # pass 3 only sees the shortlist, so it does not grow with video length
    assert ten["pass3"] == sixty["pass3"]
    assert estimate_video_cost(10, "openai", "best")["total"][1] > ten["total"][1]
    assert estimate_video_cost(10, "heuristic", transcribe_with_whisper_api=False)["total"] == [0.0, 0.0]
