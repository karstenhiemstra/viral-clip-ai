"""The analysis pipeline: long video -> ranked, de-duplicated, hook-first clip plans.

    transcript (Whisper / SRT, only if missing)
      -> sentence units + audio loudness + scene cuts + audience hotspots   (free, local)
      -> PASS 1: signal windows  +  LLM on the full transcript (cheap model) stage 1
      -> PASS 2: merge + dedupe + shortlist top N (free, local)
      -> PASS 3: smart model judges ONLY the shortlist: viewer simulation,
         dimension scores, climax, verdict, better edit           stages 2-7
      -> hook-first boundary optimisation + dead-air removal                stage 10
      -> funnel Viral Score (+ signal blend, penalties, crowd, personal)    stage 9
      -> optional vision pass on the best few                               pass 5
      -> duplicate detection + MMR diversity -> top K                        stage 8/9
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.ai.boundaries import REJECT_FLAGS, ClipWindow, DurationRules, optimize_boundaries
from app.ai.candidates import Candidate, llm_candidates, merge_candidates, signal_candidates
from app.ai.dedupe import mmr_select, similarity_matrix
from app.ai.evaluator import (
    emphasis_from_text,
    evaluate_heuristic,
    evaluate_llm,
    heuristic_flags,
    heuristic_packaging,
)
from app.ai.llm import LLMClient, LLMFatalError, UsageMeter
from app.ai.scoring import compute_viral_score
from app.ai.signals import (
    VideoContext,
    feature_vector,
    heuristic_dimension_scores,
    signal_score,
    topic_breaks,
    window_features,
)
from app.ai.transcript import Word, segment_sentences, words_from_rows
from app.ai.transcription import TranscriptionError, get_transcriber
from app.ai.vision import LLMVisionAnalyzer, blend_vision
from app.models import Creator, Transcript, Video
from app.services import learning
from app.services.queue import JobWaiting
from app.services.settings_store import CLIP_MAX_SECONDS, CLIP_MIN_SECONDS, RuntimeSettings
from app.services.storage import get_storage
from app.video import ffmpeg
from app.video.audio_features import AudioProfile, load_profile
from app.video.scenes import detect_scene_cuts

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]


class AnalysisError(RuntimeError):
    pass


@dataclass
class ClipPlan:
    cid: str
    window: ClipWindow
    words: list[Word]
    text: str
    scores: dict[str, float]
    stage_scores: dict[str, float]
    viral_score: float
    breakdown: dict[str, Any]
    features: dict[str, float]
    signals: dict[str, Any]
    title: str
    why: str
    hook_line: str
    category: str
    flags: list[str]
    emphasis_words: list[str]
    sources: list[str]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisOutput:
    plans: list[ClipPlan]
    candidates_log: list[dict[str, Any]]
    stage_log: list[dict[str, Any]]
    meter: UsageMeter
    provider: str
    models: dict[str, str]
    warnings: list[str]


# The AI's own verdict that nothing pays off (or the payoff falls outside the clip): rejected too.
AI_REJECT_FLAGS = ("no_climax", "ends_before_payoff")
REJECT_LABELS_NL = {
    "hook_late": "hook niet in de eerste 2 seconden",
    "incomplete_story": "climax of reactie past niet in de clip",
    "too_short": "korter dan de minimale duur",
    "no_climax": "geen duidelijke payoff",
    "ends_before_payoff": "stopt vóór de payoff",
}


def duration_rules(rs: RuntimeSettings, creator: Creator | None) -> DurationRules:
    c = rs.clips
    lo = creator.clip_min_seconds if creator and creator.clip_min_seconds else c.min_seconds
    hi = creator.clip_max_seconds if creator and creator.clip_max_seconds else c.max_seconds
    lo = min(max(lo, CLIP_MIN_SECONDS), CLIP_MAX_SECONDS)  # clips are always 10-15 s
    hi = min(max(hi, lo), CLIP_MAX_SECONDS)
    target = min(max(c.target_seconds, lo), hi) if not (creator and (creator.clip_min_seconds or creator.clip_max_seconds)) else (lo + hi) / 2
    return DurationRules(
        min_seconds=lo, max_seconds=hi, target_seconds=target, remove_silences=c.remove_silences,
        silence_min_gap=c.silence_min_gap,
    )


def media_path(video: Video) -> Path | None:
    if not video.media_key:
        return None
    p = get_storage().local_path(video.media_key)
    return p if p.exists() else None


def ensure_transcript(db: Session, video: Video, creator: Creator | None, rs: RuntimeSettings, progress: Progress) -> list[Word]:
    if video.transcript is not None and video.transcript.words:
        return words_from_rows(video.transcript.words)
    media = media_path(video)
    if media is None:
        raise JobWaiting(
            "Wacht op bronmateriaal: upload het videobestand (of zet het in de inbox-map) of upload een "
            "SRT/VTT-transcript. ViralClip downloadt bewust niets van YouTube."
        )
    transcriber = get_transcriber(db, rs)
    if transcriber is None:
        raise JobWaiting(
            "Bronvideo ontvangen. Voor de transcriptie is een OpenAI API key nodig: vul hem in bij Settings → "
            "API keys, dan start de analyse vanzelf. Of upload ondertitels (.srt).",
            reason="api_key",
        )
    duration = float(video.media_meta.get("duration") or video.duration_seconds or 0)
    progress(5, f"Transcriberen met {transcriber.name} ({duration / 60:.0f} min audio)")
    # No language is passed on purpose: the spoken language is detected (per piece of audio) and the words
    # are written down as they are said. The interface or creator language never changes the captions.
    try:
        words, report = transcriber.transcribe(media, duration)
    except TranscriptionError:
        raise
    except Exception as e:  # pragma: no cover - backend specific
        raise TranscriptionError(str(e)) from e
    if not words:
        raise AnalysisError("De transcriptie is leeg (geen spraak gevonden?)")
    log.info("Spoken language of video %s: %s", video.id, report)
    tr = Transcript(
        video_id=video.id,
        source=transcriber.name,
        language=report.get("caption_language"),
        language_info=report,
        words=[w.to_row() for w in words],
        full_text=" ".join(w.text for w in words),
        word_timing="exact",
    )
    db.add(tr)
    db.commit()
    return words


def analyze_video(
    db: Session,
    video: Video,
    creator: Creator | None,
    rs: RuntimeSettings,
    *,
    llm: LLMClient | None,
    progress: Progress | None = None,
) -> AnalysisOutput:
    report: Progress = progress or (lambda pct, msg: None)
    stage_log: list[dict[str, Any]] = []
    warnings: list[str] = []
    meter = UsageMeter()
    t0 = time.monotonic()

    def mark(stage: str, **detail: Any) -> None:
        nonlocal t0
        stage_log.append({"stage": stage, "seconds": round(time.monotonic() - t0, 2), **detail})
        t0 = time.monotonic()

    # 1. transcript -------------------------------------------------------------------------------
    report(2, "Transcript ophalen")
    words = ensure_transcript(db, video, creator, rs, report)
    sentences = segment_sentences(words)
    if len(sentences) < 3:
        raise AnalysisError("Transcript is te kort om clips uit te halen")
    mark("transcript", words=len(words), sentences=len(sentences))

    # 2. local signals ----------------------------------------------------------------------------
    media = media_path(video)
    audio: AudioProfile | None = None
    cuts: list[float] = []
    if media is not None:
        report(25, "Audio-analyse (volume, reacties, stiltes)")
        try:
            audio = load_profile(media)
        except ffmpeg.FFmpegError as e:
            warnings.append(f"Audio-analyse mislukt: {e}")
        if rs.pipeline.scene_detection and video.media_meta.get("has_video", True):
            report(30, "Camerawissels detecteren")
            try:
                cuts = detect_scene_cuts(media)
            except ffmpeg.FFmpegError as e:
                warnings.append(f"Scènedetectie mislukt: {e}")
    duration = float(video.media_meta.get("duration") or video.duration_seconds or words[-1].end)
    ctx = VideoContext(
        words=words,
        sentences=sentences,
        duration=duration,
        audio=audio,
        scene_cuts=cuts,
        hotspots=list(video.crowd_hotspots or []),
        title=video.title or "",
        creator=(creator.name if creator else video.channel_title) or "",
        language=(creator.language if creator else None) or video.language or "nl",
        topic_breaks=topic_breaks(sentences, cuts),
    )
    mark("signals", audio=audio is not None, scene_cuts=len(cuts), hotspots=len(ctx.hotspots),
         topic_changes=sum(1 for b in ctx.topic_breaks if b >= 0.6))

    # 3. candidates ---------------------------------------------------------------------------------
    rules = duration_rules(rs, creator)
    count = rs.pipeline.candidate_count
    lang = rs.ai.output_language
    report(35, "Potentiële momenten zoeken")
    sig = signal_candidates(ctx, count=count, min_s=rules.min_seconds, max_s=rules.max_seconds, target_s=rules.target_seconds)
    llm_c: list[Candidate] = []
    if llm is not None:
        # Pass 1 (cheap "fast" model): read the whole transcript in chunks and propose moments.
        try:
            llm_c = llm_candidates(
                llm, ctx, target_count=count, min_s=rules.min_seconds, max_s=rules.max_seconds, output_language=lang,
                meter=meter, progress=lambda f: report(35 + 15 * f, "Pass 1 (goedkoop model): momenten zoeken"),
                warnings=warnings,
            )
        except LLMFatalError as e:
            warnings.append(f"AI uitgeschakeld voor deze analyse: {e}")
            llm = None
    # Pass 2 (free, local): merge AI moments with signal-based ones, dedupe, keep the best `count`.
    cands = merge_candidates(ctx, [llm_c, sig])
    shortlist = cands[:count]
    mark("pass2_shortlist", signal=len(sig), llm=len(llm_c), merged=len(cands), shortlist=len(shortlist))

    # Pass 3 (smart model): detailed evaluation of ONLY the shortlisted candidates.
    evaluated_by_llm = 0
    if llm is not None and shortlist:
        report(50, f"Pass 3 (slim model): {len(shortlist)} kandidaten beoordelen")
        try:
            evaluated_by_llm = evaluate_llm(
                llm, ctx, shortlist, min_s=rules.min_seconds, max_s=rules.max_seconds, target_s=rules.target_seconds,
                output_language=lang, batch_size=rs.pipeline.detail_batch_size, meter=meter,
                progress=lambda f: report(50 + 30 * f, "Pass 3 (slim model): kandidaten beoordelen"), warnings=warnings,
            )
        except LLMFatalError as e:
            warnings.append(f"AI uitgeschakeld voor deze analyse: {e}")
            llm = None
        if evaluated_by_llm == 0:
            warnings.append("AI-beoordeling (pass 3) niet gelukt; heuristische scoring gebruikt")
    evaluate_heuristic(ctx, shortlist)
    mark("pass3_evaluation", llm_evaluated=evaluated_by_llm, heuristic=len(shortlist) - evaluated_by_llm)

    # 5. boundaries + scoring -----------------------------------------------------------------------
    report(82, "Clipgrenzen optimaliseren en scoren")
    model = learning.active_model(db) if rs.scoring.personalization_strength > 0 else None
    plans: list[ClipPlan] = []
    rejected: dict[str, list[str]] = {}
    for c in shortlist:
        ev = c.evaluation or {}
        llm_mode = ev.get("mode") == "llm"
        window = optimize_boundaries(
            ctx, ev.get("s0", c.s0), ev.get("s1", c.s1), rules, hook_s=ev.get("hook_s") if llm_mode else c.hook_s,
            climax_s=ev.get("climax_s") if llm_mode else None, reaction_s=ev.get("reaction_s") if llm_mode else None,
            heuristic=not llm_mode,
        )
        clip_words = ctx.words[window.w0 : window.w1]
        feats = window_features(ctx, window.start, window.end, clip_words)
        sig_score = signal_score(feats)
        dims = dict(ev.get("scores") or {}) if llm_mode else heuristic_dimension_scores(feats)
        # Heuristic flags describe the FINAL window (boundaries may have moved); LLM flags are kept.
        base_flags = (ev.get("flags") or []) if llm_mode else heuristic_flags(feats)
        flags = set(base_flags) | set(window.flags)
        if feats.get("payoff_after_end"):  # the audio: the big reaction comes right after the cut
            flags.add("ends_before_payoff")
        flags = sorted(flags)
        # Not a complete mini conversation within the limits (hook too late, payoff/reaction left out, too
        # short) or, according to the AI, no payoff at all: rejected, whatever its score.
        reasons = [f for f in flags if f in REJECT_FLAGS or (llm_mode and f in AI_REJECT_FLAGS)]
        if reasons:
            rejected[c.cid] = reasons
            continue
        if not llm_mode:  # packaging must describe the final (possibly moved) window
            ev = {**ev, **heuristic_packaging(ctx, clip_words, feats, c.category)}
        category = ev.get("category") or c.category or "other"
        fv = feature_vector(feats, dims)
        adj = learning.personal_adjustment(model, fv, category, rs.scoring.personalization_strength)
        res = compute_viral_score(
            dims,
            weights=rs.scoring.weights,
            stage_weights=rs.scoring.stage_weights,
            signal=sig_score,
            signal_blend=rs.scoring.signal_blend if llm_mode else 0.0,
            flags=flags,
            verdict=ev.get("verdict"),
            crowd=feats.get("crowd", 0.0),
            personal_adjustment=adj,
        )
        text = " ".join(w.text for w in clip_words)
        plans.append(
            ClipPlan(
                cid=c.cid,
                window=window,
                words=clip_words,
                text=text,
                scores=dims,
                stage_scores=res.stage_scores,
                viral_score=res.viral_score,
                breakdown=res.breakdown | {"mode": ev.get("mode")},
                features=fv,
                signals={
                    "signal_score": sig_score,
                    "audio_peak_z": feats.get("audio_peak_z"),
                    "silence_ratio": feats.get("audio_silence_ratio"),
                    "crowd": feats.get("crowd"),
                    "speech_rate": feats.get("speech_rate"),
                    "time_to_strong": feats.get("hook_time_to_strong"),
                    "starts_filler": feats.get("hook_starts_filler"),
                    "scene_cuts_per_10s": feats.get("scene_cuts_per_10s"),
                },
                title=ev.get("title") or text[:70],
                why=ev.get("why") or "",
                hook_line=ev.get("hook_line") or text[:120],
                category=category,
                flags=flags,
                emphasis_words=emphasis_from_text(text, ev.get("emphasis_words") or []),
                sources=sorted(c.sources),
                extra={
                    "first_seconds": ev.get("first_seconds"),
                    "viewer_reaction": ev.get("viewer_reaction"),
                    "verdict": ev.get("verdict"),
                    "llm_description": c.description,
                },
            )
        )
    mark("scoring", plans=len(plans), rejected=len(rejected))
    if rejected:
        counts: dict[str, int] = {}
        for reasons in rejected.values():
            for r in reasons:
                counts[r] = counts.get(r, 0) + 1
        warnings.append(
            f"{len(rejected)} kandidaat-clip(s) afgewezen omdat ze geen compleet mini-gesprek van "
            f"{rules.min_seconds:g}-{rules.max_seconds:g} s vormen: "
            + ", ".join(f"{REJECT_LABELS_NL.get(k, k)} ({v})" for k, v in sorted(counts.items()))
        )

    # 6. optional vision pass on the best few ---------------------------------------------------------
    if rs.pipeline.use_vision and llm is not None and media is not None and video.media_meta.get("has_video", True):
        report(88, "Vision-analyse van de beste kandidaten")
        analyzer = LLMVisionAnalyzer(llm, meter)
        for p in sorted(plans, key=lambda p: p.viral_score, reverse=True)[: rs.pipeline.vision_top_n]:
            vis = analyzer.analyze(media, p.window.start, p.window.end, p.text)
            if vis:
                p.scores = blend_vision(p.scores, vis)
                p.extra["vision"] = vis
                p.features["visual_interest"] = vis["visual_interest"] / 100
                res = compute_viral_score(
                    p.scores, weights=rs.scoring.weights, stage_weights=rs.scoring.stage_weights,
                    signal=p.signals["signal_score"],
                    signal_blend=rs.scoring.signal_blend if p.breakdown.get("mode") == "llm" else 0.0,
                    flags=p.flags, verdict=p.extra.get("verdict"), crowd=p.signals.get("crowd") or 0.0,
                    personal_adjustment=p.breakdown.get("personal_adjustment", 0.0),
                )
                p.viral_score, p.stage_scores = res.viral_score, res.stage_scores
                p.breakdown = res.breakdown | {"mode": p.breakdown.get("mode"), "vision": True}
        mark("vision")

    # 7. duplicate detection + diverse top-K ------------------------------------------------------------
    report(93, "Dubbele momenten verwijderen en top clips kiezen")
    max_clips = (creator.max_clips_per_video if creator and creator.max_clips_per_video else None) or rs.clips.max_per_video
    texts = [p.text for p in plans]
    embeddings = None
    if llm is not None and rs.pipeline.use_embeddings and texts:
        try:
            embeddings = llm.embed(texts)
        except Exception as e:  # pragma: no cover - network specific
            warnings.append(f"Embeddings niet beschikbaar: {e}")
    sims = similarity_matrix(texts, embeddings) if texts else None
    selected = (
        mmr_select(
            [p.viral_score for p in plans],
            [(p.window.start, p.window.end) for p in plans],
            sims,
            k=max_clips,
            min_score=rs.scoring.min_viral_score,
        )
        if plans
        else []
    )
    chosen = [plans[i] for i in selected]
    mark("selection", selected=len(chosen), similarity="embeddings" if embeddings else "tfidf")

    # Log every candidate with its outcome (lets the UI explain "why not this moment").
    selected_ids = {p.cid for p in chosen}
    by_cid = {p.cid: p for p in plans}
    log_rows = []
    for c in cands:
        row = c.to_log(ctx)
        p = by_cid.get(c.cid)
        if p is not None:
            row.update(
                viral_score=p.viral_score, final_start=p.window.start, final_end=p.window.end,
                selected=c.cid in selected_ids, verdict=p.extra.get("verdict"), why=p.why, category=p.category,
            )
        elif c.cid in rejected:
            row.update(selected=False, note="afgewezen: " + ", ".join(REJECT_LABELS_NL.get(r, r) for r in rejected[c.cid]))
        else:
            row.update(selected=False, note="niet in shortlist")
        log_rows.append(row)

    return AnalysisOutput(
        plans=chosen,
        candidates_log=log_rows,
        stage_log=stage_log,
        meter=meter,
        provider=llm.provider if llm else "heuristic",
        models=dict(llm.models) if llm else {},
        warnings=warnings,
    )
