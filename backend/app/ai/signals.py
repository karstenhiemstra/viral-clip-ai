"""Deterministic signals for a time window: lexical (NL+EN), hook, audio and crowd features.

They serve three purposes:
1. recall-oriented candidate generation that works without any LLM,
2. a sanity check / blend term next to the LLM scores (LLMs cannot hear the audio),
3. the feature vector the personal learning system trains on.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.transcript import (
    Sentence,
    Word,
    contains_outro,
    is_intro_text,
    leading_filler_count,
    normalize,
    starts_with_context_opener,
)
from app.video.audio_features import AudioProfile, clip_features, peak_after

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
    "teaser": {"nieuws", "geheim", "onthulling", "bekentenis", "opbiechten", "waarheid", "news", "secret",
               "confession", "reveal", "eindelijk", "finally"},
    "stakes": {"euro", "euros", "€", "duizend", "miljoen", "geld", "betalen", "gratis", "gewonnen", "verloren",
               "winnen", "verliezen", "dood", "ziekenhuis", "politie", "gearresteerd", "boete", "money", "million",
               "thousand", "won", "lost", "police", "hospital", "dollar", "record"},
}
SUPERLATIVE_RE = re.compile(r"^(?:aller)?\w{2,}ste$")
NUMBER_RE = re.compile(r"\d")
STRONG_CATEGORIES = ("intensity", "surprise", "stakes", "humor", "question", "controversy", "teaser")


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
    # topic_breaks[i] = 0..1 strength of a topic change between sentence i and i+1 (see topic_breaks()).
    topic_breaks: list[float] = field(default_factory=list)

    def break_between(self, a: int, b: int) -> float:
        """Strongest topic change strictly inside sentences a..b (0 when the span is one topic)."""
        if b <= a or not self.topic_breaks:
            return 0.0
        return max(self.topic_breaks[max(0, a) : min(b, len(self.topic_breaks))] or [0.0])

    @property
    def speech_rate_median(self) -> float:
        if not self.words or self.duration <= 0:
            return 2.5
        spoken = sum(max(0.0, s.end - s.start) for s in self.sentences) or self.duration
        return len(self.words) / spoken


# Sentence openers that usually start a NEW segment of a vlog ("Oké, we gaan nu...", "Daarna...").
TRANSITION_OPENERS = (
    "oké", "oke", "ok", "okay", "oké dus", "goed", "zo", "daarna", "vervolgens", "later", "inmiddels", "thuis",
    "intussen", "ondertussen", "nu gaan we", "we gaan nu", "dan gaan we", "en dan gaan we", "volgende",
    "anyway", "alright", "after that", "later that", "next", "so now", "now we",
)
# Openers that announce a NEW story or reveal ("Ik moet jullie iets vertellen...", "Raad eens...").
STORY_OPENERS = (
    "ik moet jullie", "moet ik jullie", "ik heb nieuws", "groot nieuws", "raad eens", "wist je dat", "wisten jullie",
    "weet je nog", "gisteren", "vorige week", "vorig jaar", "laatst", "het verhaal", "ik ga jullie",
    "guess what", "let me tell you", "i have to tell you", "i need to tell you", "yesterday", "last week",
)
_TRANSITION_TOKENS = sorted(([normalize(t) for t in p.split()] for p in TRANSITION_OPENERS), key=len, reverse=True)
_STORY_TOKENS = sorted(([normalize(t) for t in p.split()] for p in STORY_OPENERS), key=len, reverse=True)


def _starts_with(text: str, phrases: list[list[str]]) -> bool:
    toks = [normalize(t) for t in text.split()[:5]]
    return any(toks[: len(p)] == p for p in phrases)


def starts_with_transition(text: str) -> bool:
    return _starts_with(text, _TRANSITION_TOKENS)


def starts_new_story(text: str) -> bool:
    return _starts_with(text, _STORY_TOKENS)


def topic_breaks(sentences: list[Sentence], scene_cuts: list[float] | None = None) -> list[float]:
    """How strongly the topic changes between consecutive sentences (0..1), from free signals:
    an unusually long pause, a camera cut in that pause, a transition opener ("Oké, we gaan nu...",
    "Daarna...", "Thuis..."), and intro/outro/sponsor material on one side only.

    Used to keep clips inside one story: a clip that starts on a punchline and then drifts into
    "we hebben melk, brood en kaas nodig" is exactly what a viewer scrolls away from."""
    n = len(sentences)
    if n < 2:
        return []
    gaps = [max(0.0, sentences[i + 1].start - sentences[i].end) for i in range(n - 1)]
    typical = max(0.25, sorted(gaps)[len(gaps) // 2])
    cuts = sorted(scene_cuts or [])
    out: list[float] = []
    for i in range(n - 1):
        cur, nxt = sentences[i], sentences[i + 1]
        strength = 0.0
        ratio = gaps[i] / typical  # pauses are judged relative to this speaker's normal rhythm
        # No single signal makes a topic change (vloggers pause and jump-cut inside stories too);
        # it takes two of: a long pause, a camera cut, a transition/story opener, promo on one side.
        if ratio >= 3.0 and gaps[i] >= 1.0:
            strength += 0.4
        elif ratio >= 2.2 and gaps[i] >= 0.75:
            strength += 0.3
        if any(cur.end - 0.35 <= c <= nxt.start + 0.35 for c in cuts):
            strength += 0.3
        if starts_with_transition(nxt.text):
            strength += 0.4
        elif starts_new_story(nxt.text):
            strength += 0.35
        promo_cur = contains_outro(cur.text) or is_intro_text(cur.text)
        promo_nxt = contains_outro(nxt.text) or is_intro_text(nxt.text)
        if promo_cur != promo_nxt:
            strength += 0.4
        out.append(round(min(1.0, strength), 3))
    return out


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
        ("groot nieuws", "teaser"), ("moet jullie iets vertellen", "teaser"), ("moet ik jullie vertellen", "teaser"),
        ("je raadt nooit", "teaser"), ("je gelooft nooit", "teaser"), ("wat er toen gebeurde", "teaser"),
        ("raad eens", "teaser"), ("guess what", "teaser"), ("you won't believe", "teaser"), ("big news", "teaser"),
    ):
        if phrase in low:
            hits[cat] += 1
    hits["humor"] += len(re.findall(r"\[(?:gelach|laughter|laughs|lacht)\]|\(lacht\)|\(laughs\)", low))
    return hits


def punchline_hits(words: list[Word]) -> int:
    """Laughter / punchline markers in these words ("hahaha", "[gelach]", "lol", ...)."""
    return category_hits(_tokens(words), " ".join(w.text for w in words))["humor"]


def reaction_hits(words: list[Word]) -> int:
    """Words that make a short line a reaction ("Wat?!", "Echt?", "Nee joh!", "hahaha", "wow")."""
    hits = category_hits(_tokens(words), " ".join(w.text for w in words))
    return hits["humor"] + hits["surprise"] + hits["question"] + hits["intensity"]


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
        "intro": 1.0 if is_intro_text(" ".join(w.text for w in clip_words[:14])) else 0.0,
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
    # The loudest reaction right AFTER the cut means the payoff was cut off.
    after = peak_after(ctx.audio, end, 4.0)
    feats["audio_after_peak_z"] = round(after, 3)
    feats["topic_break"] = max(
        [b for i, b in enumerate(ctx.topic_breaks) if ctx.sentences[i].end > start + 0.3 and ctx.sentences[i + 1].start < end - 0.3]
        or [0.0]
    )
    feats["payoff_after_end"] = 1.0 if audio.get("available") and after > max(1.8, audio.get("peak_z", 0) + 0.4) else 0.0
    # The punchline/laugh is said right after the cut (transcript) while the clip itself does not end on one.
    i = bisect.bisect_left(ctx.words, end - 0.01, key=lambda w: w.start)
    after_words = [w for w in ctx.words[i : i + 12] if w.start < end + 4.0]
    feats["punchline_after_end"] = 1.0 if (
        after_words and after_words[0].start - end < 1.5 and punchline_hits(after_words) and not feats["humor_end"]
    ) else 0.0
    return feats


def _c(x: float) -> float:
    return max(0.0, min(100.0, x))


def heuristic_dimension_scores(f: dict[str, float]) -> dict[str, float]:
    """Rule-based estimates of the dimensions. Used when no LLM is configured and as a fallback."""
    audio = f.get("audio_available", 0.0) > 0
    peak = max(0.0, min(3.5, f.get("audio_peak_z", 0.0))) if audio else 0.0
    hook_z = max(-1.5, min(2.5, f.get("audio_hook_z", 0.0))) if audio else 0.0
    crowd = f.get("crowd", 0.0)
    humor = f.get("lex_humor", 0.0)
    rel_rate = f.get("rel_speech_rate", 1.0)
    silence = f.get("audio_silence_ratio", 0.0)

    hook = 42 + 18 * f.get("hook_first_strength", 0) + 7 * hook_z + 10 * f.get("hook_first_question", 0)
    hook -= 22 * f.get("hook_starts_filler", 0) + 14 * f.get("hook_context_opener", 0) + 25 * f.get("intro", 0)
    hook_strength = 100 - 14 * f.get("hook_time_to_strong", 5.0)
    curiosity = 35 + 10 * min(2, f.get("questions", 0)) + 9 * f.get("lex_story", 0) + 8 * f.get("lex_question", 0)
    curiosity += 14 * f.get("lex_teaser", 0)
    curiosity += 6 * f.get("has_number", 0) + 5 * min(2, f.get("superlatives", 0))
    emotion = 30 + 11 * peak + 1.2 * f.get("audio_std_db", 0) + 12 * f.get("lex_intensity", 0) + 5 * min(3, f.get("exclamations", 0))
    surprise = 30 + 16 * f.get("lex_surprise", 0) + (10 if peak > 2.3 else 0) + 6 * f.get("lex_stakes", 0)
    humor_s = 22 + 28 * humor + 6 * f.get("audio_burst_count", 0) + (10 if f.get("humor_end") else 0)
    comment = 30 + 16 * f.get("lex_controversy", 0) + 6 * min(2, f.get("questions", 0)) + 25 * crowd
    retention = 48 + 12 * min(1.3, rel_rate) - 70 * silence + 10 * (1 if f.get("audio_peak_pos", 0) > 0.45 else 0)
    retention -= 25 * f.get("outro", 0) + 15 * f.get("intro", 0)
    topic = f.get("topic_break", 0.0) if f.get("topic_break", 0.0) >= 0.5 else 0.0
    retention -= 22 * topic
    context = 78 - 22 * f.get("hook_context_opener", 0) - 8 * f.get("hook_starts_filler", 0) + 6 * f.get("ends_complete", 0)
    context -= 25 * topic
    payoff = 38 + 16 * (1 if f.get("audio_peak_pos", 0) > 0.5 else 0) + 8 * max(0, f.get("audio_end_z", 0))
    payoff += 12 * f.get("humor_end", 0) + 10 * f.get("ends_complete", 0) - 25 * f.get("payoff_after_end", 0)
    payoff -= 15 * f.get("punchline_after_end", 0)
    # the story arc: build-up towards a climax, a strong ending, a complete story on its own
    peak_pos = f.get("audio_peak_pos", 0.0) if audio else 0.0
    buildup = 40 + 22 * (1 if peak > 1.5 and 0.3 <= peak_pos <= 0.97 else 0) + 7 * min(2, f.get("lex_story", 0))
    buildup += 5 * min(2, f.get("questions", 0)) + 8 * f.get("lex_teaser", 0)
    buildup -= 14 * f.get("hook_context_opener", 0) + 18 * (1 if peak > 1.8 and peak_pos < 0.12 else 0)
    ending = 42 + 14 * f.get("ends_complete", 0) + 14 * f.get("humor_end", 0) + 8 * max(0, f.get("audio_end_z", 0))
    ending += 12 * (1 if peak > 1.5 and peak_pos >= 0.55 else 0)
    ending -= 35 * f.get("payoff_after_end", 0) + 15 * f.get("punchline_after_end", 0) + 25 * f.get("outro", 0)
    standalone = 76 - 24 * f.get("hook_context_opener", 0) - 8 * f.get("hook_starts_filler", 0) - 12 * f.get("intro", 0)
    standalone += 6 * f.get("ends_complete", 0) - 25 * topic - 10 * f.get("payoff_after_end", 0)
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
        "buildup": buildup,
        "ending": ending,
        "standalone": standalone,
    }
    return {k: round(_c(v), 1) for k, v in scores.items()}


def signal_score(f: dict[str, float]) -> float:
    """One number (0-100) for 'the raw signals say something is happening here'."""
    audio = f.get("audio_available", 0.0) > 0
    lex = (
        f.get("lex_intensity", 0) + f.get("lex_surprise", 0) + f.get("lex_humor", 0) * 1.5 + f.get("lex_teaser", 0)
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
    s -= 25 * f.get("outro", 0) + 20 * f.get("intro", 0) + 10 * f.get("payoff_after_end", 0) + 5 * f.get("punchline_after_end", 0)
    if f.get("topic_break", 0.0) >= 0.5:  # two topics glued together
        s -= 22 * f["topic_break"]
    return round(_c(s), 1)


def feature_vector(f: dict[str, float], scores: dict[str, float] | None = None) -> dict[str, float]:
    """Stable, bounded subset used for learning (prefix d_ = dimension scores /100)."""
    keys = (
        "duration", "rel_speech_rate", "questions", "exclamations", "has_number", "crowd", "scene_cuts_per_10s",
        "ends_complete", "humor_end", "lex_question", "lex_intensity", "lex_humor", "lex_surprise",
        "lex_controversy", "lex_story", "lex_stakes", "lex_teaser", "audio_peak_z", "audio_hook_z", "audio_silence_ratio",
        "audio_peak_pos", "hook_starts_filler", "hook_context_opener", "hook_time_to_strong", "hook_first_strength",
        "hook_first_question", "intro", "payoff_after_end", "topic_break", "punchline_after_end",
    )
    vec = {k: float(f.get(k, 0.0)) for k in keys}
    for k, v in (scores or {}).items():
        vec[f"d_{k}"] = float(v) / 100.0
    return {k: (0.0 if (math.isnan(v) or math.isinf(v)) else round(v, 4)) for k, v in vec.items()}


def normalize_text_for_similarity(text: str) -> list[str]:
    return [t for t in (normalize(x) for x in text.split()) if len(t) > 2]
