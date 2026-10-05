"""Clips are complete mini stories: build-up -> tension/context -> CLIMAX/PAYOFF -> (reaction).

The selection looks ahead for a climax that comes later than the first interesting line, never ends just
before the payoff, adds a little setup when a clip would open right on its climax, and the score punishes
clips that stop before the payoff or have no climax at all."""

import numpy as np

from app.ai.boundaries import DurationRules, optimize_boundaries
from app.ai.candidates import Candidate
from app.ai.evaluator import evaluate_llm, heuristic_flags, sanitize_evaluation
from app.ai.llm import LLMResult, LLMUsage, UsageMeter
from app.ai.prompts import EVALUATION_SCHEMA, EVALUATOR_SYSTEM
from app.ai.scoring import DIMENSIONS, compute_viral_score
from app.ai.signals import VideoContext, heuristic_dimension_scores, window_features
from app.ai.transcript import segment_sentences
from app.video.audio_features import compute_profile
from tests.conftest import make_words

RULES = DurationRules(min_seconds=4, max_seconds=18, target_seconds=14)


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


def test_punchline_that_comes_later_is_included():
    """The candidate stops on the set-up; the punchline is two sentences later: look ahead and include it."""
    ctx = ctx_for([
        "Mijn oom wilde per se zelf de kerstboom ophangen aan het plafond.",
        "Hij stond op een krukje met de boom boven zijn hoofd.",
        "Toen zei mijn tante dat het plafond van gips was.",
        "En de hele boom kwam naar beneden op zijn hoofd hahaha.",
        "Daarna hebben we gewoon taart gegeten.",
    ])
    w = optimize_boundaries(ctx, 0, 1, RULES, hook_s=0)
    assert w.s1 == 3, "the clip must end on the punchline, not before it"
    assert w.s0 == 0


def test_reactions_of_several_people_after_the_first_moment_are_included():
    """Somebody says something interesting; the big reaction of the group comes two sentences later."""
    ctx = ctx_for([
        "Ik heb jullie iets te vertellen over vanavond.",
        "Ik heb ons allemaal ingeschreven voor de marathon.",
        "Het is morgenochtend al om zeven uur.",
        "Wat?! Nee echt niet, ben je gek geworden?!",
        "Oké we gaan nu even boodschappen doen.",
    ], loud=(3,))
    w = optimize_boundaries(ctx, 0, 1, RULES, hook_s=0)
    assert w.s1 == 3, "the reaction (the climax) must be in the clip"
    assert "ends_before_payoff" not in heuristic_flags(window_features(ctx, w.start, w.end, ctx.words[w.w0 : w.w1]))


def test_clip_that_opens_on_its_climax_gets_the_setup_before_it():
    """An event builds up; a candidate that starts right on the climax starts a few seconds earlier."""
    ctx = ctx_for([
        "We staan hier bij het hoogste podium van het festival.",
        "Hij gaat nu de sprong proberen die nog nooit iemand deed.",
        "En hij valt keihard van het podium af!",
        "Gelukkig was er niks aan de hand met hem.",
        "Iedereen klapte toen hij weer opstond.",
    ], loud=(2,))
    climax = 2
    w = optimize_boundaries(ctx, climax, 4, RULES, hook_s=climax)
    assert w.s0 < climax, "the viewer needs the build-up before the fall"
    assert ctx.sentences[climax].start - ctx.sentences[w.s0].start <= 5.0, "at most a few seconds earlier"
    assert w.s0 <= climax <= w.s1


def test_ai_climax_in_the_context_after_extends_the_edit():
    c = Candidate("c1", s0=10, s1=12)
    base = {"scores": dict.fromkeys(DIMENSIONS, 60), "verdict": "good", "start_sentence": 10, "end_sentence": 12}
    ev = sanitize_evaluation({**base, "climax_sentence": 15, "flags": ["ends_before_payoff"]}, c, 40)
    assert (ev["s0"], ev["s1"], ev["climax_s"]) == (10, 15, 15)
    assert "ends_before_payoff" not in ev["flags"]  # fixed: the climax is in the clip now
    # a climax the model did not see is ignored, "no climax at all" is a flag
    assert sanitize_evaluation({**base, "climax_sentence": 30}, c, 40)["climax_s"] is None
    assert "no_climax" in sanitize_evaluation({**base, "climax_sentence": -1}, c, 40)["flags"]


def test_a_complete_story_may_run_longer_but_never_loses_its_climax():
    """17 seconds with the payoff beats 14 seconds that stops before it - within the tolerance."""
    lines = [
        "Mijn buurman heeft al weken een enorme doos in zijn tuin staan.",
        "Niemand weet wat erin zit en hij zegt er niks over.",
        "Vandaag deed hij eindelijk de doos open terwijl wij keken.",
        "Er zat een tweede doos in met een briefje: je bent geprankt!",
    ]
    ctx = ctx_for(lines)
    climax = 3
    w = optimize_boundaries(ctx, 0, climax, RULES, hook_s=None, climax_s=climax)
    assert (w.s0, w.s1) == (0, climax), "the whole story stays, including its payoff"
    assert RULES.max_seconds < w.source_duration <= RULES.max_seconds + RULES.tolerance
    # without a known climax the same span is cut back to the target length
    plain = optimize_boundaries(ctx, 0, climax, RULES, hook_s=None)
    assert plain.source_duration < w.source_duration


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
                  "start_sentence": 2, "end_sentence": 4, "climax_sentence": 9, "category": "story"}
            return LLMResult({"evaluations": [ev]}, LLMUsage(model="fake", input_tokens=1, output_tokens=1, cost_usd=0.0))

    c = Candidate("c1", s0=2, s1=4)
    assert evaluate_llm(StoryLLM(), ctx, [c], min_s=12, max_s=18, target_s=15, output_language="nl", batch_size=6,
                        meter=UsageMeter(), story_max_s=21) == 1
    assert "[s10 " in seen[0] and "[s11 " not in seen[0]  # 6 sentences of look-ahead after the candidate
    assert "maximum story length 21" in seen[0]
    assert (c.evaluation["s1"], c.evaluation["climax_s"]) == (9, 9)
    w = optimize_boundaries(ctx, c.evaluation["s0"], c.evaluation["s1"], RULES, climax_s=c.evaluation["climax_s"])
    assert w.s1 >= 9


def test_the_rubric_asks_for_the_story_arc():
    item = EVALUATION_SCHEMA["properties"]["evaluations"]["items"]
    assert "climax_sentence" in item["required"]
    assert {"buildup", "ending", "standalone"} <= set(item["properties"]["scores"]["required"])
    for phrase in ("build-up", "CLIMAX/PAYOFF", "Look ahead", "ends_before_payoff", "no_climax", "starts_mid_story"):
        assert phrase in EVALUATOR_SYSTEM


def test_score_punishes_stopping_before_the_payoff_and_rewards_a_complete_story():
    weights = dict.fromkeys(DIMENSIONS, 1.0)
    stages = {"stop": 0.4, "hold": 0.35, "engage": 0.25}
    dims = dict.fromkeys(DIMENSIONS, 70.0)

    def score(d, flags=()):
        return compute_viral_score(d, weights=weights, stage_weights=stages, flags=list(flags)).viral_score

    complete = score(dims)
    assert score(dims, ["ends_before_payoff"]) < score(dims, ["weak_payoff"]) < complete
    assert score(dims, ["no_climax"]) < complete and score(dims, ["starts_mid_story"]) < complete
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
    assert s_full["buildup"] >= s_early["buildup"]
