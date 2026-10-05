"""Clips are complete mini conversations of 10-15 seconds: HOOK (first 2 s) -> context -> REACTION / outcome.

The selection keeps the hook in the first two seconds, looks ahead for a punchline or reaction that comes
a little later, always includes the reaction to what is said, and rejects windows that cannot be such a
complete mini conversation within the limits (or that, according to the AI, have no payoff)."""

import numpy as np
import pytest

from app.ai.boundaries import HOOK_WINDOW, REJECT_FLAGS, DurationRules, optimize_boundaries
from app.ai.candidates import Candidate
from app.ai.evaluator import evaluate_llm, heuristic_flags, sanitize_evaluation
from app.ai.llm import LLMResult, LLMUsage, UsageMeter
from app.ai.pipeline import analyze_video, duration_rules
from app.ai.prompts import CANDIDATE_SYSTEM, EVALUATION_SCHEMA, EVALUATOR_SYSTEM
from app.ai.scoring import DIMENSIONS, compute_viral_score
from app.ai.signals import VideoContext, heuristic_dimension_scores, window_features
from app.ai.transcript import segment_sentences
from app.models import Creator, Transcript, Video
from app.services.settings_store import ClipSettings, RuntimeSettings, load_settings
from app.video.audio_features import compute_profile
from tests.conftest import make_words
from tests.fakes import FakeLLM

RULES = DurationRules()  # 10-15 s


