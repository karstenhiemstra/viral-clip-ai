"""Prompt -> request, the edit styles, and the edit plan: which moments of the footage, how long, which effects.

Everything is rule-based and local: the prompt is read with keywords ("cinematic", "snelle", "voetbal", ...),
the footage is chosen with the measurements from ``analysis`` and the cuts follow the beats of the music.
The same prompt with another ``seed`` gives another edit (other moments, timing, transitions and effects).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import numpy as np

from app.edit.analysis import Footage, Music

STYLE_KEYS = ("hype", "cinematic", "fast", "clean", "football")
TRANSITIONS = ("cut", "flash", "zoom", "whip", "glitch", "dip")
ZOOMS = ("in", "out", "punch")
MIN_SHOT = 0.3  # s
MAX_SHOTS = 48
DEFAULT_DURATION = 15.0


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    beats_per_shot: tuple[int, ...]  # with music: shot lengths in beats (repeating pattern)
    shot_seconds: tuple[float, float]  # without music: shot length range
    transitions: dict[str, float]  # entry transition of a shot -> weight
    zooms: dict[str, float]  # zoom kind -> probability (the rest: no zoom)
    zoom_amount: float
    shake: float  # probabilities per shot ...
    slowmo: float
    ramp: float
    freeze: float
    slow_speed: float = 0.5
    motion_blur: bool = False
    grade: str = ""  # ffmpeg colour filters
    letterbox: bool = False
    vignette: bool = False
    framing: str = "crop"  # crop: 9:16 aimed at the action | blur: whole frame on a blurred background
    title: str = "pop"  # pop | elegant | clean


STYLES: dict[str, Style] = {
    "hype": Style(
        "hype", "Hype", (1, 2, 1, 2, 2), (0.5, 1.3),
        {"cut": 0.25, "flash": 0.3, "zoom": 0.2, "whip": 0.15, "glitch": 0.1}, {"in": 0.3, "out": 0.15, "punch": 0.15},
        0.16, 0.3, 0.12, 0.18, 0.08, motion_blur=True, grade="eq=contrast=1.12:saturation=1.3:brightness=0.015",
    ),
    "cinematic": Style(
        "cinematic", "Cinematic", (4, 2, 4, 4), (1.8, 3.0),
        {"cut": 0.35, "dip": 0.45, "flash": 0.1, "zoom": 0.1}, {"in": 0.45, "out": 0.3},
        0.08, 0.0, 0.45, 0.12, 0.05, slow_speed=0.6,
        grade="eq=contrast=1.08:saturation=0.85:gamma=0.96,colorbalance=rs=0.06:bs=-0.06:rh=0.05:bh=-0.07",
        letterbox=True, vignette=True, title="elegant",
    ),
    "fast": Style(
        "fast", "Fast / Aggressive", (1, 1, 2, 1, 1), (0.35, 0.8),
        {"cut": 0.25, "flash": 0.25, "glitch": 0.25, "whip": 0.25}, {"in": 0.25, "punch": 0.35},
        0.2, 0.55, 0.05, 0.25, 0.06, motion_blur=True, grade="eq=contrast=1.2:saturation=1.25,unsharp=5:5:0.8",
    ),
    "clean": Style(
        "clean", "Clean", (2, 4, 2, 4), (1.4, 2.4),
        {"cut": 0.7, "dip": 0.3}, {"in": 0.35, "out": 0.2},
        0.05, 0.0, 0.1, 0.0, 0.0, grade="eq=saturation=1.06:contrast=1.03", framing="blur", title="clean",
    ),
    "football": Style(
        "football", "Football Edit", (2, 1, 2, 2, 1, 2), (0.7, 1.5),
        {"cut": 0.2, "flash": 0.3, "zoom": 0.3, "whip": 0.2}, {"in": 0.3, "punch": 0.2, "out": 0.1},
        0.14, 0.25, 0.3, 0.3, 0.18, motion_blur=True, grade="eq=contrast=1.1:saturation=1.22,unsharp=5:5:0.6",
    ),
}

# --- the prompt ---------------------------------------------------------------------------------------

_STYLE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cinematic", ("cinematic", "cinematische", "cinematisch", "filmisch", "filmische", "cinema", "episch", "epische", "epic")),
    ("fast", ("snel", "snelle", "fast", "aggressive", "aggressief", "aggressieve", "agressief", "agressieve", "intens", "intense")),
    ("hype", ("hype", "hyped", "energiek", "energieke")),
    ("clean", ("clean", "rustig", "rustige", "simpel", "simpele", "minimal", "minimalistisch", "strak", "strakke")),
    ("football", ("voetbal", "voetbaledit", "football", "soccer", "skills", "goals", "voetballer")),
)
_STOP = set(
    "maak make maken create een an a the de het edit edits video videos montage compilatie compilation clip clips "
    "van of met over from about with voor for me mij please graag alsjeblieft aub pls nieuwe new korte kort short "
    "style stijl seconden seconde sec secs seconds second".split()
)
FOOTBALLERS = set(
    "neymar messi ronaldo cristiano cr7 mbappe haaland vinicius bellingham yamal salah bruyne debruyne dijk vandijk "
    "kane lewandowski modric benzema griezmann pedri gavi saka foden rashford depay gakpo frimpong ibrahimovic zlatan "
    "zidane ronaldinho maradona pele beckham kroos musiala wirtz dembele osimhen lautaro raphinha rodrygo valverde "
    "odegaard palmer".split()
)
_DURATION = re.compile(r"(\d{1,3})\s*(?:s|sec|secs|seconden|seconde|seconds|second|secondes)\b", re.I)
_SUBJECT = re.compile(r"\b(?:edits?|video'?s?|montage|compilatie|compilation|clips?)\s+(?:van|of|met|over|from|about|with|voor)\s+(.+)$", re.I)


def normalize(text: str) -> str:
    """Lowercase without accents: 'Mbappé' -> 'mbappe'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", normalize(text))


