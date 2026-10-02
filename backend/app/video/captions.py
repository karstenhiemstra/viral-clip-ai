"""Caption engine: word-timed ASS subtitles with presets, burned in by ffmpeg/libass.

Presets
* bold_white - big white words with a heavy outline, 2-3 words at a time (classic, very readable).
* dynamic    - TikTok style: UPPERCASE, the spoken word lights up and pops, emphasis words coloured.
* minimal    - smaller sentence-case lines on a soft translucent box.

Captions are positioned from the reframing plan so they sit below faces (or above them for close-ups)
and inside TikTok's safe zone (clear of the bottom UI and the right-hand buttons).

The user can edit the captions of a clip: the automatic grouping is offered as a list of cues
({start, end, text} in clip time, see ``auto_cues``), edited cues are stored on the clip and turned back
into word groups for the same styles and animations (``cues_to_groups``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.ai.transcript import normalize

PLAY_W, PLAY_H = 1080, 1920


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS uses BGR + inverted alpha)."""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    font: str
    size: int
    color: str
    highlight: str
    emphasis: str
    outline_color: str
    outline: float
    shadow: float
    bold: bool
    uppercase: bool
    max_words: int
    max_chars: int
    karaoke: bool  # highlight the currently spoken word
    pop: bool
    box: bool = False
    box_color: str = "#000000"
    box_alpha: int = 0x60


PRESETS: dict[str, CaptionStyle] = {
    "bold_white": CaptionStyle(
        name="bold_white", font="Montserrat ExtraBold", size=92, color="#FFFFFF", highlight="#FFFFFF",
        emphasis="#FFE14D", outline_color="#000000", outline=7, shadow=2, bold=True, uppercase=False,
        max_words=3, max_chars=18, karaoke=False, pop=False,
    ),
    "dynamic": CaptionStyle(
        name="dynamic", font="Montserrat Black", size=98, color="#FFFFFF", highlight="#3CFF6B",
        emphasis="#FFE14D", outline_color="#000000", outline=8, shadow=3, bold=True, uppercase=True,
        max_words=3, max_chars=16, karaoke=True, pop=True,
    ),
    "minimal": CaptionStyle(
        name="minimal", font="Montserrat SemiBold", size=64, color="#FFFFFF", highlight="#FFFFFF",
        emphasis="#FFFFFF", outline_color="#000000", outline=0, shadow=0, bold=False, uppercase=False,
        max_words=7, max_chars=34, karaoke=False, pop=False, box=True, box_color="#000000", box_alpha=0x70,
    ),
}

_FINAL_PUNCT = re.compile(r"[.!?…]$")


@dataclass
class CaptionWord:
    start: float
    end: float
    text: str


def _escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def group_words(words: list[CaptionWord], style: CaptionStyle) -> list[list[CaptionWord]]:
    groups: list[list[CaptionWord]] = []
    cur: list[CaptionWord] = []
    for w in words:
        if cur:
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            gap = w.start - cur[-1].end
            if len(cur) >= style.max_words or chars > style.max_chars or gap > 0.45 or _FINAL_PUNCT.search(cur[-1].text):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _group_end(groups: list[list[CaptionWord]], gi: int) -> float:
    """When a caption group disappears: hold briefly after the last word, never overlap the next group."""
    group = groups[gi]
    g_start, g_end = group[0].start, group[-1].end
    nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else g_end + 0.6
    g_end = max(g_end, min(nxt, g_end + 0.35))
    return max(g_end, g_start + 0.25)


def caption_y(face_bottom_ratio: float | None, face_top_ratio: float | None, layout: str) -> int:
    """Vertical centre of the caption block in the 1080x1920 frame."""
    if layout == "split":
        return int(PLAY_H * 0.5)
    if layout in ("fit_blur", "audio"):
        return int(PLAY_H * 0.73)
    y = 0.68
    if face_bottom_ratio is not None:
        if face_bottom_ratio > 0.8 and face_top_ratio is not None and face_top_ratio > 0.3:
            y = 0.22  # extreme close-up: put captions above the face
        elif face_bottom_ratio + 0.06 > y:
            y = min(0.78, face_bottom_ratio + 0.07)
    return int(PLAY_H * y)


