import numpy as np

from app.ai.dedupe import mmr_select, overlap_ratio, similarity_matrix


def test_overlap_ratio():
    assert overlap_ratio((0, 10), (5, 15)) == 0.5
    assert overlap_ratio((0, 30), (10, 20)) == 1.0  # nested
    assert overlap_ratio((0, 10), (20, 30)) == 0.0


def test_similarity_detects_paraphrase_duplicates():
    texts = [
        "hij betaalde duizend euro voor een banaan in de supermarkt",
        "duizend euro betaald voor een banaan in de supermarkt, echt waar",
        "mijn moeder heeft de loterij gewonnen met een miljoen",
    ]
    sims = similarity_matrix(texts)
    assert sims[0, 1] > sims[0, 2]
    assert np.allclose(np.diag(sims), 1.0)


def test_embeddings_are_used_when_given():
    sims = similarity_matrix(["a", "b"], embeddings=[[1, 0], [1, 0]])
    assert sims[0, 1] == 1.0


def test_mmr_picks_diverse_top_k():
    scores = [95, 94, 93, 80, 70]
    spans = [(0, 15), (2, 17), (100, 115), (200, 215), (300, 315)]  # 0 and 1 overlap
    sims = np.eye(5)
    sims[2, 3] = sims[3, 2] = 0.9  # 2 and 3 are near-duplicates in content
    picked = mmr_select(scores, spans, sims, k=3)
    assert picked == [0, 2, 4]


def test_mmr_min_score():
    assert mmr_select([50, 40], [(0, 1), (5, 6)], np.eye(2), k=5, min_score=45) == [0]
