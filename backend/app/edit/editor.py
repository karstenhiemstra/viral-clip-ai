"""Prompt -> request, the edit styles, and the edit plan: which actions, how long, which effects.

Everything is rule-based and local: the prompt is read with keywords ("cinematic", "snelle", "voetbal", ...)
and the plan is built from COMPLETE actions found in the footage (``analysis.detect_actions``): a whole
dribble, skill move, sprint or shot, with a little context before and after, never half a movement.

Priorities, in this order:
1. a complete action (5 whole actions beat 12 half ones),
2. player + ball clearly in the picture (the moving part of the picture stays in frame),
3. good framing: the 9:16 crop follows the action; the whole frame when the action is too wide for 9:16;
   a little closer when it is far away,
4. a strong moment (the strongest action opens the edit, the next strongest closes it),
5. timing: cuts on the beat - by giving an action a little more or less context, never by cutting it,
6. effects, rationed: most cuts are clean; at most one striking effect per shot and never two striking shots
   in a row; slow motion, a speed ramp, a punch-in or a short shake go to the strongest moments, at the
   moment itself,
7. text: the name only at the end (or not at all), never on the first shot.

The same prompt with another ``seed`` gives another edit (other actions, order and effects).
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

import numpy as np

from app.edit.analysis import Action, Footage, Music, detect_actions

STYLE_KEYS = ("hype", "cinematic", "fast", "clean", "football")
TRANSITIONS = ("cut", "flash", "zoom", "whip", "glitch", "dip")
ZOOMS = ("in", "out", "punch")
STRONG_TRANSITIONS = {"flash", "zoom", "whip", "glitch"}
MOMENTS = ("slowmo", "ramp", "punch", "shake", "freeze")
MIN_SHOT = 0.3  # s
MAX_SHOTS = 48
DEFAULT_DURATION = 15.0


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    shot_seconds: tuple[float, float]  # fallback shots (when no clear action is found): length range
    accents: dict[str, float]  # transitions for the accent cuts -> weight
    moments: dict[str, float]  # effects on the strongest moments -> weight
    action_len: tuple[float, float] = (0.8, 3.5)  # length of one action (s)
    accent_ratio: float = 0.35  # share of the cuts that get an accent transition
    max_moments: int = 2  # slow motion / ramp / punch-in / shake / freeze frames per edit
    push_in: float = 0.3  # share of the calm shots with a subtle slow zoom
    zoom_amount: float = 0.06  # how far that subtle zoom goes
    calm: str = "cut"  # the normal transition
    slow_speed: float = 0.5
    motion_blur: bool = False
    grade: str = ""  # ffmpeg colour filters
    letterbox: bool = False
    vignette: bool = False
    framing: str = "crop"  # crop: 9:16 following the action | blur: whole frame on a blurred background
    title: str = "pop"  # pop | elegant | clean


STYLES: dict[str, Style] = {
    "hype": Style(
        "hype", "Hype", (0.8, 1.6), {"flash": 0.45, "zoom": 0.35, "whip": 0.2},
        {"ramp": 0.35, "punch": 0.25, "slowmo": 0.2, "shake": 0.1, "freeze": 0.1},
        action_len=(0.7, 3.0), accent_ratio=0.4, motion_blur=True,
        grade="eq=contrast=1.12:saturation=1.3:brightness=0.015",
    ),
    "cinematic": Style(
        "cinematic", "Cinematic", (1.8, 3.0), {"dip": 0.7, "flash": 0.3}, {"slowmo": 0.7, "ramp": 0.3},
        action_len=(1.0, 4.0), accent_ratio=0.35, push_in=0.6, zoom_amount=0.06, slow_speed=0.6,
        grade="eq=contrast=1.08:saturation=0.85:gamma=0.96,colorbalance=rs=0.06:bs=-0.06:rh=0.05:bh=-0.07",
        letterbox=True, vignette=True, title="elegant",
    ),
    "fast": Style(
        "fast", "Fast / Aggressive", (0.5, 1.0), {"flash": 0.35, "whip": 0.35, "glitch": 0.3},
        {"punch": 0.35, "shake": 0.3, "ramp": 0.2, "freeze": 0.15},
        action_len=(0.6, 2.2), accent_ratio=0.5, max_moments=3, push_in=0.2, zoom_amount=0.08, motion_blur=True,
        grade="eq=contrast=1.2:saturation=1.25,unsharp=5:5:0.8",
    ),
    "clean": Style(
        "clean", "Clean", (1.4, 2.4), {"dip": 1.0}, {"slowmo": 1.0},
        action_len=(1.0, 4.0), accent_ratio=0.3, max_moments=1, zoom_amount=0.04,
        grade="eq=saturation=1.06:contrast=1.03", framing="blur", title="clean",
    ),
    "football": Style(
        "football", "Football Edit", (0.9, 1.6), {"flash": 0.45, "zoom": 0.35, "whip": 0.2},
        {"ramp": 0.35, "slowmo": 0.3, "punch": 0.15, "freeze": 0.1, "shake": 0.1},
        action_len=(0.8, 3.5), accent_ratio=0.35, push_in=0.25, motion_blur=True,
        grade="eq=contrast=1.1:saturation=1.22,unsharp=5:5:0.6",
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



# --- speed of a shot ----------------------------------------------------------------------------------


def _profile(shot: dict) -> list[tuple[float, float]]:
    """(source seconds, speed) in order; the last part takes whatever is left. Slow motion and a speed ramp
    sit at the strongest moment (``peak``, seconds from the start of the shot): normal speed into it, fast
    into it for a ramp, then about a second of slow motion through it, then normal speed again."""
    peak = shot.get("peak")
    speed = float(shot.get("speed") or 1.0)
    if shot.get("ramp"):
        pre = max(0.0, (peak if peak is not None else 0.4) - 0.25)
        return [(pre, 1.5), (0.9, 0.5), (math.inf, 1.0)]
    if speed < 0.9 and peak is not None:
        return [(max(0.0, peak - 0.45), 1.0), (0.9, speed), (math.inf, 1.0)]
    return [(math.inf, speed)]


def shot_parts(shot: dict) -> list[tuple[float, float]]:
    """(source seconds, speed) per part of a shot. ``out`` (output seconds, freeze included) decides the length."""
    left = max(0.1, float(shot["out"]) - float(shot.get("freeze") or 0))
    parts = []
    for src, speed in _profile(shot):
        if left <= 1e-6:
            break
        if src <= 0:
            continue
        out = min(left, src / speed)
        parts.append((out * speed, speed))
        left -= out
    return parts


def source_seconds(shot: dict) -> float:
    """Seconds of footage a shot uses."""
    return sum(src for src, _ in shot_parts(shot))


def output_seconds(shot: dict, src_total: float) -> float:
    """Output seconds (freeze included) when ``src_total`` seconds of footage are shown with the shot's speed."""
    out, left = 0.0, src_total
    for src, speed in _profile(shot):
        take = min(left, src)
        if take <= 0:
            continue
        out += take / speed
        left -= take
    return out + float(shot.get("freeze") or 0)


