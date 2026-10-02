"""Captions stay in the spoken language: detection, mixed languages, no forced language, no translation."""

from types import SimpleNamespace

import pytest

from app.ai.language import Piece, language_report, sentence_languages, text_language
from app.ai.transcript import Word
from app.ai.transcription import OpenAITranscriber, parse_subtitles
from app.config import get_settings

SAMPLES = {
    "nl": "We gaan vandaag naar Amsterdam en ik heb er echt zin in, dat is niet normaal jongens.",
    "en": "We're going to Amsterdam today and I really can't wait, this is just crazy you know.",
    "de": "Wir fahren heute nach Amsterdam und ich freue mich, das ist nicht normal, aber auch schön.",
    "fr": "Nous allons à Amsterdam aujourd'hui et je suis très content, c'est pas normal mais ça va.",
    "es": "Hoy vamos a Ámsterdam y estoy muy contento, no es normal pero es lo que hay.",
    "it": "Oggi andiamo ad Amsterdam e sono molto contento, non è normale ma questo è il bello.",
}


@pytest.mark.parametrize("lang", SAMPLES)
def test_language_of_the_words(lang):
    detected, confidence, _ = text_language(SAMPLES[lang])
    assert detected == lang and confidence >= 0.5


def test_mixed_speech_keeps_each_language():
    text = "Vandaag gaan we naar Amsterdam met de hele groep. And then we're going home with all of them."
    assert [lang for lang, _ in sentence_languages(text)] == ["nl", "en"]
    report = language_report([Piece(8.0, "dutch", text)], "test")
    assert report["mixed"] is True and set(report["languages"]) == {"nl", "en"}
    assert report["translation_applied"] is False


def test_report_metadata_and_confidence():
    sure = language_report([Piece(20.0, "dutch", SAMPLES["nl"]), Piece(20.0, "nl", SAMPLES["nl"])], "test")
    assert sure["detected_language"] == sure["caption_language"] == "nl"
    assert sure["translation_applied"] is False and sure["confidence"] >= 0.95 and not sure["uncertain"]
    # the audio label says English but the words are clearly Dutch: the words win, but it is not certain
    unsure = language_report([Piece(20.0, "english", SAMPLES["nl"])], "test")
    assert unsure["caption_language"] == "nl" and unsure["confidence"] < 0.6 and unsure["uncertain"]


def test_translation_is_off_by_default():
    assert get_settings().translate_captions is False


def _resp(language: str, text: str, start: float = 0.0) -> SimpleNamespace:
    words = [SimpleNamespace(word=w, start=start + i * 0.4, end=start + i * 0.4 + 0.3) for i, w in enumerate(text.split())]
    return SimpleNamespace(language=language, words=words, segments=[SimpleNamespace(text=text, end=words[-1].end)])


def test_pieces_are_short_and_cut_in_pauses():
    from app.ai.transcription import AudioLevels, plan_pieces

    levels = AudioLevels.__new__(AudioLevels)
    import numpy as np

    levels.db = np.full(int(70 / AudioLevels.HOP), -20.0)
    levels.db[int(24.0 / AudioLevels.HOP)] = -60.0  # a pause at 24 s
    pieces = plan_pieces(levels, 70.0)
    assert pieces[0] == (0.0, 24.0) and all(b - a <= 29.0 for a, b in pieces) and pieces[-1][1] == 70.0


def test_whisper_never_gets_a_language_and_each_piece_keeps_its_own(db, test_video, monkeypatch):
    calls: list[dict] = []
    answers = iter([
        _resp("dutch", "We gaan vandaag naar Amsterdam en het is echt mooi daar"),
        _resp("english", "We gaan vandaag naar Amsterdam en het is echt mooi daar"),  # unsure: label vs words
        _resp("dutch", "We gaan vandaag naar"), _resp("dutch", "Amsterdam en het is echt mooi daar"),
    ])
    stt = OpenAITranscriber("sk-test")

    def create(**kwargs):
        calls.append({k: v for k, v in kwargs.items() if k != "file"})
        return next(answers)

    monkeypatch.setattr(stt.client.audio.transcriptions, "create", create)
    monkeypatch.setattr(stt.client.audio.translations, "create", lambda **k: pytest.fail("never translate"))
    monkeypatch.setattr("app.ai.transcription.PARALLEL_REQUESTS", 1)  # keep the fake answers in order
    monkeypatch.setattr("app.ai.transcription.plan_pieces", lambda levels, d: [(0.0, 15.0), (15.0, 30.0)])
    words, report = stt.transcribe(test_video, 30.0)

    assert all("language" not in c and "prompt" not in c for c in calls)  # never forced, never steered
    assert len(calls) == 4  # 2 pieces of <= 30 s + the unsure one again as two halves
    assert report["pieces"] == 2 and report["rechecked_pieces"] == 1
    assert report["caption_language"] == "nl" and report["translation_applied"] is False
    assert " ".join(w.text for w in words).startswith("We gaan vandaag naar Amsterdam")  # as said
    assert all(a.start <= b.start for a, b in zip(words, words[1:], strict=False))


def test_transcript_language_is_the_spoken_one_not_the_creator_or_interface(client, db, test_video, monkeypatch):
    from app.ai.pipeline import ensure_transcript
    from app.models import Creator, Video
    from app.services.settings_store import load_settings
    from app.video.captions import auto_cues
    from app.video.render import output_words

    seen: dict = {}

    class EnglishSpeaker:
        name = "fake"

        def transcribe(self, media, duration):
            seen["args"] = (media, duration)
            text = "We're going to Amsterdam today and I really can't wait."
            words = [Word(i * 0.4, i * 0.4 + 0.3, w) for i, w in enumerate(text.split())]
            return words, language_report([Piece(duration, "english", text)], "fake")

    monkeypatch.setattr("app.ai.pipeline.get_transcriber", lambda db, rs: EnglishSpeaker())
    vid = client.post("/api/videos/upload", files={"file": ("v.mp4", test_video.open("rb"), "video/mp4")}).json()["id"]
    video = db.get(Video, vid)
    creator = Creator(name="NL creator", youtube_channel_id="UC_nl", language="nl")
    rs = load_settings(db)
    rs.ai.output_language = "nl"  # Dutch interface
    words = ensure_transcript(db, video, creator, rs, lambda *a, **k: None)

    assert len(seen["args"]) == 2  # media + duration: no language handed to the speech-to-text
    db.refresh(video)
    assert video.transcript.language == "en"
    assert video.transcript.language_info["translation_applied"] is False
    api = client.get(f"/api/videos/{vid}").json()["caption_language"]
    assert api["caption_language"] == "en" and api["translation_applied"] is False
    cues = auto_cues(output_words([(w.start, w.end, w.text) for w in words], [(0.0, 10.0)]), "capcut")
    assert " ".join(c["text"] for c in cues) == "We're going to Amsterdam today and I really can't wait."


def test_uploaded_subtitles_keep_their_language():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nWe're going to Amsterdam today and I really can't wait.\n"
    words, header = parse_subtitles(srt)
    from app.services.media import subtitle_language

    lang, info = subtitle_language(" ".join(w.text for w in words), header, fallback="nl")
    assert lang == "en" and info["caption_language"] == "en" and info["translation_applied"] is False
