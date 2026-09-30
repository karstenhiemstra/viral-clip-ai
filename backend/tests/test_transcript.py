from app.ai.transcript import (
    Word,
    interpolate_words,
    is_filler_sentence,
    leading_filler_count,
    segment_sentences,
    starts_with_context_opener,
    words_from_rows,
)
from app.ai.transcription import parse_subtitles
from tests.conftest import make_words

SRT = """1
00:00:01,000 --> 00:00:03,500
Hallo allemaal!

2
00:00:04,000 --> 00:00:07,000
Wacht wat, dit is
echt niet normaal.
"""

VTT_ROLLING = """WEBVTT
Kind: captions
Language: nl

00:00:01.000 --> 00:00:03.000
eerste regel hier

00:00:03.000 --> 00:00:05.000
eerste regel hier
tweede regel daar
"""

VTT_YOUTUBE = """WEBVTT
Kind: captions
Language: nl

00:00:00.160 --> 00:00:02.310 align:start position:0%
 
wacht<00:00:00.560><c> wat</c><00:00:00.880><c> gebeurt</c><00:00:01.360><c> hier</c>

00:00:02.310 --> 00:00:02.320 align:start position:0%
wacht wat gebeurt hier
 

00:00:02.320 --> 00:00:04.990 align:start position:0%
wacht wat gebeurt hier
dit<00:00:02.800><c> is</c><00:00:03.040><c> gestoord</c>
"""


def test_segment_sentences_splits_on_punctuation_and_pauses():
    words = make_words(["Dit is zin een.", "En dit is zin twee zonder punt", "Derde zin?"])
    sents = segment_sentences(words)
    assert [s.text for s in sents] == ["Dit is zin een.", "En dit is zin twee zonder punt", "Derde zin?"]
    assert all(s.start < s.end for s in sents)
    assert sents[1].w0 == 4


def test_segment_sentences_caps_long_runs():
    words = [Word(i * 0.3, i * 0.3 + 0.25, f"woord{i}") for i in range(120)]
    sents = segment_sentences(words, max_duration=11.0, max_words=32)
    assert len(sents) >= 4
    assert all(s.w1 - s.w0 <= 32 for s in sents)


def test_leading_filler_and_openers():
    words = make_words(["Nou eh dus dit is het ergste ooit"])
    assert leading_filler_count(words) == 3
    assert leading_filler_count(make_words(["Zoals ik al zei was het raar"])) == 4
    assert leading_filler_count(make_words(["Wacht wat gebeurt hier"])) == 0
    assert starts_with_context_opener("Hij zei toen dat...")
    assert not starts_with_context_opener("Niemand gelooft dit")
    sents = segment_sentences(make_words(["Oké.", "Dit is echt belangrijk."]))
    assert is_filler_sentence(sents[0], [w for w in make_words(["Oké.", "Dit is echt belangrijk."])])


def test_words_from_rows_repairs_bad_timings():
    words = words_from_rows([[1.0, 1.0, "a"], [0.5, 0.9, "b"], [2.0, 2.4, ""]])
    assert [w.text for w in words] == ["b", "a"]
    assert all(w.end > w.start for w in words)


def test_parse_srt():
    words, lang = parse_subtitles(SRT, "x.srt")
    assert [w.text for w in words][:2] == ["Hallo", "allemaal!"]
    assert words[0].start == 1.0
    assert words[-1].text == "normaal."
    assert words[-1].end <= 7.0


def test_parse_vtt_removes_rolling_duplicates():
    words, lang = parse_subtitles(VTT_ROLLING, "x.vtt")
    assert lang == "nl"
    assert " ".join(w.text for w in words) == "eerste regel hier tweede regel daar"


def test_parse_youtube_vtt_uses_inline_word_timings():
    words, _ = parse_subtitles(VTT_YOUTUBE, "x.vtt")
    assert [w.text for w in words] == ["wacht", "wat", "gebeurt", "hier", "dit", "is", "gestoord"]
    assert abs(words[1].start - 0.56) < 1e-6
    assert abs(words[-1].start - 3.04) < 1e-6


def test_interpolate_words_spreads_over_cue():
    words = interpolate_words([(10.0, 12.0, "een twee drie")])
    assert words[0].start == 10.0 and words[-1].end <= 12.0
    assert words[0].start < words[1].start < words[2].start


def test_intro_detection():
    from app.ai.transcript import is_intro_text

    assert is_intro_text("Vandaag gaan we naar Parijs")
    assert is_intro_text("Welkom terug op mijn kanaal")
    assert not is_intro_text("Dit is het ergste wat ik ooit zag")
