"""Speech-to-text with word timestamps + subtitle file parsing.

Captions are a transcription of what is SAID, in the language it is said in - never a translation. So no
language is ever forced on the speech-to-text (not the interface language, not the creator's setting): the
spoken language is detected per piece of audio and mixed-language speech keeps its language per piece.
See ``app.ai.language`` for the language metadata stored with each transcript.

Backends:
* ``openai``         - Whisper API (``whisper-1``, word timestamps). ~$0.006/min.
* ``faster_whisper`` - local, free, CPU/GPU (``pip install '.[local-whisper]'``).
* subtitle upload   - SRT/VTT (incl. YouTube's word-timed VTT) - no audio needed.
"""

from __future__ import annotations

import html
import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import numpy as np
from sqlalchemy.orm import Session

from app.ai.language import UNCERTAIN, Piece, language_report, translate_captions_requested
from app.ai.transcript import Word, interpolate_words
from app.services.settings_store import RuntimeSettings, get_secret
from app.services.usage import record_usage
from app.video import ffmpeg

log = logging.getLogger(__name__)

WHISPER_PRICE_PER_MIN = 0.006
# Whisper picks the language from the first 30 s of a request and then writes everything in it. One piece of
# at most ~30 s per request (cut in a pause) lets every piece keep the language that is actually spoken.
PIECE_SECONDS = 29.0
PIECE_MIN_SECONDS = 18.0
PARALLEL_REQUESTS = 3


class TranscriptionError(RuntimeError):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(self, media: Path, duration: float) -> tuple[list[Word], dict]:
        """(words in the spoken language, language report - see ``app.ai.language.language_report``)."""
        ...


class AudioLevels:
    """Loudness per 50 ms of the whole audio track: where the pauses are, which parts are silent."""

    HOP = 0.05

    def __init__(self, media: Path):
        self.db: np.ndarray | None = None
        try:
            sr = 4000
            pcm = ffmpeg.decode_pcm(media, sr)
            hop = int(sr * self.HOP)
            n = len(pcm) // hop
            if n:
                frames = pcm[: n * hop].astype(np.float64).reshape(n, hop)
                self.db = 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) + 1e-6)
        except ffmpeg.FFmpegError as e:
            log.info("No audio levels (%s); cutting at fixed times", e)

    def quietest(self, lo: float, hi: float) -> float:
        if self.db is None:
            return hi
        a, b = int(lo / self.HOP), int(hi / self.HOP)
        seg = self.db[a:b]
        return (a + int(np.argmin(seg))) * self.HOP if len(seg) else hi

    def silent(self, a: float, b: float) -> bool:
        """Nothing audible at all (never skip quiet speech or speech over music)."""
        if self.db is None or not len(self.db):
            return False
        seg = self.db[int(a / self.HOP): int(b / self.HOP)]
        return len(seg) > 0 and float(seg.max()) < -50.0


def plan_pieces(levels: AudioLevels, duration: float) -> list[tuple[float, float]]:
    """Pieces of at most ~30 s, cut at the quietest moment (a pause between words)."""
    pieces, t = [], 0.0
    while duration - t > PIECE_SECONDS:
        cut = levels.quietest(t + PIECE_MIN_SECONDS, t + PIECE_SECONDS)
        pieces.append((t, cut))
        t = cut
    if duration - t > 0.3:
        pieces.append((t, duration))
    return pieces


def _friendly_openai_error(e: Exception, base_url: object = None) -> str:
    import openai

    from app.ai.llm import connection_error_message

    text = str(e)
    if isinstance(e, openai.APIConnectionError):
        log.warning("Whisper connection failed", exc_info=True)
        return "Whisper: " + connection_error_message("OpenAI", e, base_url)
    if isinstance(e, openai.AuthenticationError):
        return "Whisper: ongeldige OPENAI_API_KEY. Maak een nieuwe key op platform.openai.com → API keys."
    if isinstance(e, openai.RateLimitError) and ("insufficient_quota" in text or "exceeded your current quota" in text.lower()):
        return ("Whisper: je OpenAI-tegoed is op. Voeg tegoed toe op platform.openai.com → Settings → Billing, "
                "of upload ondertitels (.srt) zodat transcriptie niet nodig is.")
    return f"Whisper API fout: {text}"


