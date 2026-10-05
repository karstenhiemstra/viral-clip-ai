"""Caption engine: word-timed ASS subtitles burned in by ffmpeg/libass.

The look is the CapCut template the user chose (the reference screenshot is the source of truth):
* Poppins ExtraBold, UPPERCASE, white with a black outline and a soft drop shadow;
* one line of up to ~4 words, centred horizontally at 76% of the frame height (two lines if a line would
  get too wide);
* the word that is being spoken sits on a solid sky-blue box (slightly rounded corners) that jumps from
  word to word in sync with the speech; the text on it stays white with its black outline;
* no other colours, no pop/scale animation.

libass cannot draw a box behind one word of a line, so every word is its own event, placed with the
font's advance widths (``assets/fonts/Poppins-ExtraBold.metrics.json``), and the box is a vector drawing
at exactly that place - text and box always line up.

The "Blurred achtergrond" layout (sharp video in the middle, blurred copy above and below) has its own look,
after the user's reference: the caption is a label in the upper blurred band - black Poppins Bold in normal
case on a white box with rounded corners, one box per line, no word highlight (``LABEL``, chosen with
``caption_preset_for``).

Captions are a transcription in the spoken language (see ``app.ai.language``); this module only formats
and times them. The user can edit the captions of a clip: the automatic grouping is offered as a list of
cues ({start, end, text} in clip time, see ``auto_cues``), edited cues are stored on the clip and turned
back into word groups for the same style and highlight (``cues_to_groups``).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings

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
    metrics: str  # advance-width table of the font in assets/fonts
    size: int  # ASS font size (libass: = winAscent + winDescent of the font, in script pixels)
    color: str
    outline_color: str
    outline: float
    shadow: float
    shadow_color: str
    shadow_alpha: int
    box_color: str  # box behind the spoken word
    box_pad_x: float  # horizontal padding of the box, in em
    box_height: float  # box height as a multiple of the cap height
    box_radius: float  # px
    uppercase: bool
    max_words: int
    max_chars: int
    max_line_width: float  # fraction of the frame width
    word_gap: float  # extra space between words (advance), in em
    line_gap: float  # px between two lines
    y: float  # vertical centre of the captions, fraction of the frame height
    label: bool = False  # a white box behind each whole line (no word highlight), see LABEL
    bold: bool = False  # ASS bold flag (selects the Bold face of a font family)


CAPCUT = CaptionStyle(
    name="capcut", font="Poppins ExtraBold", metrics="Poppins-ExtraBold.metrics.json", size=130,
    color="#FFFFFF", outline_color="#000000", outline=5, shadow=3, shadow_color="#000000", shadow_alpha=0x70,
    box_color="#28A7F0", box_pad_x=0.14, box_height=1.46, box_radius=7, uppercase=True,
    max_words=4, max_chars=22, max_line_width=0.88, word_gap=0.30, line_gap=14, y=0.76,
)
# Captions of the "Blurred achtergrond" layout: a label in the upper blurred band.
LABEL = CaptionStyle(
    name="label", font="Poppins", metrics="Poppins-Bold.metrics.json", size=104, color="#000000",
    outline_color="#FFFFFF", outline=0, shadow=0, shadow_color="#000000", shadow_alpha=0xFF, box_color="#FFFFFF",
    box_pad_x=0.42, box_height=2.05, box_radius=16, uppercase=False, max_words=8, max_chars=38,
    max_line_width=0.84, word_gap=0.0, line_gap=0, y=0.2, label=True, bold=True,
)
PRESETS: dict[str, CaptionStyle] = {"capcut": CAPCUT, "label": LABEL}
# Clips and settings from before the CapCut style: they now get the CapCut style too.
LEGACY_PRESETS = ("dynamic", "bold_white", "minimal")


def get_style(preset: str | None) -> CaptionStyle | None:
    """The caption style for a clip's preset; None = no captions."""
    if preset == "none":
        return None
    return PRESETS.get(preset or "capcut", CAPCUT)


