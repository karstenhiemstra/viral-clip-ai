import random

from app.models import Clip, Video
from app.services.learning import MIN_SAMPLES, personal_adjustment, train_model


def _seed(db, n=40):
    v = Video(title="x")
    db.add(v)
    db.commit()
    rng = random.Random(3)
    for i in range(n):
        hook = rng.random()
        rating = "viral" if hook > 0.75 else "good" if hook > 0.5 else "bad" if hook > 0.25 else "reject"
        db.add(
            Clip(
                video_id=v.id, start_time=i * 20, end_time=i * 20 + 15, duration=15, viral_score=60,
                features={"d_hook": hook, "d_humor": rng.random(), "duration": 15.0}, rating=rating,
                category="humor" if i % 2 else "story",
            )
        )
    db.commit()


def test_not_enough_samples(db):
    _seed(db, n=MIN_SAMPLES - 1)
    assert train_model(db) is None


def test_model_learns_that_hooks_matter(db):
    _seed(db)
    model = train_model(db)
    assert model is not None and model.n_samples == 40
    coef = dict(zip(model.feature_names, model.coefficients, strict=False))
    assert coef["d_hook"] > abs(coef["d_humor"])
    assert model.metrics["cv_correlation"] > 0.5
    assert any(i["feature"] == "d_hook" and i["effect"] > 0 for i in model.insights)
    good = personal_adjustment(model, {"d_hook": 0.95, "d_humor": 0.5, "duration": 15}, "humor", 1.0)
    bad = personal_adjustment(model, {"d_hook": 0.05, "d_humor": 0.5, "duration": 15}, "humor", 1.0)
    assert good > 0 > bad
    assert -12 <= bad and good <= 12
    assert personal_adjustment(model, {"d_hook": 0.95}, "humor", 0.0) == 0.0