class OpenAITranscriber:
    name = "openai_whisper"

    def __init__(self, api_key: str, model: str = "whisper-1", base_url: str | None = None):
        from openai import OpenAI

        from app.config import openai_base_url

        self.client = OpenAI(api_key=api_key, base_url=base_url or openai_base_url(), max_retries=4)
        self.model = model

    def _piece(self, media: Path, tmp: Path, tag: str, start: float, end: float) -> tuple[list[Word], Piece]:
        audio = ffmpeg.extract_audio(media, tmp / f"piece-{tag}.mp3", start=start, duration=end - start)
        with open(audio, "rb") as f:
            try:
                # No `language`: Whisper detects what is spoken in this piece and writes it down as said.
                resp = self.client.audio.transcriptions.create(
                    model=self.model, file=f, response_format="verbose_json", timestamp_granularities=["word", "segment"]
                )
            except Exception as e:  # SDK raises typed errors; surface a readable message
                raise TranscriptionError(_friendly_openai_error(e, self.client.base_url)) from e
        minutes = (end - start) / 60
        record_usage("openai", "transcription", units=minutes, cost_usd=minutes * WHISPER_PRICE_PER_MIN)
        words: list[Word] = []
        for w in getattr(resp, "words", None) or []:
            text = (w.word if hasattr(w, "word") else w["word"]).strip()
            ws = float(w.start if hasattr(w, "start") else w["start"])
            we = float(w.end if hasattr(w, "end") else w["end"])
            if text:
                words.append(Word(ws + start, we + start, text))
        # Whisper's word list has no punctuation; borrow it from the segments for better sentence splits.
        _restore_punctuation(words, getattr(resp, "segments", None) or [], start)
        return words, Piece(end - start, getattr(resp, "language", None), " ".join(w.text for w in words))

    def transcribe(self, media: Path, duration: float) -> tuple[list[Word], dict]:
        levels = AudioLevels(media)
        pieces = [p for p in plan_pieces(levels, max(duration, 1.0)) if not levels.silent(*p)]
        with tempfile.TemporaryDirectory(prefix="vc-stt-") as tmp, ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
            done = list(pool.map(lambda ip: self._piece(media, Path(tmp), str(ip[0]), *ip[1]), enumerate(pieces)))
            results: list[list[tuple[list[Word], Piece]]] = [[r] for r in done]
            # Not sure which language a piece is in? Listen closer before deciding: transcribe its two halves
            # separately (each gets its own language detection) and keep that when it is clearer.
            retried = 0
            for i, ((a, b), (_words, piece)) in enumerate(zip(pieces, done, strict=True)):
                if piece.certainty()[1] >= UNCERTAIN or b - a < 10:
                    continue
                mid = levels.quietest(a + 0.35 * (b - a), a + 0.65 * (b - a))
                halves = [self._piece(media, Path(tmp), f"{i}a", a, mid), self._piece(media, Path(tmp), f"{i}b", mid, b)]
                retried += 1
                if min(h[1].certainty()[1] for h in halves) > piece.certainty()[1]:
                    results[i] = halves
        words = [w for r in results for ws, _ in r for w in ws]
        report = language_report([p for r in results for _, p in r], "whisper-per-piece", translate_captions_requested())
        report["pieces"] = len(pieces)
        report["rechecked_pieces"] = retried
        return words, report


class FasterWhisperTranscriber:
    name = "faster_whisper"
    _model_cache: dict[str, object] = {}

    def __init__(self, model_size: str = "small"):
        try:
            from faster_whisper import WhisperModel  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise TranscriptionError("faster-whisper is niet geïnstalleerd: pip install '.[local-whisper]'") from e
        self.model_size = model_size

    def _model(self):  # pragma: no cover - heavy
        from faster_whisper import WhisperModel

        if self.model_size not in self._model_cache:
            self._model_cache[self.model_size] = WhisperModel(self.model_size, device="auto", compute_type="int8")
        return self._model_cache[self.model_size]

    def transcribe(self, media: Path, duration: float) -> tuple[list[Word], dict]:  # pragma: no cover - heavy
        # task="transcribe" (never "translate"), no forced language; `multilingual` re-detects the language
        # for every 30 s window (faster-whisper >= 1.1), so mixed speech keeps its own language.
        kwargs = {"language": None, "task": "transcribe", "word_timestamps": True, "vad_filter": True}
        try:
            segments, info = self._model().transcribe(str(media), multilingual=True, **kwargs)
        except TypeError:
            segments, info = self._model().transcribe(str(media), **kwargs)
        words: list[Word] = []
        pieces: list[Piece] = []
        for seg in segments:
            seg_words = [Word(float(w.start), float(w.end), w.word.strip()) for w in seg.words or [] if w.word.strip()]
            words.extend(seg_words)
            pieces.append(Piece(float(seg.end) - float(seg.start), getattr(info, "language", None),
                                " ".join(w.text for w in seg_words), getattr(info, "language_probability", None)))
        return words, language_report(pieces, "faster-whisper", translate_captions_requested())


