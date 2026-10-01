"""Personal learning system.

Every clip stores the feature vector it was scored with (dimension scores, hook/audio/lexical features,
category, duration). Your feedback (🔥 viral / 👍 good / 👎 bad / ❌ reject) and the real TikTok stats
you log become training targets. A small ridge regression learns which features predict *your*
results and nudges future Viral Scores (bounded, and only as far as its cross-validated accuracy
justifies). It also produces plain-language insights for the Analytics page.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Clip, ClipPerformance, Rating, ScoringModel

log = logging.getLogger(__name__)

RATING_TARGET = {Rating.VIRAL: 1.0, Rating.GOOD: 0.7, Rating.BAD: 0.2, Rating.REJECT: 0.0}
MIN_SAMPLES = 12
RIDGE_LAMBDA = 5.0
MAX_ADJUST = 12.0

FEATURE_LABELS_NL = {
    "d_hook": "een sterke hook",
    "d_hook_strength": "de interessante info direct in de eerste seconde",
    "d_curiosity": "veel nieuwsgierigheid",
    "d_emotion": "veel emotie",
    "d_surprise": "een verrassing",
    "d_humor": "humor",
    "d_shareability": "hoge deelbaarheid",
    "d_comment_potential": "discussiepotentieel",
    "d_retention": "hoge retentie",
    "d_context": "weinig benodigde context",
    "d_payoff": "een duidelijke payoff",
    "d_rewatch": "rewatch-waarde",
    "duration": "een langere duur",
    "crowd": "momenten die kijkers in de comments noemen",
    "audio_peak_z": "luide reacties / gelach",
    "hook_first_question": "een vraag in de eerste zin",
    "hook_time_to_strong": "een trage opbouw naar het eerste sterke woord",
    "hook_starts_filler": "een start met stopwoorden",
    "lex_controversy": "controversiële uitspraken",
    "lex_humor": "gelach in de audio/tekst",
    "rel_speech_rate": "snel spreektempo",
}


@dataclass
class TrainingSet:
    X: np.ndarray
    y: np.ndarray
    feature_names: list[str]
    clip_ids: list[int]
    categories: list[str | None]
    durations: list[float]


def _performance_targets(db: Session) -> dict[int, float]:
    """Latest stats per clip -> 0..1 score: views (log, normalised per creator) + completion + engagement."""
    latest = (
        select(ClipPerformance.clip_id, func.max(ClipPerformance.recorded_at).label("ts"))
        .group_by(ClipPerformance.clip_id)
        .subquery()
    )
    rows = db.execute(
        select(ClipPerformance, Clip.creator_id)
        .join(latest, (ClipPerformance.clip_id == latest.c.clip_id) & (ClipPerformance.recorded_at == latest.c.ts))
        .join(Clip, Clip.id == ClipPerformance.clip_id)
    ).all()
    if not rows:
        return {}
    by_creator: dict[Any, list[float]] = {}
    for perf, creator_id in rows:
        if perf.views:
            by_creator.setdefault(creator_id, []).append(math.log10(perf.views + 1))
    out: dict[int, float] = {}
    for perf, creator_id in rows:
        parts, weights = [], []
        vals = by_creator.get(creator_id, [])
        if perf.views and len(vals) >= 3:
            mu, sd = float(np.mean(vals)), float(np.std(vals)) or 1.0
            z = (math.log10(perf.views + 1) - mu) / sd
            parts.append(1 / (1 + math.exp(-z)))
            weights.append(0.5)
        elif perf.views:
            parts.append(min(1.0, math.log10(perf.views + 1) / 6))
            weights.append(0.5)
        completion = perf.completion_rate if perf.completion_rate is not None else (
            perf.avg_percentage_watched / 100 if perf.avg_percentage_watched is not None else None
        )
        if completion is not None:
            parts.append(max(0.0, min(1.0, completion if completion <= 1 else completion / 100)))
            weights.append(0.3)
        if perf.views:
            eng = ((perf.likes or 0) + 2 * (perf.comments or 0) + 3 * (perf.shares or 0) + 2 * (perf.saves or 0)) / perf.views
            parts.append(min(1.0, eng / 0.15))
            weights.append(0.2)
        if parts:
            out[perf.clip_id] = sum(p * w for p, w in zip(parts, weights, strict=False)) / sum(weights)
    return out


def build_training_set(db: Session) -> TrainingSet | None:
    perf = _performance_targets(db)
    clips = db.scalars(select(Clip).where((Clip.rating.is_not(None)) | (Clip.id.in_(list(perf) or [-1])))).all()
    samples: list[tuple[Clip, float]] = []
    for c in clips:
        targets = []
        if c.rating in RATING_TARGET:
            targets.append(RATING_TARGET[c.rating])
        if c.id in perf:
            targets.append(perf[c.id])
        if targets and c.features:
            samples.append((c, float(np.mean(targets))))
    if len(samples) < MIN_SAMPLES:
        return None
    categories = sorted({c.category or "other" for c, _ in samples})
    names = sorted({k for c, _ in samples for k in (c.features or {}).keys()})
    names += [f"cat_{cat}" for cat in categories]
    X = np.zeros((len(samples), len(names)))
    for i, (c, _) in enumerate(samples):
        for j, n in enumerate(names):
            if n.startswith("cat_"):
                X[i, j] = 1.0 if (c.category or "other") == n[4:] else 0.0
            else:
                X[i, j] = float((c.features or {}).get(n, 0.0))
    y = np.array([t for _, t in samples])
    return TrainingSet(X, y, names, [c.id for c, _ in samples], [c.category for c, _ in samples],
                       [c.duration for c, _ in samples])


def _fit_ridge(X: np.ndarray, y: np.ndarray, lam: float = RIDGE_LAMBDA) -> tuple[np.ndarray, float, np.ndarray, np.ndarray]:
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    Z = (X - mu) / sd
    y0 = y.mean()
    A = Z.T @ Z + lam * np.eye(Z.shape[1])
    w = np.linalg.solve(A, Z.T @ (y - y0))
    return w, float(y0), mu, sd


def _cv_correlation(X: np.ndarray, y: np.ndarray, folds: int = 5) -> float | None:
    n = len(y)
    if n < 20:
        return None
    idx = np.arange(n)
    rng = np.random.default_rng(42)
    rng.shuffle(idx)
    preds = np.zeros(n)
    for k in range(folds):
        test = idx[k::folds]
        train = np.setdiff1d(idx, test)
        w, b, mu, sd = _fit_ridge(X[train], y[train])
        preds[test] = ((X[test] - mu) / sd) @ w + b
    if np.std(preds) < 1e-9 or np.std(y) < 1e-9:
        return 0.0
    return float(np.corrcoef(preds, y)[0, 1])


def compute_insights(ts: TrainingSet) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    y = ts.y
    base = float(np.mean(y)) or 1e-6
    for j, name in enumerate(ts.feature_names):
        if name.startswith("cat_"):
            continue
        col = ts.X[:, j]
        if np.std(col) < 1e-9:
            continue
        lo_cut, hi_cut = np.quantile(col, [1 / 3, 2 / 3])
        hi, lo = y[col >= hi_cut], y[col <= lo_cut]
        if len(hi) < 4 or len(lo) < 4 or hi_cut == lo_cut:
            continue
        diff = float(np.mean(hi) - np.mean(lo))
        rel = diff / max(1e-6, float(np.mean(lo)))
        label = FEATURE_LABELS_NL.get(name)
        if not label or abs(rel) < 0.15:
            continue
        direction = "beter" if diff > 0 else "slechter"
        insights.append(
            {
                "feature": name,
                "effect": round(rel, 3),
                "samples": int(len(hi) + len(lo)),
                "text": f"Clips met {label} presteren gemiddeld {abs(rel) * 100:.0f}% {direction} dan clips zonder.",
            }
        )
    # categories
    cats: dict[str, list[float]] = {}
    for cat, t in zip(ts.categories, y, strict=False):
        cats.setdefault(cat or "other", []).append(float(t))
    for cat, vals in cats.items():
        if len(vals) >= 4:
            rel = (float(np.mean(vals)) - base) / base
            if abs(rel) >= 0.15:
                insights.append(
                    {
                        "feature": f"cat_{cat}",
                        "effect": round(rel, 3),
                        "samples": len(vals),
                        "text": f"Categorie '{cat}' scoort {abs(rel) * 100:.0f}% {'boven' if rel > 0 else 'onder'} je gemiddelde.",
                    }
                )
    insights.sort(key=lambda i: abs(i["effect"]), reverse=True)
    return insights[:8]


def train_model(db: Session, target: str = "blended") -> ScoringModel | None:
    ts = build_training_set(db)
    if ts is None:
        return None
    w, b, mu, sd = _fit_ridge(ts.X, ts.y)
    preds = ((ts.X - mu) / sd) @ w + b
    ss_res = float(np.sum((ts.y - preds) ** 2))
    ss_tot = float(np.sum((ts.y - ts.y.mean()) ** 2)) or 1e-9
    cv = _cv_correlation(ts.X, ts.y)
    version = (db.scalar(select(func.max(ScoringModel.version))) or 0) + 1
    db.query(ScoringModel).filter(ScoringModel.active.is_(True)).update({"active": False})
    model = ScoringModel(
        version=version,
        target=target,
        n_samples=len(ts.y),
        feature_names=ts.feature_names,
        coefficients=[round(float(x), 6) for x in w],
        intercept=b,
        feature_means=[float(x) for x in mu],
        feature_stds=[float(x) for x in sd],
        metrics={"r2_in_sample": round(1 - ss_res / ss_tot, 4), "cv_correlation": None if cv is None else round(cv, 4),
                 "target_mean": round(float(ts.y.mean()), 4), "target_std": round(float(ts.y.std()), 4)},
        insights=compute_insights(ts),
        active=True,
    )
    db.add(model)
    db.commit()
    _cache.clear()
    return model


_cache: dict[str, Any] = {}


def active_model(db: Session) -> ScoringModel | None:
    """Active personal model, cached for a minute (API and worker are separate processes)."""
    now = time.monotonic()
    if "model" not in _cache or now - _cache.get("at", 0) > 60:
        _cache["model"] = db.scalar(
            select(ScoringModel).where(ScoringModel.active.is_(True)).order_by(ScoringModel.version.desc())
        )
        _cache["at"] = now
    return _cache["model"]


def personal_adjustment(model: ScoringModel | None, features: dict[str, float], category: str | None, strength: float) -> float:
    """Bounded score nudge (points) from the personal model. 0 when no trustworthy model exists."""
    if model is None or strength <= 0 or not model.coefficients:
        return 0.0
    x = np.array(
        [
            (1.0 if (category or "other") == n[4:] else 0.0) if n.startswith("cat_") else float(features.get(n, 0.0))
            for n in model.feature_names
        ]
    )
    mu = np.array(model.feature_means)
    sd = np.array(model.feature_stds)
    sd[sd < 1e-9] = 1.0
    pred = float(((x - mu) / sd) @ np.array(model.coefficients) + model.intercept)
    t_std = float((model.metrics or {}).get("target_std") or 0.25) or 0.25
    cv = (model.metrics or {}).get("cv_correlation")
    reliability = 0.4 if cv is None else max(0.0, min(1.0, float(cv) * 1.5))
    confidence = min(1.0, model.n_samples / 60) * reliability
    z = (pred - model.intercept) / t_std
    return float(max(-MAX_ADJUST, min(MAX_ADJUST, z * 6.0 * confidence * strength)))
