import pytest

from app.ai.scoring import DIMENSIONS, compute_viral_score, geometric_funnel, potential_label, stage_scores
from app.services.settings_store import DEFAULT_STAGE_WEIGHTS, DEFAULT_WEIGHTS


def dims(value: float, **overrides) -> dict:
    d = dict.fromkeys(DIMENSIONS, value)
    d.update(overrides)
    return d


def score(d, **kw):
    return compute_viral_score(d, weights=DEFAULT_WEIGHTS, stage_weights=DEFAULT_STAGE_WEIGHTS, **kw)


def test_uniform_scores_map_to_same_value():
    assert score(dims(80)).viral_score == pytest.approx(80, abs=0.2)


def test_weak_hook_is_a_bottleneck():
    """Funnel: great engagement cannot rescue a clip nobody keeps watching."""
    strong_hook = score(dims(70, hook=90, hook_strength=90, curiosity=85, shareability=60))
    weak_hook = score(dims(70, hook=15, hook_strength=15, curiosity=20, shareability=95, comment_potential=95))
    assert strong_hook.viral_score > weak_hook.viral_score + 10
    # And the geometric mean punishes a weak stage harder than an arithmetic mean would.
    st = stage_scores(dims(70, hook=15, hook_strength=15, curiosity=20), DEFAULT_WEIGHTS)
    arithmetic = sum(st[k] * DEFAULT_STAGE_WEIGHTS[k] for k in st)
    assert geometric_funnel(st, DEFAULT_STAGE_WEIGHTS) < arithmetic


def test_weights_are_configurable():
    d = dims(50, humor=100)
    base = compute_viral_score(d, weights=DEFAULT_WEIGHTS, stage_weights=DEFAULT_STAGE_WEIGHTS).viral_score
    heavy = compute_viral_score(d, weights=DEFAULT_WEIGHTS | {"humor": 5.0}, stage_weights=DEFAULT_STAGE_WEIGHTS).viral_score
    assert heavy > base


def test_flags_verdict_crowd_and_personal_adjustment():
    d = dims(85)
    clean = score(d).viral_score
    assert score(d, flags=["needs_context"]).viral_score < clean
    assert score(d, flags=["sponsor_or_ad"]).viral_score < score(d, flags=["needs_context"]).viral_score
    assert score(d, verdict="skip").viral_score <= 45
    assert score(d, verdict="maybe").viral_score <= 76
    assert score(d, crowd=1.0).viral_score == pytest.approx(min(100, clean + 6), abs=0.2)
    assert score(d, personal_adjustment=-10).viral_score == pytest.approx(clean - 10, abs=0.2)
    assert 0 <= score(dims(100), crowd=1, personal_adjustment=50).viral_score <= 100


def test_signal_blend():
    d = dims(80)
    blended = score(d, signal=40, signal_blend=0.25)
    assert blended.viral_score == pytest.approx(0.75 * 80 + 0.25 * 40, abs=0.3)
    assert blended.breakdown["signal_score"] == 40


def test_potential_label_never_promises():
    assert potential_label(96) == "High viral potential"
    assert "viral" not in potential_label(40).lower() or "potential" in potential_label(40).lower()