# --- framing ------------------------------------------------------------------------------------------


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) < 2 or n <= 1:
        return x.astype(float)
    half = n // 2
    return np.convolve(np.pad(x, half, mode="edge"), np.ones(2 * half + 1) / (2 * half + 1), mode="valid")


def _framing(fx: Footage, a: float, b: float, style: Style) -> dict:
    """How the 9:16 picture is taken from the frame for the window [a, b]:
    crop - a 9:16 crop that follows the action (``track``: [t from the window start, cx, cy] keyframes,
           smoothed, at most ~1/3 frame width per second so it never jerks), ``zoom`` > 1 for far-away action;
    fit  - the whole frame on a blurred background, when the action is too wide for a 9:16 crop."""
    if style.framing == "blur":
        return {"mode": "fit", "zoom": 1.0, "track": []}
    i0, i1 = fx.idx(a), max(fx.idx(a) + 1, fx.idx(b) + 1)
    crop_frac = min(1.0, (fx.height * 9 / 16) / max(1, fx.width))
    cover = float(np.median(fx.cover[i0:i1])) if len(fx.cover) >= i1 else 1.0
    extent = float(np.percentile(fx.extent[i0:i1], 80)) if len(fx.extent) >= i1 else 0.2
    if crop_frac < 0.95 and cover < 0.4 and extent > 0.45:
        return {"mode": "fit", "zoom": 1.0, "track": []}
    zoom = 1.0
    if cover >= 0.6 and extent > 0:
        zoom = 1.35 if extent < 0.06 else 1.2 if extent < 0.1 else 1.0
        zoom = max(1.0, min(zoom, crop_frac / max(1e-3, extent * 1.6)))  # the action must still fit
    cx = fx.cx[i0:i1].astype(float) if len(fx.cx) >= i1 else np.full(i1 - i0, 0.5)
    cy = fx.cy[i0:i1].astype(float) if len(fx.cy) >= i1 else np.full(i1 - i0, 0.5)
    cx, cy = _smooth(cx, int(round(0.8 * fx.fps))), _smooth(cy, int(round(0.8 * fx.fps)))
    step = 0.33 / fx.fps  # the crop moves at most a third of the frame width per second
    for k in range(1, len(cx)):
        cx[k] = cx[k - 1] + float(np.clip(cx[k] - cx[k - 1], -step, step))
        cy[k] = cy[k - 1] + float(np.clip(cy[k] - cy[k - 1], -step, step))
    every = max(1, int(round(0.4 * fx.fps)))
    keys = list(range(0, len(cx), every))
    if keys[-1] != len(cx) - 1:
        keys.append(len(cx) - 1)
    track = [[round(max(0.0, (i0 + k) / fx.fps - a), 3), round(float(cx[k]), 3), round(float(cy[k]), 3)] for k in keys]
    return {"mode": "crop", "zoom": round(zoom, 2), "track": track}