def _display_text(w: CaptionWord, style: CaptionStyle) -> str:
    t = w.text.strip()
    if style.name != "minimal":
        t = t.strip(",;:")
    return t.upper() if style.uppercase else t


def build_ass(
    words: list[CaptionWord],
    preset: str,
    *,
    y: int,
    emphasis: list[str] | None = None,
    title: str | None = None,
    title_duration: float = 2.8,
    groups: list[list[CaptionWord]] | None = None,
) -> str:
    """``groups`` (from ``cues_to_groups``): captions edited by the user, shown exactly at their own times;
    otherwise ``words`` are grouped automatically."""
    style = PRESETS.get(preset, PRESETS["bold_white"])
    emph = {normalize(e) for e in (emphasis or []) if e}
    border_style = 3 if style.box else 1
    back = ass_color(style.box_color, style.box_alpha) if style.box else ass_color("#000000", 0x80)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style.font},{style.size},{ass_color(style.color)},{ass_color(style.highlight)},{ass_color(style.outline_color)},{back},{-1 if style.bold else 0},0,0,0,100,100,0,0,{border_style},{style.outline if not style.box else 14},{style.shadow},5,90,150,0,1
Style: Title,Montserrat ExtraBold,64,{ass_color('#111111')},{ass_color('#111111')},{ass_color('#FFFFFF')},{ass_color('#FFFFFF', 0)},-1,0,0,0,100,100,0,0,3,18,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    pos = f"{{\\an5\\pos({PLAY_W // 2},{y})}}"
    exact = groups is not None
    if groups is None:
        groups = group_words(words, style)
    for gi, group in enumerate(groups):
        g_start = group[0].start
        g_end = group[-1].end if exact else _group_end(groups, gi)

        def render(active: int | None, group: list[CaptionWord] = group) -> str:
            parts = []
            for i, w in enumerate(group):
                txt = _escape(_display_text(w, style))
                is_emph = normalize(w.text) in emph
                if active is not None and i == active:
                    colour = ass_color(style.highlight)
                    scale = "\\fscx112\\fscy112" if style.pop else ""
                    parts.append(f"{{\\c{colour}{scale}}}{txt}{{\\r}}")
                elif is_emph:
                    parts.append(f"{{\\c{ass_color(style.emphasis)}}}{txt}{{\\r}}")
                else:
                    parts.append(txt)
            return " ".join(parts)

        if style.karaoke:
            for i, w in enumerate(group):
                s = g_start if i == 0 else w.start
                e = group[i + 1].start if i + 1 < len(group) else g_end
                if e - s < 0.04:
                    continue
                events.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{pos}{render(i)}")
        else:
            events.append(f"Dialogue: 0,{_ts(g_start)},{_ts(g_end)},Cap,,0,0,0,,{pos}{render(None)}")

    if title:
        tpos = f"{{\\an5\\pos({PLAY_W // 2},{int(PLAY_H * 0.16)})\\fad(120,200)}}"
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(title_duration)},Title,,0,0,0,,{tpos}{_escape(title[:70])}")
    return header + "\n".join(events) + "\n"


# --- manual editing -----------------------------------------------------------------------------------

MIN_CUE = 0.1  # seconds
MAX_CUES = 500
MAX_CUE_CHARS = 200


class CaptionError(ValueError):
    """Invalid edited captions (message is shown to the user)."""