@dataclass
class EditRequest:
    prompt: str
    subject: str
    style: str
    style_explicit: bool
    duration: float


def parse_prompt(prompt: str, default_duration: float = DEFAULT_DURATION) -> EditRequest:
    """'Maak een snelle voetbal edit van Mbappé' -> subject 'Mbappé', style 'fast'. Without a style word the
    style is chosen automatically: football for football players/words, otherwise hype."""
    text = (prompt or "").strip()
    toks = tokens(text)
    style = next((key for key, words in _STYLE_WORDS if any(w in toks for w in words)), None)
    m = _DURATION.search(text)
    duration = float(min(60, max(6, int(m.group(1))))) if m else default_duration
    stripped = _DURATION.sub(" ", text)
    style_words = {w for _, words in _STYLE_WORDS for w in words}
    sm = _SUBJECT.search(stripped)
    raw = sm.group(1) if sm else stripped
    keep = [w for w in re.findall(r"[^\s,.!?;:()\"]+", raw) if normalize(w) not in _STOP | style_words]
    if sm is None:  # "Neymar edit": everything that is not a known word
        keep = [w for w in keep if not normalize(w).isdigit()]
    subject = " ".join(keep).strip(" -'\"")
    if not style:
        football = any(t in FOOTBALLERS for t in tokens(subject)) or any(t in dict(_STYLE_WORDS)["football"] for t in toks)
        auto = "football" if football else "hype"
    return EditRequest(prompt=text, subject=subject[:120], style=style or auto, style_explicit=style is not None,
                       duration=duration)


def match_score(subject: str, text: str) -> int:
    """How well a video (its title, file name, channel, tags) matches the subject; 0 = not at all."""
    words = [t for t in tokens(subject) if len(t) >= 3 or t.isalnum() and any(c.isdigit() for c in t)]
    hay = " " + " ".join(tokens(text)) + " "
    hits = sum(1 for w in words if w in hay)
    if hits and " ".join(words) in hay:
        hits += 2
    return hits


# --- the plan -----------------------------------------------------------------------------------------