def caption_preset_for(preset: str | None, layout: str | None) -> str:
    """The caption style that is rendered: the user's preset, except that the "Blurred achtergrond" layout
    (fit_blur) has its own label look. "none" always means no captions."""
    if preset == "none":
        return "none"
    return "label" if layout == "fit_blur" else (preset or "capcut")


def blurred_positions(fg_height: int) -> tuple[int, int]:
    """(caption centre y, title centre y) for the Blurred layout: in the upper blurred band above the
    sharp video (``fg_height`` px high, centred). A source that fills the frame has no band: near the top."""
    top = (PLAY_H - fg_height) / 2
    if top < 300:
        return int(PLAY_H * LABEL.y), int(PLAY_H * 0.07)
    return int(top * 0.62), int(top * 0.26)


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
    """Vertical centre of the captions in the 1080x1920 frame: where the reference template has them (76%),
    or between the two halves of a split screen."""
    if layout == "split":
        return int(PLAY_H * 0.5)
    return int(PLAY_H * CAPCUT.y)


def _display_text(w: CaptionWord, style: CaptionStyle) -> str:
    t = w.text.strip().strip(".,;:…")  # the template shows words without trailing punctuation (keeps ? and !)
    return t.upper() if style.uppercase else t


@lru_cache(maxsize=4)
def _font_metrics(name: str) -> dict:
    return json.loads((get_settings().fonts_dir / name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class _Font:
    em: float  # px per em at the style's ASS size
    cap: float  # cap height in px
    cap_shift: float  # caps centre relative to an \an5 position (px, positive = higher)
    advances: dict
    default: float
    upm: int

    def width(self, text: str) -> float:
        return sum(self.advances.get(c, self.default) for c in text) / self.upm * self.em


def _font(style: CaptionStyle) -> _Font:
    m = _font_metrics(style.metrics)
    upm = m["units_per_em"]
    em = style.size * upm / (m["win_ascent"] + m["win_descent"])
    shift = (m["cap_height"] / 2 - (m["win_ascent"] - m["win_descent"]) / 2) / upm * em
    return _Font(em, m["cap_height"] / upm * em, shift, m["advances"], m["default_advance"], upm)


def _box(x: float, y: float, w: float, h: float, r: float) -> str:
    """Rounded rectangle as an ASS vector drawing at top-left (x, y)."""
    w, h, r = round(w), round(h), round(min(r, w / 2, h / 2))
    path = (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")
    return f"{{\\an7\\pos({round(x)},{round(y)})\\bord0\\shad0\\p1}}{path}{{\\p0}}"


def _layout(texts: list[str], style: CaptionStyle, font: _Font, y: int) -> list[tuple[float, float, float, float]]:
    """(centre x, caps centre y, width, scale) per word: lines of at most ``max_line_width``, centred."""
    max_w = style.max_line_width * PLAY_W
    gap = style.word_gap * font.em
    widths = [font.width(t) for t in texts]
    lines: list[list[int]] = [[]]
    for i, w in enumerate(widths):
        cur = lines[-1]
        line_w = sum(widths[j] for j in cur) + gap * len(cur)
        if cur and line_w + w > max_w:
            lines.append([i])
        else:
            cur.append(i)
    line_h = font.cap * style.box_height + style.line_gap
    out: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 1.0)] * len(texts)
    for li, line in enumerate(lines):
        total = sum(widths[j] for j in line) + gap * (len(line) - 1)
        scale = min(1.0, max_w / total) if total else 1.0  # one very long word: shrink that line to fit
        cy = y + (li - (len(lines) - 1) / 2) * line_h
        x = PLAY_W / 2 - total * scale / 2
        for j in line:
            out[j] = (x + widths[j] * scale / 2, cy, widths[j] * scale, scale)
            x += (widths[j] + gap) * scale
    return out


def _label_lines(texts: list[str], style: CaptionStyle, font: _Font) -> list[str]:
    """Words -> lines of at most ``max_line_width`` (as the font measures them)."""
    max_w = style.max_line_width * PLAY_W
    lines: list[str] = []
    for t in texts:
        if lines and font.width(lines[-1] + " " + t) <= max_w:
            lines[-1] += " " + t
        else:
            lines.append(t)
    return lines


def _label_events(lines: list[str], start: float, end: float, style: CaptionStyle, font: _Font, y: int) -> list[str]:
    """One white rounded box + one black text per line, the lines centred around ``y``."""
    events: list[str] = []
    bh = font.cap * style.box_height
    line_h = bh + style.line_gap
    for li, line in enumerate(lines):
        width = font.width(line)
        scale = min(1.0, style.max_line_width * PLAY_W / width) if width else 1.0  # one very long word
        cy = y + (li - (len(lines) - 1) / 2) * line_h
        bw = (width + 2 * style.box_pad_x * font.em) * scale
        fs = f"\\fscx{round(scale * 100)}\\fscy{round(scale * 100)}" if scale < 1 else ""
        events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Box,,0,0,0,,{_box(PLAY_W / 2 - bw / 2, cy - bh / 2, bw, bh, style.box_radius)}")
        events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{{\\an5\\pos({PLAY_W / 2:.1f},{cy + font.cap_shift * scale:.1f}){fs}}}{_escape(line)}")
    return events


def build_ass(
    words: list[CaptionWord],
    preset: str,
    *,
    y: int,
    emphasis: list[str] | None = None,
    title: str | None = None,
    title_duration: float = 2.8,
    groups: list[list[CaptionWord]] | None = None,
    title_y: int | None = None,
) -> str:
    """ASS subtitles in the CapCut style. ``groups`` (from ``cues_to_groups``): captions edited by the user,
    shown exactly at their own times; otherwise ``words`` are grouped automatically. ``emphasis`` is accepted
    for compatibility; the template has no extra word colours."""
    style = get_style(preset) or CAPCUT
    font = _font(style)
    shadow = ass_color(style.shadow_color, style.shadow_alpha)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style.font},{style.size},{ass_color(style.color)},{ass_color(style.color)},{ass_color(style.outline_color)},{shadow},{-1 if style.bold else 0},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},5,0,0,0,1
