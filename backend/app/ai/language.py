"""Which language is spoken - so captions stay in that language.

Captions are a TRANSCRIPTION of what is said, never a translation. Speech-to-text runs without a forced
language (the interface language, the creator's language setting or the user's language never decide what
the captions say); the transcriber detects the spoken language itself, per short piece of audio, so mixed
speech keeps its own language per sentence.

This module double-checks those detections with the words that came out (frequent function words per
language), gives the dominant language with a confidence, and the metadata stored with the transcript:

    {"detected_language": "nl", "caption_language": "nl", "translation_applied": false, "confidence": 0.98,
     "languages": {"nl": 1.0}, "mixed": false, ...}

``TRANSLATE_CAPTIONS`` (default false) is the explicit switch: translation is not offered at all, so captions
always stay in the spoken language.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Frequent, short function words per language. A word that belongs to several languages counts for each of
# them in proportion (e.g. "de" is Dutch, Spanish, Portuguese and French).
_STOPWORDS: dict[str, set[str]] = {
    "nl": set("de het een en is van ik je dat niet wat op te met zijn maar ook er die nog wel dan naar hij we ze "
              "heb heeft voor als gaan gaat echt jij mijn dit hebben kan om uit zo nou jongens toch even dus "
              "hier daar waar want omdat zeg zegt ben bent was waren wordt geen veel heel".split()),
    "en": set("the and is you that it of to in what this was i we they have are with for not just so my but be "
              "do going like really there about would can all know don't it's i'm he she his her them your "
              "from at our been were will then here when why how".split()),
    "de": set("der die das und ist ich nicht du es wir sie ein eine zu mit auf für sich auch aber was wie noch so "
              "mal jetzt schon dann haben wird kann mein bin sind dem den des nur hier oder wenn weil gibt "
              "sehr ganz doch".split()),
    "fr": set("le la les et est je tu il elle nous vous un une des pas que qui ce c'est dans pour avec sur mais "
              "très oui non moi on ça du au aux suis sont était fait comme tout bien alors parce".split()),
    "es": set("el la los las y es que de en no un una por con para lo pero muy más yo tú está como qué esto eso "
              "sí porque también cuando mi del al hay ser son tengo estoy aquí ahora pues".split()),
    "it": set("il la che e è di non un una per con sono ma mi ti ci questo quello cosa come perché anche molto "
              "io tu lui lei noi sei ha ho della nel del gli le lo una allora sempre ancora".split()),
    "pt": set("o a os as e é que de não um uma com para por mas muito eu você isso isto está são também mais "
              "como ele ela nós meu minha do da no na dos das então agora aqui".split()),
}
_OWNERS: dict[str, list[str]] = {}
for _lang, _words in _STOPWORDS.items():
    for _w in _words:
        _OWNERS.setdefault(_w, []).append(_lang)

LANGUAGE_NAMES = {"nl": "Nederlands", "en": "Engels", "de": "Duits", "fr": "Frans", "es": "Spaans",
                  "it": "Italiaans", "pt": "Portugees"}
_WHISPER_NAMES = {"dutch": "nl", "flemish": "nl", "english": "en", "german": "de", "french": "fr",
                  "spanish": "es", "castilian": "es", "italian": "it", "portuguese": "pt"}
_TOKEN = re.compile(r"[a-zà-ÿ']+")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
MIN_HITS = 3  # fewer known words than this: the text alone cannot tell the language
UNCERTAIN = 0.6  # below this a piece of audio gets a second, closer look


def language_code(name: str | None) -> str | None:
    """Whisper names ('dutch') or codes ('nl', 'en-US') -> 'nl' / 'en'."""
    if not name:
        return None
    name = name.strip().lower()
    return _WHISPER_NAMES.get(name) or (name[:2] if len(name) >= 2 and name[:2].isalpha() else None)


def text_language(text: str) -> tuple[str | None, float, int]:
    """(language, confidence 0..1, number of recognised words) from the words of a text."""
    scores: dict[str, float] = {}
    hits = 0
    for tok in _TOKEN.findall(text.lower().replace("’", "'")):
        owners = _OWNERS.get(tok)
        if not owners:
            continue
        hits += 1
        for lang in owners:
            scores[lang] = scores.get(lang, 0.0) + 1.0 / len(owners)
    if hits < MIN_HITS or not scores:
        return None, 0.0, hits
    best = max(scores, key=scores.get)
    return best, round(scores[best] / sum(scores.values()), 3), hits


def sentence_languages(text: str) -> list[tuple[str | None, int]]:
    """Language per sentence: [(language or None, number of words)]."""
    out = []
    for sentence in _SENTENCE_END.split(text.strip()):
        n = len(sentence.split())
        if n:
            lang, conf, _ = text_language(sentence)
            out.append((lang if conf >= 0.5 else None, n))
    return out


@dataclass
class Piece:
    """One piece of transcribed audio: what the speech-to-text heard (``audio_language``, optional
    probability) and the words it wrote."""

    duration: float
    audio_language: str | None
    text: str
    audio_probability: float | None = None

    def certainty(self) -> tuple[str | None, float]:
        """(language of this piece, how sure 0..1). The written words win when they clearly are another
        language than the audio label: they are what the captions will show."""
        audio = language_code(self.audio_language)
        p_audio = self.audio_probability if self.audio_probability is not None else (0.85 if audio else 0.0)
        text, p_text, hits = text_language(self.text)
        if not self.text.strip():
            return audio, 1.0  # nothing said: nothing to get wrong
        if text is None:
            return audio, p_audio
        if audio is None:
            return text, p_text
        if audio == text:
            return audio, round(1 - (1 - p_audio) * (1 - p_text), 3)
        if p_text >= 0.75 and hits >= 5:
            return text, 0.5  # the words are clearly `text`: trust them, but this piece is not certain
        return audio, 0.3


def language_report(pieces: list[Piece], method: str, translate_requested: bool = False) -> dict:
    """Dominant spoken language, confidence and the share per language (by words) over all pieces."""
    shares: dict[str, float] = {}
    weighted, total = 0.0, 0
    for p in pieces:
        n = len(p.text.split())
        if not n:
            continue
        lang, certainty = p.certainty()
        weighted += certainty * n
        total += n
        per_sentence = [(sl or lang, k) for sl, k in sentence_languages(p.text)]
        for sl, k in per_sentence:
            if sl:
                shares[sl] = shares.get(sl, 0.0) + k
    total_shares = sum(shares.values()) or 1.0
    languages = {k: round(v / total_shares, 3) for k, v in sorted(shares.items(), key=lambda kv: -kv[1])}
    dominant = next(iter(languages), None)
    confidence = round(weighted / total, 2) if total else 0.0
    report = {
        "detected_language": dominant,
        "caption_language": dominant,  # = the spoken language; the captions are never translated
        "translation_applied": False,
        "confidence": confidence,
        "languages": languages,
        "mixed": sum(1 for v in languages.values() if v >= 0.1) > 1,
        "uncertain": confidence < UNCERTAIN,
        "method": method,
    }
    if translate_requested:
        report["translation_requested"] = True
        log.warning("TRANSLATE_CAPTIONS staat aan, maar vertalen wordt niet ondersteund: de captions blijven "
                    "in de gesproken taal")
    return report


def translate_captions_requested() -> bool:
    from app.config import get_settings

    return bool(get_settings().translate_captions)