def _timeline(style: Style, duration: float, music: Music | None, rng: np.random.Generator) -> tuple[list[float], float | None]:
    """Output length of every shot; with music the cuts fall on the beats. Returns (lengths, music start)."""
    if music is not None and len(music.beats) > 4:
        start = music.best_start(duration, rng)
        rel = [float(b - start) for b in music.beats if b - start > 0.05]
        bounds, i, k = [0.0], -1, 0
        while True:
            i += style.beats_per_shot[k % len(style.beats_per_shot)]
            k += 1
            if i >= len(rel):
                break
            if rel[i] - bounds[-1] < 0.25:
                continue
            bounds.append(rel[i])
            if rel[i] >= duration - 0.2 or len(bounds) > MAX_SHOTS:
                break
        if bounds[-1] >= min(duration, music.duration - start) * 0.6:
            return [b - a for a, b in zip(bounds, bounds[1:], strict=False)], start
    outs: list[float] = []
    total = 0.0
    while total < duration - 0.25 and len(outs) < MAX_SHOTS:
        length = float(rng.uniform(*style.shot_seconds))
        outs.append(length)
        total += length
    outs[-1] -= total - duration
    if outs[-1] < MIN_SHOT and len(outs) > 1:
        outs[-2] += outs.pop()
    return [round(x, 3) for x in outs], None


def source_seconds(shot: dict) -> float:
    """Seconds of footage a shot uses: slow motion uses less, a speed ramp is fast first, then slow."""
    moving = max(0.1, float(shot["out"]) - float(shot.get("freeze") or 0))
    if shot.get("ramp"):
        return 0.55 * moving * 1.7 + 0.45 * moving * 0.45
    return moving * float(shot.get("speed") or 1.0)


def _pick(weights: dict[str, float], rng: np.random.Generator) -> str:
    keys = list(weights)
    p = np.asarray([weights[k] for k in keys], dtype=float)
    return str(keys[int(rng.choice(len(keys), p=p / p.sum()))])


def _effects(style: Style, out: float, first: bool, rng: np.random.Generator) -> dict:
    zoom = None
    r = rng.random()
    acc = 0.0
    for kind, p in style.zooms.items():
        acc += p
        if r < acc:
            zoom = kind
            break
    slowmo = rng.random() < style.slowmo
    ramp = not slowmo and out >= 0.9 and rng.random() < style.ramp
    freeze = round(min(0.45, out * 0.35), 2) if out >= 0.8 and rng.random() < style.freeze else 0.0
    return {
        "out": round(out, 3),
        "speed": style.slow_speed if slowmo else 1.0,
        "ramp": bool(ramp),
        "freeze": freeze,
        "transition": "cut" if first else _pick(style.transitions, rng),
        "zoom": zoom,
        "shake": bool(rng.random() < style.shake),
        "enabled": True,
    }


class _Pool:
    """Candidate moments in the footage; picks the best free window of a given length."""

    def __init__(self, footage: list[Footage]):
        self.items = []
        for fx in footage:
            s = fx.scores()
            if len(s) == 0:
                continue
            csum = np.concatenate([[0.0], np.cumsum(s, dtype=np.float64)])
            edge = min(2.0, fx.duration * 0.03)
            self.items.append({"fx": fx, "csum": csum, "shots": fx.shots(edge), "edge": edge, "used": []})
        self.uses: dict[int, int] = {}

    def total_seconds(self) -> float:
        return sum(max(0.0, it["fx"].duration - 2 * it["edge"]) for it in self.items)

    def pick(self, length: float, rng: np.random.Generator, noise: float) -> dict | None:
        best, best_score = None, -np.inf
        for strict in (True, False):  # within one camera shot first; else anywhere; finally allow reuse
            for reuse in (False, True):
                for it in self.items:
                    fx = it["fx"]
                    spans = it["shots"] if strict else [(it["edge"], fx.duration - it["edge"])]
                    for a, b in spans:
                        if b - a < length:
                            continue
                        starts = np.arange(a, b - length + 1e-6, max(0.2, 1.0 / fx.fps))
                        if not reuse:
                            free = np.ones(len(starts), dtype=bool)
                            for u0, u1 in it["used"]:
                                free &= (starts + length + 0.5 <= u0) | (starts >= u1 + 0.5)
                            starts = starts[free]
                        if len(starts) == 0:
                            continue
                        i0 = np.clip((starts * fx.fps).astype(int), 0, len(it["csum"]) - 2)
                        i1 = np.clip(((starts + length) * fx.fps).astype(int), 1, len(it["csum"]) - 1)
                        mean = (it["csum"][i1] - it["csum"][i0]) / np.maximum(1, i1 - i0)
                        mean = mean + rng.normal(0.0, noise, len(mean)) - 0.4 * self.uses.get(fx.video_id, 0) / (1 + len(self.items))
                        j = int(np.argmax(mean))
                        if mean[j] > best_score:
                            best, best_score = (it, float(starts[j])), float(mean[j])
                if best is not None:
                    break
            if best is not None:
                break
        if best is None:
            return None
        it, start = best
        fx = it["fx"]
        it["used"].append((start, start + length))
        self.uses[fx.video_id] = self.uses.get(fx.video_id, 0) + 1
        i0, i1 = int(start * fx.fps), max(int(start * fx.fps) + 1, int((start + length) * fx.fps))
        cx = float(np.mean(fx.cx[i0:i1])) if len(fx.cx[i0:i1]) else 0.5
        return {"video_id": fx.video_id, "start": round(start, 3), "score": round(best_score, 2), "cx": round(cx, 3)}