Style: Box,{style.font},{style.size},{ass_color(style.box_color)},{ass_color(style.box_color)},{ass_color(style.box_color)},{ass_color(style.box_color)},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Title,{style.font},64,{ass_color('#111111')},{ass_color('#111111')},{ass_color('#FFFFFF')},{ass_color('#FFFFFF', 0)},{-1 if style.bold else 0},0,0,0,100,100,0,0,3,18,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    exact = groups is not None
    if groups is None:
        groups = group_words(words, style)
    for gi, group in enumerate(groups):
        g_start = group[0].start
        g_end = group[-1].end if exact else _group_end(groups, gi)
        texts = [_display_text(w, style) for w in group]
        if style.label:
            lines = _label_lines([t for t in texts if t], style, font)
            events += _label_events(lines, g_start, g_end, style, font, y) if lines else []
            continue
        places = _layout(texts, style, font, y)
        for i, (w, txt, (cx, cy, width, scale)) in enumerate(zip(group, texts, places, strict=True)):
            if not txt:
                continue
            fs = f"\\fscx{round(scale * 100)}\\fscy{round(scale * 100)}" if scale < 1 else ""
            pos_y = cy + font.cap_shift * scale
            events.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,0,0,0,,{{\\an5\\pos({cx:.1f},{pos_y:.1f}){fs}}}{_escape(txt)}")
            # the spoken word sits on the box, from when it is said until the next word starts
            s = g_start if i == 0 else w.start
            e = group[i + 1].start if i + 1 < len(group) else g_end
            if e - s < 0.04:
                continue
            bw = width + 2 * style.box_pad_x * font.em * scale
            bh = font.cap * style.box_height * scale
            events.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Box,,0,0,0,,{_box(cx - bw / 2, cy - bh / 2, bw, bh, style.box_radius)}")

    if title:
        ty = int(PLAY_H * 0.16) if title_y is None else title_y
        tpos = f"{{\\an5\\pos({PLAY_W // 2},{ty})\\fad(120,200)}}"
        events.append(f"Dialogue: 2,{_ts(0)},{_ts(title_duration)},Title,,0,0,0,,{tpos}{_escape(title[:70])}")
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
    style = get_style(preset) or CAPCUT
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
