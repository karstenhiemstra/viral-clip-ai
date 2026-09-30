"""Word-level transcript model + sentence segmentation + filler/opener detection (NL + EN)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_PUNCT_END = re.compile(r"[.!?…]+[\"'”’)]*$")
_STRIP = re.compile(r"[^\w€$%']+", re.UNICODE)


@dataclass
class Word:
    start: float
    end: float
    text: str

    @property
    def norm(self) -> str:
        return normalize(self.text)

    def to_row(self) -> list[Any]:
        return [round(self.start, 3), round(self.end, 3), self.text]


@dataclass
class Sentence:
    idx: int
    start: float
    end: float
    w0: int  # first word index (inclusive)
    w1: int  # last word index (exclusive)
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().strip()
    return _STRIP.sub("", text)


def words_from_rows(rows: list[list[Any]]) -> list[Word]:
    out = []
    for r in rows or []:
        if len(r) >= 3 and str(r[2]).strip():
            out.append(Word(float(r[0]), float(r[1]), str(r[2]).strip()))
    out.sort(key=lambda w: w.start)
    # Repair overlapping/zero-length timings (common after chunked transcription).
    for i, w in enumerate(out):
        if w.end <= w.start:
            nxt = out[i + 1].start if i + 1 < len(out) else w.start + 0.3
            w.end = max(w.start + 0.05, min(nxt, w.start + 0.6))
    return out


def interpolate_words(cues: list[tuple[float, float, str]]) -> list[Word]:
    """Spread cue text over its time span proportional to word length (for SRT/VTT without word timing)."""
    words: list[Word] = []
    for start, end, text in cues:
        tokens = [t for t in re.split(r"\s+", text.strip()) if t]
        if not tokens:
            continue
        total = sum(len(t) + 1 for t in tokens)
        span = max(0.05, end - start)
        t = start
        for tok in tokens:
            d = span * (len(tok) + 1) / total
            words.append(Word(t, t + d * 0.92, tok))
            t += d
    return words


def segment_sentences(
    words: list[Word], *, pause_split: float = 0.7, max_duration: float = 11.0, max_words: int = 32
) -> list[Sentence]:
    """Split words into sentence-like units using punctuation, pauses and length limits.

    Units are the atoms the LLM reasons about ("start at sentence 41"): short enough for precise
    boundaries, long enough to carry meaning.
    """
    sentences: list[Sentence] = []
    if not words:
        return sentences
    start_i = 0
    for i, w in enumerate(words):
        is_last = i == len(words) - 1
        gap = (words[i + 1].start - w.end) if not is_last else 0.0
        n_words = i - start_i + 1
        dur = w.end - words[start_i].start
        punct = bool(_PUNCT_END.search(w.text))
        soft = w.text.endswith((",", ";", ":", "—", "-"))
        split = (
            is_last
            or (punct and n_words >= 2)
            or gap >= pause_split
            or n_words >= max_words
            or dur >= max_duration
            or (soft and dur >= max_duration * 0.6)
        )
        if split:
            chunk = words[start_i : i + 1]
            sentences.append(
                Sentence(
                    idx=len(sentences),
                    start=chunk[0].start,
                    end=chunk[-1].end,
                    w0=start_i,
                    w1=i + 1,
                    text=" ".join(x.text for x in chunk),
                )
            )
            start_i = i + 1
    return sentences


def words_between(words: list[Word], start: float, end: float) -> list[Word]:
    return [w for w in words if w.end > start + 0.01 and w.start < end - 0.01]


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --- filler & opener lexicon ----------------------------------------------------------------------

FILLER_WORDS = {
    # nl
    "eh", "ehm", "uh", "uhm", "euh", "uhh", "hm", "hmm", "nou", "dus", "oké", "oke", "ok", "okay", "ja", "jah",
    "goed", "zo", "even", "effe", "ofzo", "enzo", "gewoon", "weetjewel", "jongens", "allemaal", "yo",
    # en
    "um", "umm", "er", "erm", "so", "like", "well", "anyway", "anyways", "alright", "right", "yeah", "guys",
}
# Multi-word openers that kill a hook (checked on normalized, space-joined text).
FILLER_PHRASES = (
    "zoals ik al zei", "zoals gezegd", "zoals ik zei", "waar was ik", "even kijken", "laten we", "vandaag gaan we",
    "in deze video", "welkom bij", "welkom terug", "hallo allemaal", "hoi allemaal", "hey allemaal", "hallo jongens",
    "hoi jongens", "what's up", "whats up", "as i said", "like i said", "in this video", "welcome back",
    "today we're", "today we are", "hey guys", "hi guys", "let's go", "lets go", "anyway so",
)
_FILLER_PHRASE_TOKENS = sorted(
    ([normalize(t) for t in p.split()] for p in FILLER_PHRASES), key=len, reverse=True
)
# Sentence openers that usually point back at something the viewer has not seen.
CONTEXT_OPENERS = {
    "hij", "zij", "ze", "die", "dat", "daarom", "daardoor", "daarna", "toen", "want", "maar", "en", "ook", "dan",
    "hem", "haar", "hun", "daar", "dus",
    "he", "she", "they", "it", "that", "those", "then", "because", "but", "and", "also", "so",
}
GREETING_OUTRO = (
    "abonneer", "abonneren", "like en abonneer", "bel-icoon", "belletje", "tot de volgende", "doei", "tot morgen",
    "link in de beschrijving", "kortingscode", "gesponsord", "sponsor", "merch", "subscribe", "link in the description",
    "see you next time", "discount code", "sponsored", "thanks for watching", "bedankt voor het kijken",
)


def leading_filler_count(words: list[Word], max_scan: int = 6) -> int:
    """Number of leading words that are filler ("Nou, eh, dus ...", "Zoals ik al zei ...")."""
    norms = [w.norm for w in words]
    i = 0
    limit = min(len(words), max_scan)
    while i < limit:
        for phrase in _FILLER_PHRASE_TOKENS:
            if norms[i : i + len(phrase)] == phrase:
                i += len(phrase)
                break
        else:
            if norms[i] in FILLER_WORDS:
                i += 1
                continue
            break
    return min(i, len(words))


def starts_with_context_opener(text: str) -> bool:
    first = normalize(text.split(" ", 1)[0]) if text else ""
    return first in CONTEXT_OPENERS


def is_filler_sentence(s: Sentence, words: list[Word]) -> bool:
    ws = words[s.w0 : s.w1]
    if not ws:
        return True
    lead = leading_filler_count(ws, max_scan=len(ws))
    return lead >= len(ws) or (len(ws) <= 2 and all(w.norm in FILLER_WORDS for w in ws))


def contains_outro(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in GREETING_OUTRO)
