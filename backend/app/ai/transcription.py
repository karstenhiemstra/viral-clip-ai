"""Speech-to-text with word timestamps + subtitle file parsing.

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
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from app.ai.transcript import Word, interpolate_words
from app.services.settings_store import RuntimeSettings, get_secret
from app.services.usage import record_usage
from app.video import ffmpeg

log = logging.getLogger(__name__)

WHISPER_PRICE_PER_MIN = 0.006
CHUNK_SECONDS = 20 * 60


class TranscriptionError(RuntimeError):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(self, media: Path, language: str | None, duration: float) -> tuple[list[Word], str | None]: ...


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

        self.client = OpenAI(api_key=api_key, base_url=base_url or openai_base_url())
        self.model = model

    def transcribe(self, media: Path, language: str | None, duration: float) -> tuple[list[Word], str | None]:
        words: list[Word] = []
        detected: str | None = None
        with tempfile.TemporaryDirectory(prefix="vc-stt-") as tmp:
            offset = 0.0
            idx = 0
            total = max(duration, 1.0)
            while offset < total - 0.5:
                chunk_len = min(CHUNK_SECONDS, total - offset)
                audio = ffmpeg.extract_audio(media, Path(tmp) / f"chunk{idx}.mp3", start=offset, duration=chunk_len)
                with open(audio, "rb") as f:
                    kwargs = {
                        "model": self.model,
                        "file": f,
                        "response_format": "verbose_json",
                        "timestamp_granularities": ["word", "segment"],
                    }
                    if language:
                        kwargs["language"] = language
                    try:
                        resp = self.client.audio.transcriptions.create(**kwargs)
                    except Exception as e:  # SDK raises typed errors; surface a readable message
                        raise TranscriptionError(_friendly_openai_error(e, self.client.base_url)) from e
                record_usage(
                    "openai", "transcription", units=chunk_len / 60, cost_usd=chunk_len / 60 * WHISPER_PRICE_PER_MIN
                )
                detected = detected or getattr(resp, "language", None)
                for w in getattr(resp, "words", None) or []:
                    text = (w.word if hasattr(w, "word") else w["word"]).strip()
                    start = float(w.start if hasattr(w, "start") else w["start"])
                    end = float(w.end if hasattr(w, "end") else w["end"])
                    if text:
                        words.append(Word(start + offset, end + offset, text))
                # Whisper's word list has no punctuation; borrow it from the segments for better sentence splits.
                _restore_punctuation(words, getattr(resp, "segments", None) or [], offset)
                offset += chunk_len
                idx += 1
        return words, _lang_code(detected)


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

    def transcribe(self, media: Path, language: str | None, duration: float) -> tuple[list[Word], str | None]:  # pragma: no cover
        segments, info = self._model().transcribe(
            str(media), language=language or None, word_timestamps=True, vad_filter=True
        )
        words: list[Word] = []
        for seg in segments:
            for w in seg.words or []:
                if w.word.strip():
                    words.append(Word(float(w.start), float(w.end), w.word.strip()))
        return words, _lang_code(getattr(info, "language", None))


def _lang_code(lang: str | None) -> str | None:
    if not lang:
        return None
    lang = lang.lower()
    names = {"dutch": "nl", "english": "en", "german": "de", "french": "fr", "flemish": "nl"}
    return names.get(lang, lang[:2])


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