def _restore_punctuation(words: list[Word], segments, offset: float) -> None:
    """Attach sentence-final punctuation from segment text to the last word of each segment."""
    for seg in segments:
        text = (seg.text if hasattr(seg, "text") else seg.get("text", "")).strip()
        end = float(seg.end if hasattr(seg, "end") else seg.get("end", 0)) + offset
        if not text or text[-1] not in ".?!…,":
            continue
        best = None
        for w in reversed(words):
            if w.end <= end + 0.35:
                best = w
                break
        if best is not None and best.text[-1:] not in ".?!…,":
            best.text += text[-1]


def get_transcriber(db: Session | None, rs: RuntimeSettings) -> Transcriber | None:
    choice = rs.ai.transcriber
    openai_key = get_secret(db, "openai_api_key")
    if choice == "none":
        return None
    if choice in ("openai", "auto") and openai_key:
        from app.config import openai_base_url

        return OpenAITranscriber(openai_key, rs.ai.whisper_model or "whisper-1", openai_base_url())
    if choice in ("faster_whisper", "auto"):
        try:
            return FasterWhisperTranscriber(rs.ai.faster_whisper_model or "small")
        except TranscriptionError:
            if choice == "faster_whisper":
                raise
    if choice == "openai":
        raise TranscriptionError("OpenAI transcriptie gekozen maar OPENAI_API_KEY ontbreekt")
    return None


# --- subtitle parsing -------------------------------------------------------------------------------

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(_TS + r"\s*-->\s*" + _TS)
_INLINE_TS = re.compile(r"<" + _TS + r">")
_TAG_RE = re.compile(r"</?[^>]+>")


def _ts(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str, filename: str = "") -> tuple[list[Word], str | None]:
    """Parse SRT or WebVTT into words. Uses VTT inline word timings (YouTube style) when present,
    removes the rolling duplicate lines of auto-generated captions, and interpolates otherwise."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    language = None
    m = re.search(r"^Language:\s*([a-zA-Z-]+)", text, re.MULTILINE)
    if m:
        language = m.group(1)[:2].lower()
    blocks = re.split(r"\n\n+", text)  # whitespace-only lines inside a cue are not cue separators
    cues: list[tuple[float, float, str]] = []
    timed_words: list[Word] = []
    prev_last_line = ""
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        ts_line_idx = next((i for i, ln in enumerate(lines) if _CUE_RE.search(ln)), None)
        if ts_line_idx is None:
            continue
        mm = _CUE_RE.search(lines[ts_line_idx])
        start = _ts(*mm.groups()[:4])
        end = _ts(*mm.groups()[4:])
        body = lines[ts_line_idx + 1 :]
        if not body:
            continue
        # YouTube VTT: inline <00:00:01.240><c> word</c> timing on the new line.
        inline_line = next((ln for ln in body if _INLINE_TS.search(ln)), None)
        if inline_line is not None:
            timed_words.extend(_parse_inline_words(inline_line, start, end))
            prev_last_line = _TAG_RE.sub("", body[-1]).strip()
            continue
        clean = [html.unescape(_TAG_RE.sub("", ln)).strip() for ln in body]
        clean = [ln for ln in clean if ln]
        if clean and prev_last_line and clean[0] == prev_last_line:
            clean = clean[1:]  # rolling duplicate
        if not clean:
            continue
        prev_last_line = clean[-1]
        cue_text = " ".join(clean)
        cue_text = re.sub(r"\[(?:muziek|music|applaus|applause)\]", "", cue_text, flags=re.IGNORECASE).strip()
        if cue_text:
            cues.append((start, end, cue_text))
    if timed_words:
        timed_words.sort(key=lambda w: w.start)
        return timed_words, language
    return interpolate_words(cues), language


def _parse_inline_words(line: str, cue_start: float, cue_end: float) -> list[Word]:
    tokens: list[tuple[float, str]] = []
    pos = 0
    current_t = cue_start
    for m in _INLINE_TS.finditer(line):
        seg = line[pos : m.start()]
        if seg.strip():
            tokens.append((current_t, seg))
        current_t = _ts(*m.groups())
        pos = m.end()
    if line[pos:].strip():
        tokens.append((current_t, line[pos:]))
    words: list[Word] = []
    for j, (t, raw) in enumerate(tokens):
        txt = html.unescape(_TAG_RE.sub("", raw)).strip()
        if not txt:
            continue
        nxt = tokens[j + 1][0] if j + 1 < len(tokens) else cue_end
        for k, tok in enumerate(txt.split()):
            words.append(Word(t + k * 0.01, max(t + 0.05, nxt - 0.02), tok))
    return words