def auto_cues(words: list[CaptionWord], preset: str, duration: float | None = None) -> list[dict]:
    """The automatic captions as editable cues [{start, end, text}] (clip time), timed like ``build_ass``
    and kept inside the clip, so saving them unchanged is always valid."""
    style = PRESETS.get(preset, PRESETS["dynamic"])
    limit = duration if duration is not None else math.inf
    groups: list[list[CaptionWord]] = []
    for g in group_words(sorted((w for w in words if w.start < limit - MIN_CUE), key=lambda w: w.start), style):
        if groups and g[0].start - groups[-1][0].start < MIN_CUE:  # two people at once: one caption
            groups[-1] = groups[-1] + g
        else:
            groups.append(g)
    cues: list[dict] = []
    for gi, group in enumerate(groups):
        start = group[0].start
        end = _group_end(groups, gi)
        if gi + 1 < len(groups):
            end = min(end, groups[gi + 1][0].start)
        end = min(max(end, start + MIN_CUE), limit)
        cues.append({"start": round(start, 2), "end": round(end, 2), "text": " ".join(w.text for w in group)})
    return cues


def validate_cues(cues: list[dict], duration: float) -> list[dict]:
    """Clean and check edited cues: text present, 0 <= start < end <= clip duration, no overlaps."""
    if len(cues) > MAX_CUES:
        raise CaptionError(f"Maximaal {MAX_CUES} captions per clip")
    out = []
    for c in cues:
        text = " ".join(str(c.get("text") or "").split())
        try:
            start, end = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError) as e:
            raise CaptionError("Elke caption heeft een starttijd en een eindtijd nodig") from e
        if not (math.isfinite(start) and math.isfinite(end)):
            raise CaptionError("Ongeldige tijd")
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    out.sort(key=lambda c: (c["start"], c["end"]))
    for i, c in enumerate(out, 1):
        if not c["text"]:
            raise CaptionError(f"Caption {i} is leeg: typ tekst of verwijder de caption")
        if len(c["text"]) > MAX_CUE_CHARS:
            raise CaptionError(f"Caption {i} is te lang (max. {MAX_CUE_CHARS} tekens): splits hem op")
        if c["start"] < 0:
            raise CaptionError(f"Caption {i} begint vóór het begin van de clip")
        if c["end"] > duration + 0.05:
            raise CaptionError(f"Caption {i} eindigt na het einde van de clip ({duration:.1f} s)")
        if c["end"] - c["start"] < MIN_CUE - 1e-6:
            raise CaptionError(f"Caption {i}: de eindtijd moet na de starttijd liggen")
    for i in range(1, len(out)):
        prev, cur = out[i - 1], out[i]
        if prev["end"] > cur["start"]:
            if prev["end"] - cur["start"] > 0.15 or cur["start"] - prev["start"] < MIN_CUE - 1e-6:
                raise CaptionError(f"Caption {i} en {i + 1} overlappen: laat {i} eindigen vóór {i + 1} begint")
            prev["end"] = cur["start"]  # a hair of overlap (rounding): trim silently
    return out


def cues_to_groups(cues: list[dict], words: list[CaptionWord]) -> list[list[CaptionWord]]:
    """Edited cues -> word groups for ``build_ass``. Each cue becomes one caption shown from its start to its
    end. Its words keep their spoken timing when the cue still has as many words as were spoken in it (e.g. a
    spelling fix), so the karaoke highlight stays in sync; otherwise the cue's time is shared by word length."""
    groups: list[list[CaptionWord]] = []
    for cue in cues:
        tokens = str(cue.get("text") or "").split()
        s, e = float(cue["start"]), float(cue["end"])
        if not tokens or e <= s:
            continue
        spoken = [w for w in words if s - 0.05 <= (w.start + w.end) / 2 <= e + 0.05]
        if len(spoken) == len(tokens):
            group = [CaptionWord(min(max(w.start, s), e), min(max(w.end, s), e), t) for w, t in zip(spoken, tokens, strict=True)]
        else:
            total = sum(len(t) + 1 for t in tokens)
            group, t0 = [], s
            for tok in tokens:
                d = (e - s) * (len(tok) + 1) / total
                group.append(CaptionWord(t0, t0 + d, tok))
                t0 += d
        group[0].start, group[-1].end = s, e
        groups.append(group)
    return groups
