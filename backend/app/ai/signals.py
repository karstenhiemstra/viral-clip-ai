"""Deterministic signals for a time window: lexical (NL+EN), hook, audio and crowd features.

They serve three purposes:
1. recall-oriented candidate generation that works without any LLM,
2. a sanity check / blend term next to the LLM scores (LLMs cannot hear the audio),
3. the feature vector the personal learning system trains on.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.transcript import (
    Sentence,
    Word,
    contains_outro,
    leading_filler_count,
    normalize,
    starts_with_context_opener,
)
from app.video.audio_features import AudioProfile, clip_features

LEXICON: dict[str, set[str]] = {
    "question": {"wat", "waarom", "hoe", "wie", "welke", "waar", "wanneer", "serieus", "echt", "what", "why", "how",
                 "who", "really", "seriously", "wtf"},
    "intensity": {"super", "mega", "extreem", "gestoord", "gek", "belachelijk", "insane", "crazy", "wtf", "omg",
                  "jezus", "godver", "shit", "fuck", "fucking", "kut", "tering", "damn", "holy", "wauw", "wow", "vet",
                  "heftig", "ziek", "ziekelijk", "eng", "doodeng", "nooit", "altijd", "iedereen", "niemand",
                  "helemaal", "compleet", "totaal", "echt", "absoluut", "never", "always", "everyone", "nobody",
                  "literally", "letterlijk", "enorm", "gigantisch", "kapot", "sick"},
    "humor": {"haha", "hahaha", "hahah", "hahahaha", "lol", "lmao", "grappig", "gelach", "lacht", "lachen",
              "laughing", "laughter", "laughs", "funny", "hilarisch", "😂", "🤣"},
    "surprise": {"opeens", "ineens", "plotseling", "nee", "huh", "hè", "wacht", "wait", "oh", "ohh", "ohhh", "omg",
                 "whoa", "wow", "echt", "serieus", "what", "shocked", "geschrokken", "schrok", "onverwacht"},
    "controversy": {"haat", "hate", "slecht", "fake", "nep", "liegen", "lieg", "loog", "leugen", "ruzie", "schandalig",
                    "respectloos", "walgelijk", "mening", "vind", "onzin", "bullshit", "overrated", "underrated",
                    "excuses", "cancel", "gecanceld", "boos", "woedend", "kwaad", "angry", "mad", "wrong", "fout",
                    "schaam", "irritant", "dom", "stom", "stupid", "worst", "slechtste", "eerlijk"},
    "story": {"toen", "gebeurde", "verhaal", "vroeger", "ooit", "keer", "bleek", "blijkt", "eigenlijk", "geheim",
              "verteld", "waarheid", "story", "happened", "once", "secret", "truth", "actually", "gisteren",
              "vorige", "plots"},
    "stakes": {"euro", "euros", "€", "duizend", "miljoen", "geld", "betalen", "gratis", "gewonnen", "verloren",
               "winnen", "verliezen", "dood", "ziekenhuis", "politie", "gearresteerd", "boete", "money", "million",
               "thousand", "won", "lost", "police", "hospital", "dollar", "record"},
}
SUPERLATIVE_RE = re.compile(r"^(?:aller)?\w{2,}ste$")
NUMBER_RE = re.compile(r"\d")
STRONG_CATEGORIES = ("intensity", "surprise", "stakes", "humor", "question", "controversy")


@dataclass
class VideoContext:
    words: list[Word]
    sentences: list[Sentence]
    duration: float
    audio: AudioProfile | None = None
    scene_cuts: list[float] = field(default_factory=list)
    hotspots: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    creator: str = ""
    language: str = "nl"

    @property
    def speech_rate_median(self) -> float:
        if not self.words or self.duration <= 0:
            return 2.5
        spoken = sum(max(0.0, s.end - s.start) for s in self.sentences) or self.duration
        return len(self.words) / spoken


def _tokens(words: list[Word]) -> list[str]:
    return [w.norm for w in words if w.norm]


def category_hits(tokens: list[str], text: str = "") -> dict[str, int]:
    hits = {k: 0 for k in LEXICON}
    for t in tokens:
        for cat, vocab in LEXICON.items():
            if t in vocab:
                hits[cat] += 1
    low = text.lower()
    for phrase, cat in (
        ("niet normaal", "intensity"), ("no way", "surprise"), ("niet te geloven", "surprise"),
        ("oh my god", "surprise"), ("eerlijk gezegd", "controversy"), ("onpopulaire mening", "controversy"),
        ("ik zweer", "intensity"), ("wist je dat", "question"), ("raad eens", "question"),
        ("nog nooit", "story"), ("never told", "story"), ("nooit verteld", "story"),
    ):
        if phrase in low:
            hits[cat] += 1
    hits["humor"] += len(re.findall(r"\[(?:gelach|laughter|laughs|lacht)\]|\(lacht\)|\(laughs\)", low))
    return hits


def strong_token(tok: str) -> bool:
    if any(tok in LEXICON[c] for c in STRONG_CATEGORIES):
        return True
    return (bool(SUPERLATIVE_RE.match(tok)) and len(tok) >= 5) or bool(NUMBER_RE.search(tok))


def hook_features(clip_words: list[Word], clip_start: float) -> dict[str, float]:
    """How the first seconds land: filler, context-dependent opener, time to first strong word."""
    if not clip_words:
        return {"starts_filler": 1.0, "context_opener": 0.0, "time_to_strong": 5.0, "first_strength": 0.0,
                "first_question": 0.0}
    lead = leading_filler_count(clip_words)
    first_text = " ".join(w.text for w in clip_words[:12])
    first_tokens = _tokens(clip_words[:10])
    t_strong = 5.0
    for w in clip_words:
        if w.start - clip_start > 5.0:
            break
        if strong_token(w.norm) or "?" in w.text or "!" in w.text:
            t_strong = max(0.0, w.start - clip_start)
            break
    hits = category_hits(first_tokens, first_text)
    strength = min(1.5, 0.35 * sum(hits[c] for c in STRONG_CATEGORIES) + 0.3 * first_text.count("!") + 0.3 * first_text.count("?"))
    return {
        "starts_filler": 1.0 if lead > 0 else 0.0,
        "context_opener": 1.0 if starts_with_context_opener(clip_words[lead].text if lead < len(clip_words) else "") else 0.0,
        "time_to_strong": round(t_strong, 2),
        "first_strength": round(strength, 3),
        "first_question": 1.0 if "?" in first_text or (first_tokens and first_tokens[0] in LEXICON["question"]) else 0.0,
    }


def crowd_strength(hotspots: list[dict[str, Any]], start: float, end: float) -> float:
    best = 0.0
    for h in hotspots or []:
        t = float(h.get("time", -1))
        if start - 6.0 <= t <= end + 3.0:
            best = max(best, float(h.get("strength", 0.0)))
    return best


def window_features(ctx: VideoContext, start: float, end: float, clip_words: list[Word] | None = None) -> dict[str, float]:
    """All deterministic features for [start, end). Values are floats (JSON-able, model-ready)."""
    if clip_words is None:
        clip_words = [w for w in ctx.words if w.end > start + 0.01 and w.start < end - 0.01]
    text = " ".join(w.text for w in clip_words)
    tokens = _tokens(clip_words)
    n = max(1, len(tokens))
    dur = max(0.1, end - start)
    hits = category_hits(tokens, text)
    per10 = {k: min(3.0, v * 10.0 / n) for k, v in hits.items()}
    audio = clip_features(ctx.audio, start, end)
    hook = hook_features(clip_words, start)
    rate = len(tokens) / dur
    rel_rate = rate / max(0.5, ctx.speech_rate_median)
    cuts = sum(1 for c in ctx.scene_cuts if start <= c < end)
    last_words = clip_words[-6:]
    end_text = " ".join(w.text for w in last_words)
    feats: dict[str, float] = {
        "duration": round(dur, 2),
        "words": float(len(tokens)),
        "speech_rate": round(rate, 3),
        "rel_speech_rate": round(rel_rate, 3),
        "questions": float(text.count("?")),
        "exclamations": float(text.count("!")),
        "has_number": 1.0 if NUMBER_RE.search(text) else 0.0,
        "superlatives": float(sum(1 for t in tokens if SUPERLATIVE_RE.match(t) and len(t) >= 5)),
        "outro": 1.0 if contains_outro(text) else 0.0,
        "crowd": round(crowd_strength(ctx.hotspots, start, end), 3),
        "scene_cuts_per_10s": round(cuts * 10.0 / dur, 3),
        "ends_complete": 1.0 if re.search(r"[.!?…]$", end_text.strip()) else 0.0,
        "humor_end": 1.0 if category_hits(_tokens(last_words), end_text)["humor"] else 0.0,
    }
    for k, v in per10.items():
        feats[f"lex_{k}"] = round(v, 3)
    for k, v in audio.items():
        feats[f"audio_{k}"] = round(float(v), 3)
    for k, v in hook.items():
        feats[f"hook_{k}"] = float(v)
    return feats


def _c(x: float) -> float:
    return max(0.0, min(100.0, x))


def heuristic_dimension_scores(f: dict[str, float]) -> dict[str, float]:
    """Rule-based estimates of the 12 dimensions. Used when no LLM is configured and as a fallback."""
    audio = f.get("audio_available", 0.0) > 0
    peak = max(0.0, min(3.5, f.get("audio_peak_z", 0.0))) if audio else 0.0
    hook_z = max(-1.5, min(2.5, f.get("audio_hook_z", 0.0))) if audio else 0.0
    crowd = f.get("crowd", 0.0)
    humor = f.get("lex_humor", 0.0)
    rel_rate = f.get("rel_speech_rate", 1.0)
    silence = f.get("audio_silence_ratio", 0.0)

    hook = 42 + 18 * f.get("hook_first_strength", 0) + 7 * hook_z + 10 * f.get("hook_first_question", 0)
    hook -= 22 * f.get("hook_starts_filler", 0) + 14 * f.get("hook_context_opener", 0)
    hook_strength = 100 - 14 * f.get("hook_time_to_strong", 5.0)
    curiosity = 35 + 10 * min(2, f.get("questions", 0)) + 9 * f.get("lex_story", 0) + 8 * f.get("lex_question", 0)
    curiosity += 6 * f.get("has_number", 0) + 5 * min(2, f.get("superlatives", 0))
    emotion = 30 + 11 * peak + 1.2 * f.get("audio_std_db", 0) + 12 * f.get("lex_intensity", 0) + 5 * min(3, f.get("exclamations", 0))
    surprise = 30 + 16 * f.get("lex_surprise", 0) + (10 if peak > 2.3 else 0) + 6 * f.get("lex_stakes", 0)
    humor_s = 22 + 28 * humor + 6 * f.get("audio_burst_count", 0) + (10 if f.get("humor_end") else 0)
    comment = 30 + 16 * f.get("lex_controversy", 0) + 6 * min(2, f.get("questions", 0)) + 25 * crowd
    retention = 48 + 12 * min(1.3, rel_rate) - 70 * silence + 10 * (1 if f.get("audio_peak_pos", 0) > 0.45 else 0)
    retention -= 25 * f.get("outro", 0)
    context = 78 - 22 * f.get("hook_context_opener", 0) - 8 * f.get("hook_starts_filler", 0) + 6 * f.get("ends_complete", 0)
    payoff = 38 + 16 * (1 if f.get("audio_peak_pos", 0) > 0.5 else 0) + 8 * max(0, f.get("audio_end_z", 0))
    payoff += 12 * f.get("humor_end", 0) + 10 * f.get("ends_complete", 0)
    share = 0.35 * humor_s + 0.3 * surprise + 0.2 * emotion + 0.15 * curiosity + 22 * crowd
    rewatch = 0.45 * humor_s + 0.25 * surprise + 8 * min(1.4, rel_rate) + 10 * crowd
    scores = {
        "hook": hook,
        "hook_strength": hook_strength,
        "curiosity": curiosity,
        "emotion": emotion,
        "surprise": surprise,
        "humor": humor_s,
        "shareability": share,
        "comment_potential": comment,
        "retention": retention,
        "context": context,
        "payoff": payoff,
        "rewatch": rewatch,
    }
    return {k: round(_c(v), 1) for k, v in scores.items()}


def signal_score(f: dict[str, float]) -> float:
    """One number (0-100) for 'the raw signals say something is happening here'."""
    audio = f.get("audio_available", 0.0) > 0
    lex = (
        f.get("lex_intensity", 0) + f.get("lex_surprise", 0) + f.get("lex_humor", 0) * 1.5
        + f.get("lex_controversy", 0) + f.get("lex_story", 0) * 0.6 + f.get("lex_stakes", 0) * 0.8
    )
    s = 28.0
    s += 11 * min(1.8, lex)
    s += 5 * min(3, f.get("questions", 0) + f.get("exclamations", 0)) / 3 * 2
    s += 22 * f.get("crowd", 0)
    s += 8 * f.get("hook_first_strength", 0) - 8 * f.get("hook_starts_filler", 0) - 6 * f.get("hook_context_opener", 0)
    if audio:
        s += 7 * max(0.0, min(3.0, f.get("audio_peak_z", 0))) + 4 * max(-1.0, min(2.0, f.get("audio_hook_z", 0)))
        s -= 30 * f.get("audio_silence_ratio", 0)
    s -= 25 * f.get("outro", 0)
    return round(_c(s), 1)


def feature_vector(f: dict[str, float], scores: dict[str, float] | None = None) -> dict[str, float]:
    """Stable, bounded subset used for learning (prefix d_ = dimension scores /100)."""
    keys = (
        "duration", "rel_speech_rate", "questions", "exclamations", "has_number", "crowd", "scene_cuts_per_10s",
        "ends_complete", "humor_end", "lex_question", "lex_intensity", "lex_humor", "lex_surprise",
        "lex_controversy", "lex_story", "lex_stakes", "audio_peak_z", "audio_hook_z", "audio_silence_ratio",
        "audio_peak_pos", "hook_starts_filler", "hook_context_opener", "hook_time_to_strong", "hook_first_strength",
        "hook_first_question",
    )
    vec = {k: float(f.get(k, 0.0)) for k in keys}
    for k, v in (scores or {}).items():
        vec[f"d_{k}"] = float(v) / 100.0
    return {k: (0.0 if (math.isnan(v) or math.isinf(v)) else round(v, 4)) for k, v in vec.items()}


def normalize_text_for_similarity(text: str) -> list[str]:
    return [t for t in (normalize(x) for x in text.split()) if len(t) > 2]
