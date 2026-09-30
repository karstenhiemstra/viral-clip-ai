import pytest

from app.ai.llm import parse_json_loose, estimate_cost, LLMError, get_llm
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
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
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
    assert {s["stage"] for s in out.stage_log} >= {"transcript", "candidates", "evaluation", "scoring", "selection"}


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
    assert estimate_cost("gpt-5-mini-2026", 1_000_000, 0) == pytest.approx(0.25)
    assert estimate_cost("unknown-model", 1000, 1000) == 0.0


def test_get_llm_selection(db):
    rs = load_settings(db)
    assert get_llm(db, rs) is None  # no keys -> heuristic
    set_secret(db, "anthropic_api_key", "sk-ant-test")
    llm = get_llm(db, rs)
    assert llm.provider == "anthropic"
    assert llm.models["fast"] == "claude-haiku-4-5" and llm.models["smart"] == "claude-opus-5-5"
    set_secret(db, "openai_api_key", "sk-test")
    assert get_llm(db, rs).provider == "openai"
    rs.ai.llm_provider = "heuristic"
    assert get_llm(db, rs) is None
