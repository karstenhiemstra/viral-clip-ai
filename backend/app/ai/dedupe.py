"""Stage 8/9 - duplicate detection and diverse final ranking (Maximal Marginal Relevance).

Two clips are "the same moment" when they overlap in time or say nearly the same thing. Similarity is
computed with embeddings when the provider offers them, otherwise with a local TF-IDF cosine.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

import numpy as np

from app.ai.signals import normalize_text_for_similarity


def overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Intersection divided by the shorter clip (catches a short clip nested inside a longer one)."""
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 0.0


def tfidf_matrix(texts: Sequence[str]) -> np.ndarray:
    docs = [normalize_text_for_similarity(t) for t in texts]
    # word unigrams + bigrams make paraphrased duplicates visible while staying dependency-free
    grams = [d + [f"{x}_{y}" for x, y in zip(d, d[1:], strict=False)] for d in docs]
    df: Counter[str] = Counter()
    for g in grams:
        df.update(set(g))
    vocab = {w: i for i, w in enumerate(sorted(df))}
    n = len(texts)
    mat = np.zeros((n, max(1, len(vocab))), dtype=np.float64)
    for i, g in enumerate(grams):
        tf = Counter(g)
        for w, cnt in tf.items():
            idf = math.log((1 + n) / (1 + df[w])) + 1.0
            mat[i, vocab[w]] = (1 + math.log(cnt)) * idf
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def similarity_matrix(texts: Sequence[str], embeddings: list[list[float]] | None = None) -> np.ndarray:
    if embeddings is not None and len(embeddings) == len(texts) and len(texts) > 0:
        m = np.asarray(embeddings, dtype=np.float64)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        m = m / norms
    else:
        m = tfidf_matrix(texts)
    return np.clip(m @ m.T, 0.0, 1.0)


def mmr_select(
    scores: Sequence[float],
    spans: Sequence[tuple[float, float]],
    sims: np.ndarray,
    k: int,
    *,
    diversity: float = 25.0,
    max_overlap: float = 0.35,
    max_similarity: float = 0.82,
    min_score: float = 0.0,
) -> list[int]:
    """Greedy MMR: repeatedly pick the clip with the best ``score - diversity * max_sim(selected)``,
    skipping clips that overlap a selected one in time or are near-duplicates in content."""
    remaining = [i for i in range(len(scores)) if scores[i] >= min_score]
    selected: list[int] = []
    while remaining and len(selected) < k:
        best_i, best_val = None, -math.inf
        for i in remaining:
            if any(overlap_ratio(spans[i], spans[j]) > max_overlap for j in selected):
                continue
            max_sim = max((float(sims[i, j]) for j in selected), default=0.0)
            if max_sim >= max_similarity:
                continue
            val = scores[i] - diversity * max_sim
            if val > best_val:
                best_i, best_val = i, val
        if best_i is None:
            break
        selected.append(best_i)
        remaining.remove(best_i)
    return selected