class NotEnoughFootage(ValueError):
    pass


def build_plan(
    subject: str,
    style_key: str,
    duration: float,
    footage: list[Footage],
    seed: int,
    *,
    music: Music | None = None,
    music_enabled: bool = True,
) -> dict:
    style = STYLES.get(style_key, STYLES["hype"])
    rng = np.random.default_rng(seed)
    pool = _Pool(footage)
    if pool.total_seconds() < 3.0:
        raise NotEnoughFootage("Te weinig bruikbaar beeldmateriaal: voeg langere video's toe (minimaal een paar seconden beeld).")
    duration = min(duration, max(6.0, pool.total_seconds() * 0.9))
    outs, music_start = _timeline(style, duration, music if music_enabled else None, rng)
    shots = [_effects(style, out, k == 0, rng) for k, out in enumerate(outs)]
    # the best moments go to the finale and the opening (hook), then the rest
    order = [len(shots) - 1, 0] + [int(i) for i in rng.permutation(range(1, len(shots) - 1))] if len(shots) > 1 else [0]
    for k in order:
        src = pool.pick(source_seconds(shots[k]), rng, noise=0.5)
        if src is None:  # a too long shot for the footage: no slow parts, shorter
            shots[k].update(speed=1.0, ramp=False, freeze=0.0)
            src = pool.pick(source_seconds(shots[k]), rng, noise=0.5)
        if src is None:
            raise NotEnoughFootage("Te weinig bruikbaar beeldmateriaal voor deze edit.")
        shots[k].update(src)
    plan = {
        "version": 1,
        "style": style.key,
        "subject": subject,
        "title": subject.upper()[:40],
        "duration": round(sum(s["out"] for s in shots), 3),
        "seed": int(seed),
        "music_enabled": bool(music_enabled and music_start is not None),
        "music": None
        if music is None or music_start is None
        else {"file": music.path.name, "start": round(music_start, 3), "bpm": music.bpm},
        "text": True,
        "shots": shots,
    }
    return plan


def validate_plan(plan: dict, durations: dict[int, float]) -> dict:
    """A plan edited by the user: keep known fields, clamp values, drop shots of unknown videos."""
    shots = []
    for s in plan.get("shots") or []:
        vid = int(s.get("video_id", -1))
        if vid not in durations:
            continue
        shot = {
            "video_id": vid,
            "start": max(0.0, float(s.get("start", 0.0))),
            "out": round(min(8.0, max(MIN_SHOT, float(s.get("out", 1.0)))), 3),
            "speed": min(2.0, max(0.25, float(s.get("speed", 1.0)))),
            "ramp": bool(s.get("ramp", False)),
            "freeze": round(min(1.0, max(0.0, float(s.get("freeze") or 0.0))), 2),
            "transition": s.get("transition") if s.get("transition") in TRANSITIONS else "cut",
            "zoom": s.get("zoom") if s.get("zoom") in ZOOMS else None,
            "shake": bool(s.get("shake", False)),
            "cx": min(1.0, max(0.0, float(s.get("cx", 0.5)))),
            "score": float(s.get("score", 0.0)),
            "enabled": bool(s.get("enabled", True)),
        }
        shot["freeze"] = min(shot["freeze"], round(shot["out"] - 0.1, 2))
        shot["start"] = min(shot["start"], max(0.0, durations[vid] - source_seconds(shot) - 0.05))
        shots.append(shot)
    active = [s for s in shots if s["enabled"]]
    if not active:
        raise ValueError("Er moet minstens één clip in de edit blijven")
    return {**plan, "shots": shots, "duration": round(sum(s["out"] for s in active), 3)}