def ctx_for(lines: list[str], loud: tuple[int, ...] = ()) -> VideoContext:
    """Transcript of ``lines``; with ``loud`` the sentences at those indexes are loud (shouting, laughing,
    several people reacting) and the rest is normal speech."""
    words = make_words(lines)
    sents = segment_sentences(words)
    ctx = VideoContext(words=words, sentences=sents, duration=words[-1].end + 1)
    if loud:
        sr = 8000
        t = np.arange(int(sr * ctx.duration)) / sr
        amp = np.full_like(t, 0.1)
        for i in loud:
            amp[(t >= sents[i].start) & (t <= sents[i].end)] = 0.9
        ctx.audio = compute_profile((amp * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)
    return ctx


def assert_complete(ctx, w, hook: int):
    """Within 10-15 s, the hook in the first 2 seconds, not rejected."""
    assert RULES.min_seconds <= w.duration <= RULES.max_seconds + 1e-6
    assert w.end - w.start <= RULES.max_seconds + 1e-6
    assert ctx.sentences[hook].start - w.start <= HOOK_WINDOW + 1e-6
    assert not set(w.flags) & set(REJECT_FLAGS), w.flags


def test_the_reaction_of_the_other_person_is_part_of_the_clip():
    """Someone says that Sanne is a triplet -> the clip must also contain the other person's reaction."""
    ctx = ctx_for([
        "We zaten gisteren gewoon met z'n allen in de kantine.",
        "Wist je trouwens dat Sanne eigenlijk een drieling is?",
        "Ze heeft twee zussen die er precies hetzelfde uitzien.",
        "Ik zag ze vorige week samen lopen.",
        "Wat?! Een drieling? Dat meen je niet!",
        "Daarna zijn we gewoon verder gaan eten met elkaar.",
    ])
    w = optimize_boundaries(ctx, 1, 3, RULES, hook_s=1)
    assert ctx.sentences[5].text == "Dat meen je niet!"
    assert (w.s0, w.s1) == (1, 5), "the whole mini conversation, ending on the (two-line) reaction"
    assert_complete(ctx, w, hook=1)


def test_punchline_that_comes_later_is_included():
    """The candidate stops on the set-up; the punchline follows: look ahead and include it."""
    ctx = ctx_for([
        "We vierden gisteren kerst bij mijn opa en oma.",
        "Mijn oom wilde per se zelf de kerstboom ophangen!",
        "Hij stond op een wiebelig krukje.",
        "Toen zei mijn tante dat het plafond los zat.",
        "En de boom viel op zijn hoofd hahaha.",
        "Daarna hebben we gewoon taart gegeten.",
    ])
    w = optimize_boundaries(ctx, 1, 3, RULES, hook_s=1)
    assert (w.s0, w.s1) == (1, 4), "the clip must end on the punchline, not before it"
    assert_complete(ctx, w, hook=1)


def test_a_payoff_that_does_not_fit_is_rejected():
    """Hook -> punchline takes more than 15 s and there is no later hook: not a complete clip."""
    ctx = ctx_for([
        "Mijn oom wilde per se zelf de kerstboom ophangen!",
        "Hij stond op een heel wiebelig krukje in de woonkamer.",
        "Toen zei mijn tante dat het plafond van gips was.",
        "En de hele boom viel toen zo op zijn hoofd hahaha.",
    ])
    w = optimize_boundaries(ctx, 0, 2, RULES, hook_s=0)
    assert "incomplete_story" in w.flags


def test_reactions_of_several_people_are_included():
    """Somebody says something; the loud reaction of the group comes after the context."""
    ctx = ctx_for([
        "Ik heb jullie iets heel belangrijks te vertellen vandaag!",
        "Ik heb ons allemaal ingeschreven voor de marathon.",
        "Het is morgen al om zeven uur.",
        "Wat?! Nee echt niet, ben je gek geworden?!",
        "Oké we gaan nu even boodschappen doen.",
    ], loud=(3,))
    w = optimize_boundaries(ctx, 0, 1, RULES, hook_s=0)
    assert (w.s0, w.s1) == (0, 3), "the reaction (the climax) must be in the clip"
    assert_complete(ctx, w, hook=0)
    assert "ends_before_payoff" not in heuristic_flags(window_features(ctx, w.start, w.end, ctx.words[w.w0 : w.w1]))


def test_an_interesting_line_after_8_seconds_moves_the_start():
    """The AI span starts 8+ seconds before the interesting statement: start on the statement instead."""
    ctx = ctx_for([
        "We lopen hier gewoon een beetje rond in de stad.",
        "Het weer is vandaag eigenlijk best wel lekker zonnig.",
        "We gaan straks nog even wat eten bij de snackbar.",
        "Wacht, mijn moeder heeft net de loterij gewonnen!",
        "Een miljoen euro, ze belde me net helemaal in paniek op.",
        "Wat?! Dat meen je niet, echt een miljoen?",
        "Ja echt, ze kon het zelf ook niet geloven vandaag.",
    ])
    assert ctx.sentences[3].start - ctx.sentences[0].start > 8
    w = optimize_boundaries(ctx, 0, 5, RULES, hook_s=3, climax_s=4, reaction_s=5, heuristic=False)
    assert w.s0 == 3 and w.s1 >= 5
    assert_complete(ctx, w, hook=3)


def test_too_short_moments_are_rejected():
    ctx = ctx_for(["Wacht wat?! Dit is echt niet normaal!", "Hahaha nee joh!"])
    w = optimize_boundaries(ctx, 0, 1, RULES, hook_s=0)
    assert "too_short" in w.flags


@pytest.mark.parametrize("start", range(0, 20, 3))
def test_every_clip_is_10_to_15_seconds(start):
    lines = [f"Dit is zin {i} van een lang gesprek{'!' if i % 4 == 0 else '.'}" for i in range(30)]
    ctx = ctx_for(lines)
    w = optimize_boundaries(ctx, start, start + 6, RULES, hook_s=start)
    assert w.end - w.start <= RULES.max_seconds + 1e-6
    assert RULES.min_seconds <= w.duration <= RULES.max_seconds + 1e-6
    assert ctx.sentences[[s for s in range(w.s0, w.s1 + 1)][0]].start - w.start <= HOOK_WINDOW


def test_ai_hook_climax_and_reaction_shape_the_edit():
    c = Candidate("c1", s0=10, s1=12)
    base = {"scores": dict.fromkeys(DIMENSIONS, 60), "verdict": "good", "start_sentence": 10, "end_sentence": 12}
    ev = sanitize_evaluation({**base, "hook_sentence": 11, "climax_sentence": 14, "reaction_sentence": 15,
                              "flags": ["ends_before_payoff"]}, c, 40)
    assert (ev["hook_s"], ev["climax_s"], ev["reaction_s"]) == (11, 14, 15)
    assert (ev["s0"], ev["s1"]) == (10, 15)  # never ends before the reaction
    assert "ends_before_payoff" not in ev["flags"]  # fixed: the payoff is in the clip now
    # out of what the model saw: ignored; "no climax at all" is a flag (the clip is then rejected)
    other = sanitize_evaluation({**base, "climax_sentence": 30, "reaction_sentence": -1}, c, 40)
    assert other["climax_s"] is None and other["reaction_s"] is None and other["hook_s"] == 10
    assert "no_climax" in sanitize_evaluation({**base, "climax_sentence": -1}, c, 40)["flags"]


def test_evaluator_looks_ahead_and_keeps_the_climax():
    lines = [f"Dit is zin nummer {i} van het verhaal." for i in range(20)]
    ctx = ctx_for(lines)
    seen: list[str] = []

    class StoryLLM:
        provider = "fake"
        models = {"smart": "fake"}

        def complete_json(self, *, system, user, schema, schema_name, tier="smart", max_tokens=8000, images=None):
            seen.append(user)
            ev = {"id": "c1", "scores": dict.fromkeys(DIMENSIONS, 70), "flags": [], "verdict": "good",
                  "start_sentence": 2, "end_sentence": 3, "hook_sentence": 2, "climax_sentence": 4,
                  "reaction_sentence": 5, "category": "story"}
            return LLMResult({"evaluations": [ev]}, LLMUsage(model="fake", input_tokens=1, output_tokens=1, cost_usd=0.0))

    c = Candidate("c1", s0=2, s1=3)
    assert evaluate_llm(StoryLLM(), ctx, [c], min_s=10, max_s=15, target_s=13, output_language="nl", batch_size=6,
                        meter=UsageMeter()) == 1
    assert "[s9 " in seen[0] and "[s10 " not in seen[0]  # 6 sentences of look-ahead after the candidate
    assert "at most 15 seconds (hard limits)" in seen[0]
    assert (c.evaluation["s1"], c.evaluation["climax_s"], c.evaluation["reaction_s"]) == (5, 4, 5)
    ev = c.evaluation
    w = optimize_boundaries(ctx, ev["s0"], ev["s1"], RULES, hook_s=ev["hook_s"], climax_s=ev["climax_s"],
                            reaction_s=ev["reaction_s"], heuristic=False)
    assert w.s0 == 2 and w.s1 >= 5
    assert_complete(ctx, w, hook=2)


def test_the_rubric_asks_for_a_complete_mini_conversation():
    item = EVALUATION_SCHEMA["properties"]["evaluations"]["items"]
    assert {"hook_sentence", "climax_sentence", "reaction_sentence"} <= set(item["required"])
    assert {"buildup", "ending", "standalone"} <= set(item["properties"]["scores"]["required"])
    for text in (EVALUATOR_SYSTEM, CANDIDATE_SYSTEM):
        assert "2 seconds" in text and "triplet" in text and "reaction" in text
    for phrase in ("Look ahead", "ends_before_payoff", "no_climax", "starts_mid_story", "after 8 seconds"):
        assert phrase in EVALUATOR_SYSTEM


def test_score_punishes_stopping_before_the_payoff_and_rewards_a_complete_story():
    weights = dict.fromkeys(DIMENSIONS, 1.0)
    stages = {"stop": 0.4, "hold": 0.35, "engage": 0.25}
    dims = dict.fromkeys(DIMENSIONS, 70.0)

    def score(d, flags=()):
        return compute_viral_score(d, weights=weights, stage_weights=stages, flags=list(flags)).viral_score

    complete = score(dims)
    assert score(dims, ["ends_before_payoff"]) < score(dims, ["weak_payoff"]) < complete
    assert score(dims, ["ends_mid_sentence"]) < complete and score(dims, ["starts_mid_story"]) < complete
    no_arc = {**dims, "buildup": 25.0, "ending": 20.0, "standalone": 30.0}
    assert score(no_arc) < complete - 5


def test_heuristic_scores_see_a_payoff_after_the_cut():
    lines = ["Ik ga jullie nu iets laten zien.", "Kijk wat er gebeurt als ik hier op druk.", "Boem alles ontploft!"]
    ctx = ctx_for(lines, loud=(2,))
    cut = ctx.sentences[1].end  # the clip stops right before the explosion
    early = window_features(ctx, ctx.sentences[0].start, cut)
    full = window_features(ctx, ctx.sentences[0].start, ctx.sentences[2].end)
    assert early["payoff_after_end"] == 1.0 and "ends_before_payoff" in heuristic_flags(early)
    s_early, s_full = heuristic_dimension_scores(early), heuristic_dimension_scores(full)
    assert s_full["ending"] > s_early["ending"] and s_full["payoff"] > s_early["payoff"]


def test_clip_length_is_always_10_to_15_seconds():
    old = ClipSettings(min_seconds=12, max_seconds=18, target_seconds=15)  # the old default, saved earlier
    assert (old.min_seconds, old.max_seconds) == (10, 15)
    wide = ClipSettings(min_seconds=4, max_seconds=40, target_seconds=30)
    assert (wide.min_seconds, wide.max_seconds, wide.target_seconds) == (10, 15, 15)
    creator = Creator(name="c", youtube_channel_id="UC_x", clip_min_seconds=6, clip_max_seconds=25)
    rules = duration_rules(RuntimeSettings(), creator)
    assert (rules.min_seconds, rules.max_seconds) == (10, 15)


class _NoPayoffLLM(FakeLLM):
    """Says every candidate has no climax at all."""

    def complete_json(self, **kw):
        res = super().complete_json(**kw)
        for ev in res.data.get("evaluations", []):
            ev["climax_sentence"] = -1
        return res


def test_clips_without_a_payoff_are_rejected(db, sample_words):
    v = Video(title="Vlog", source="manual", duration_seconds=sample_words[-1].end + 2)
    db.add(v)
    db.commit()
    db.add(Transcript(video_id=v.id, source="test", words=[w.to_row() for w in sample_words], full_text=""))
    db.commit()
    db.refresh(v)
    out = analyze_video(db, v, None, load_settings(db), llm=_NoPayoffLLM())
    assert out.plans == []
    assert any("afgewezen" in w and "geen duidelijke payoff" in w for w in out.warnings)
    assert any(str(r.get("note", "")).startswith("afgewezen") for r in out.candidates_log)