# --- the plan -----------------------------------------------------------------------------------------


def _pick(weights: dict[str, float], rng: np.random.Generator) -> str:
    keys = list(weights)
    p = np.asarray([weights[k] for k in keys], dtype=float)
    return str(keys[int(rng.choice(len(keys), p=p / p.sum()))])


def _shot(act: Action, fx: Footage, style: Style, moment: str | None) -> dict:
    shot = {
        "video_id": act.video_id,
        "start": act.win_start,
        "peak": round(act.peak - act.win_start, 3),
        "action": [act.start, act.end],
        "bounds": [act.shot_start, act.shot_end],
        "speed": style.slow_speed if moment == "slowmo" else 1.0,
        "ramp": moment == "ramp",
        "freeze": 0.35 if moment == "freeze" else 0.0,
        "transition": "cut",
        "zoom": "punch" if moment == "punch" else None,
        "shake": moment == "shake",
        "moment": moment,
        "score": act.score,
        "checks": {**act.checks, "redundant": False},
        "framing": _framing(fx, act.win_start, act.win_end, style),
        "enabled": True,
    }
    shot["cx"] = round(float(np.mean([k[1] for k in shot["framing"]["track"]])) if shot["framing"]["track"] else 0.5, 3)
    shot["out"] = round(output_seconds(shot, act.win_end - act.win_start), 3)
    return shot


def _fallback_windows(
    footage: list[Footage], used: dict, style: Style, need: float, rng: np.random.Generator, thumbs: list[np.ndarray]
) -> list[dict]:
    """When the footage has too few clear actions: the liveliest free stretches inside one camera shot
    (marked as fallback, so they rank below real actions)."""
    picked: list[dict] = []
    total = 0.0
    used = {k: list(v) for k, v in used.items()}
    while total < need:
        best = None
        for fx in footage:
            s = fx.scores()
            if len(s) == 0:
                continue
            for a, b in fx.shots(min(1.0, fx.duration * 0.03)):
                length = min(b - a, float(rng.uniform(*style.shot_seconds)), max(0.8, need - total + 0.4))
                if length < 0.6:
                    continue
                for t in np.arange(a, b - length + 1e-6, 0.2):
                    if any(t < u1 + 0.3 and t + length > u0 - 0.3 for u0, u1 in used.get(fx.video_id, [])):
                        continue
                    th = fx.thumb(t + length / 2)
                    if th is not None and any(float(np.abs(th - o).mean()) < 0.02 for o in thumbs):
                        continue  # the same picture as a shot we already have
                    v = float(np.mean(s[fx.idx(t) : max(fx.idx(t) + 1, fx.idx(t + length))]))
                    if best is None or v > best[0]:
                        best = (v, fx, float(t), length, a, b)
        if best is None:
            break
        v, fx, t, length, a, b = best
        act = Action(fx.video_id, t, t + length, t + length / 2, t, t + length, 0.0, v - 3.0,
                     {"complete": False, "fallback": True, "visible": True, "dark": False}, a, b)
        shot = _shot(act, fx, style, None)
        picked.append(shot)
        used.setdefault(fx.video_id, []).append((t, t + length))
        if (th := fx.thumb(t + length / 2)) is not None:
            thumbs.append(th)
        total += shot["out"]
    return picked


