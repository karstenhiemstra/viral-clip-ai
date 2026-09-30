from app.ai.boundaries import DurationRules, map_to_output_time, optimize_boundaries
from app.ai.signals import VideoContext
from app.ai.transcript import segment_sentences
from tests.conftest import make_words


def ctx_for(lines, **kw):
    words = make_words(lines, **kw)
    sents = segment_sentences(words)
    return VideoContext(words=words, sentences=sents, duration=words[-1].end + 1)


def test_trims_leading_filler_words_and_sentences():
    ctx = ctx_for([
        "Oké.",
        "Nou eh dus niemand gelooft wat hier net gebeurde in de winkel.",
        "Die gast betaalde duizend euro voor een banaan en iedereen keek.",
        "Het was echt het meest absurde wat ik dit jaar heb gezien jongens.",
        "Ik lag helemaal dubbel van het lachen.",
    ])
    w = optimize_boundaries(ctx, 0, 4, DurationRules(min_seconds=8, max_seconds=30, target_seconds=15))
    first = ctx.words[w.w0].text
    assert first == "niemand"
    assert w.s0 == 1


def test_extends_short_moment_to_minimum_duration():
    ctx = ctx_for(["Wacht wat?!"] + [f"Zin nummer {i} met wat extra woorden erbij." for i in range(8)])
    rules = DurationRules(min_seconds=12, max_seconds=18, target_seconds=15)
    w = optimize_boundaries(ctx, 0, 0, rules)
    assert w.source_duration >= 11.5
    assert w.source_duration <= rules.max_seconds + rules.tolerance + 1.5


def test_long_span_is_cut_to_best_subspan_with_hook():
    lines = [f"Saaie zin nummer {i} over niets bijzonders vandaag." for i in range(6)]
    lines += ["Wacht wat?! Dit is echt niet normaal!"] + [f"Vervolg {i} met de uitleg van wat er gebeurde." for i in range(6)]
    ctx = ctx_for(lines)
    hook = 6
    w = optimize_boundaries(ctx, 0, len(ctx.sentences) - 1, DurationRules(min_seconds=10, max_seconds=16, target_seconds=13), hook_s=hook)
    assert w.s0 == hook
    assert 9 <= w.source_duration <= 19 + 1.5


def test_dead_air_is_removed_and_time_mapping_is_monotonic():
    ctx = ctx_for(["Dit is de eerste zin.", "Na een lange stilte komt de tweede zin.", "En dan nog een derde zin erachteraan."], pause=2.0)
    rules = DurationRules(min_seconds=5, max_seconds=30, target_seconds=12, silence_min_gap=0.6)
    w = optimize_boundaries(ctx, 0, 2, rules)
    assert len(w.segments) == 3
    assert w.duration < w.source_duration - 2.5
    times = [map_to_output_time(w.segments, x.start) for x in ctx.words[w.w0 : w.w1]]
    assert times == sorted(times)
    assert times[0] >= 0 and times[-1] <= w.duration


def test_no_silence_removal_keeps_single_segment():
    ctx = ctx_for(["Een.", "Twee drie vier vijf zes.", "Zeven acht negen tien."], pause=1.5)
    w = optimize_boundaries(ctx, 0, 2, DurationRules(min_seconds=3, max_seconds=30, remove_silences=False))
    assert len(w.segments) == 1


def test_end_does_not_bleed_into_next_word():
    ctx = ctx_for(["Eerste zin hier.", "Tweede zin daar.", "Derde zin."], pause=0.2)
    w = optimize_boundaries(ctx, 0, 1, DurationRules(min_seconds=1, max_seconds=30, target_seconds=3))
    nxt = ctx.words[w.w1].start
    assert w.end < nxt


def test_intro_phrases_are_not_trimmed_into_fragments():
    ctx = ctx_for([
        "Vandaag gaan we naar de supermarkt om boodschappen te doen.",
        "Wacht wat, dit is echt niet normaal!",
        "Die gast betaalde duizend euro voor een banaan.",
        "Iedereen in de winkel keek ernaar met open mond.",
    ])
    w = optimize_boundaries(ctx, 0, 3, DurationRules(min_seconds=6, max_seconds=30, target_seconds=10), hook_s=0)
    # the intro sentence is skipped as a whole instead of starting on "naar de supermarkt"
    assert ctx.words[w.w0].text == "Wacht"


def test_context_repair_includes_the_antecedent():
    ctx = ctx_for([
        "We lopen hier gewoon rond in de stad vandaag.",
        "Mijn moeder belde gisteren en ze had groot nieuws.",
        "Ze heeft de loterij gewonnen, een miljoen euro!",
        "Ik schreeuwde zo hard dat de buren kwamen kijken.",
    ])
    w = optimize_boundaries(ctx, 2, 3, DurationRules(min_seconds=5, max_seconds=20, target_seconds=10), hook_s=2)
    assert ctx.words[w.w0].text == "Mijn"


def test_payoff_after_end_is_included():
    import numpy as np

    from app.video.audio_features import compute_profile

    lines = ["Ik moet jullie iets vertellen over gisteren.", "Het was echt heel bizar allemaal.", "En toen ging het alarm keihard af!"]
    ctx = ctx_for(lines)
    sr = 8000
    dur = ctx.duration
    t = np.arange(int(sr * dur)) / sr
    loud_from = ctx.sentences[2].start
    amp = np.where(t >= loud_from, 0.9, 0.1)
    ctx.audio = compute_profile((amp * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)
    w = optimize_boundaries(ctx, 0, 1, DurationRules(min_seconds=3, max_seconds=20, target_seconds=8), hook_s=0)
    assert w.s1 == 2