def _snap(shot: dict, ends: list[float], t0: float) -> float:
    """Let the shot end on a beat (``ends``: beat times from the start of the edit) by giving it a little more
    or less context - never by cutting the action: more footage after it (or before it) inside the same camera
    shot, else a short freeze frame. Returns the new end time."""
    src = source_seconds(shot)
    act_end = (shot.get("action") or [0, shot["start"] + src])[1] - shot["start"]
    core = output_seconds({**shot, "freeze": 0}, min(src, act_end + 0.15))
    want = t0 + shot["out"]
    options = [b for b in ends if b - t0 >= max(core, 0.25) - 1e-3]
    if not options:
        return want
    end = min(options, key=lambda b: abs(b - want))
    delta = end - want
    lo, hi = shot.get("bounds") or [shot["start"], shot["start"] + src]
    if delta > 0:
        after = max(0.0, min(delta, hi - (shot["start"] + src) - 0.05))
        before = max(0.0, min(delta - after, shot["start"] - lo - 0.05))
        shot["start"] = round(shot["start"] - before, 3)
        shot["peak"] = round((shot.get("peak") or 0) + before, 3)
        for key in shot.get("framing", {}).get("track", []):
            key[0] = round(key[0] + before, 3)
        shot["freeze"] = round(float(shot.get("freeze") or 0) + max(0.0, delta - after - before), 3)
    shot["out"] = round(end - t0, 3)
    return end


def build_plan(
    subject: str,
    style_key: str,
    duration: float,
    footage: list[Footage],
    seed: int,
    *,
    music: Music | None = None,
    music_enabled: bool = True,
    text: bool = True,
) -> dict:
    style = STYLES.get(style_key, STYLES["hype"])
    rng = np.random.default_rng(seed)
    by_id = {fx.video_id: fx for fx in footage}
    usable = sum(max(0.0, fx.duration - 1.0) for fx in footage)
    if usable < 3.0:
        raise NotEnoughFootage("Te weinig bruikbaar beeldmateriaal: voeg langere video's toe (minimaal een paar seconden beeld).")
    duration = min(duration, max(6.0, usable * 0.9))
    track = music if music_enabled and music is not None and len(music.beats) > 4 else None
    music_start = track.best_start(duration, rng) if track is not None else None

    # 1. complete actions, best first (a little randomness: "opnieuw genereren" gives another edit)
    actions = [a for fx in footage for a in detect_actions(fx, *style.action_len)]
    noisy = {id(a): a.score + float(rng.normal(0.0, 0.6)) for a in actions}
    ranked = sorted(actions, key=lambda a: -noisy[id(a)])
    good = [a for a in ranked if a.checks["complete"] and a.checks["visible"] and not a.checks["dark"]]
    second = [a for a in ranked if a not in good and not a.checks["dark"]]
    shots: list[dict] = []
    used: dict[int, list[tuple[float, float]]] = {}
    thumbs: list[np.ndarray] = []
    moments_left = style.max_moments
    total = 0.0
    for pool in (good, second):
        for act in pool:
            if total >= duration - 0.3 or len(shots) >= MAX_SHOTS:
                break
            if any(act.win_start < u1 + 0.3 and act.win_end > u0 - 0.3 for u0, u1 in used.get(act.video_id, [])):
                continue
            fx = by_id[act.video_id]
            th = fx.thumb(act.peak)
            if th is not None and any(float(np.abs(th - o).mean()) < 0.02 for o in thumbs):
                continue  # (nearly) the same picture as a shot we already have
            moment = None
            if moments_left > 0 and act.intensity >= 1.5 and style.moments:
                moment = _pick(style.moments, rng)
                moments_left -= 1
            shot = _shot(act, fx, style, moment)
            if moment is not None and total + shot["out"] > duration + 1.0 and total >= duration * 0.5:
                shot, moment = _shot(act, fx, style, None), None  # the slow motion would make it too long
                moments_left += 1
            if total + shot["out"] > duration + 1.0 and total >= duration * 0.5:
                continue  # would make the edit too long: a shorter complete action fits better
            shots.append(shot)
            used.setdefault(act.video_id, []).append((act.win_start, act.win_end))
            if th is not None:
                thumbs.append(th)
            total += shot["out"]
    if total < duration - 1.0:  # too few clear actions in this footage: the liveliest stretches fill the gap
        for shot in _fallback_windows(footage, used, style, duration - total, rng, thumbs):
            shots.append(shot)
            total += shot["out"]
    if not shots:
        raise NotEnoughFootage("Te weinig bruikbaar beeldmateriaal voor deze edit.")

    # 2. order: the strongest action opens (the hook), the next strongest closes, the rest in between
    by_score = sorted(shots, key=lambda s: -s["score"])
    middle = by_score[2:]
    order = [by_score[0]] + [middle[i] for i in rng.permutation(len(middle))] + by_score[1:2]

    # 3. effects, rationed: an accent transition only now and then, never on two shots in a row and never
    #    together with a moment effect; calm shots may get a subtle slow zoom
    last_strong = -10
    for i, shot in enumerate(order):
        strong_moment = shot.get("moment") is not None
        if strong_moment and i - last_strong < 2:  # two striking shots in a row: keep the first
            src = source_seconds(shot)
            shot.update(speed=1.0, ramp=False, freeze=0.0, zoom=None, shake=False, moment=None)
            shot["out"] = round(output_seconds(shot, src), 3)
            strong_moment = False
        transition = style.calm
        if i > 0 and not strong_moment and i - last_strong >= 2 and rng.random() < style.accent_ratio * 1.5:
            transition = _pick(style.accents, rng)
        shot["transition"] = transition
        if transition in STRONG_TRANSITIONS or strong_moment:
            last_strong = i
        elif shot.get("zoom") is None and rng.random() < style.push_in:
            shot["zoom"] = "in"

    # 4. not (much) longer than asked: the weakest middle shots go
    while len(order) > 2:
        total = sum(s["out"] for s in order)
        weakest = min(order[1:-1], key=lambda s: s["score"])
        if total <= duration + 0.6 or total - weakest["out"] < duration - 0.6:
            break
        order.remove(weakest)

    # 5. timing: every cut on a beat of the music (if that makes it too long, the weakest middle shot goes)
    if track is not None:
        ends = [float(b - music_start) for b in track.beats if b - music_start > 0.2]
        while True:
            t = 0.0
            for shot in order:
                t = _snap(shot, ends, t)
            if len(order) <= 2 or t <= duration + 0.8:
                break
            order.remove(min(order[1:-1], key=lambda s: s["score"]))

    plan = {
        "version": 2,
        "style": style.key,
        "subject": subject,
        "title": subject.upper()[:40],
        "duration": round(sum(s["out"] for s in order), 3),
        "seed": int(seed),
        "music_enabled": track is not None,
        "music": None if track is None else {"file": track.path.name, "start": round(music_start, 3), "bpm": track.bpm},
        "text": bool(text),
        "shots": order,
        "quality": {
            "shots": len(order),
            "complete_actions": sum(1 for s in order if s.get("checks", {}).get("complete")),
            "fallback_shots": sum(1 for s in order if s.get("checks", {}).get("fallback")),
            "strong_effects": sum(1 for s in order if s.get("moment") or s["transition"] in STRONG_TRANSITIONS),
        },
    }
    return plan


class NotEnoughFootage(ValueError):
    pass


def _clean_framing(f: object) -> dict | None:
    if not isinstance(f, dict) or f.get("mode") not in ("crop", "fit"):
        return None
    track = []
    for k in (f.get("track") or [])[:80]:
        try:
            t, cx, cy = (float(v) for v in k[:3])
        except (TypeError, ValueError):
            continue
        track.append([max(0.0, t), min(1.0, max(0.0, cx)), min(1.0, max(0.0, cy))])
    return {"mode": f["mode"], "zoom": min(1.6, max(1.0, float(f.get("zoom") or 1.0))), "track": track}


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
        if s.get("peak") is not None:
            shot["peak"] = max(0.0, float(s["peak"]))
        if s.get("moment") in MOMENTS:
            shot["moment"] = s["moment"]
        for key in ("action", "bounds"):
            v = s.get(key)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                shot[key] = [float(v[0]), float(v[1])]
        if isinstance(s.get("checks"), dict):
            shot["checks"] = {k: v for k, v in s["checks"].items() if isinstance(v, (bool, int, float))}
        framing = _clean_framing(s.get("framing"))
        if framing is not None:
            shot["framing"] = framing
        shot["freeze"] = min(shot["freeze"], round(shot["out"] - 0.1, 2))
        shot["start"] = min(shot["start"], max(0.0, durations[vid] - source_seconds(shot) - 0.05))
        shots.append(shot)
    active = [s for s in shots if s["enabled"]]
    if not active:
        raise ValueError("Er moet minstens één clip in de edit blijven")
    return {**plan, "shots": shots, "duration": round(sum(s["out"] for s in active), 3)}
