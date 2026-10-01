"""Test setup: an isolated data dir + SQLite database per test, no network, no API keys."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="viralclip-tests-"))
os.environ.update(
    {
        "APP_ENV": "test",
        "DATA_DIR": str(_TMP / "data"),
        "DATABASE_URL": f"sqlite:///{_TMP / 'bootstrap.db'}",
        "YOUTUBE_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "API_AUTH_TOKEN": "",
        "APP_SECRET_KEY": "test-secret",
        "FACE_MODEL_PATH": str(_TMP / "no-model.onnx"),
        "FACE_MODEL_URL": "",
    }
)

import pytest  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.ai.transcript import Word  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    """SQLite per test by default; set TEST_DATABASE_URL to run the suite against PostgreSQL."""
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    engine = dbmod.init_engine(url)
    if not url.startswith("sqlite"):
        dbmod.Base.metadata.drop_all(engine)
    dbmod.create_all()
    session = dbmod.SessionLocal()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _offline_key_checks(monkeypatch):
    """Saving a key triggers a live connection test; tests never talk to YouTube/OpenAI/Anthropic."""
    import app.api.settings as settings_api

    monkeypatch.setattr(settings_api, "_check_youtube", lambda key: (key == "good-youtube-key", "De YouTube API key is ongeldig of verkeerd gekopieerd."))
    monkeypatch.setattr(settings_api, "_check_llm", lambda db, name, key: (key.startswith("sk-good"), "Ongeldige OPENAI_API_KEY."))


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def make_words(lines: list[str], start: float = 0.0, word_dur: float = 0.32, gap: float = 0.06, pause: float = 0.7) -> list[Word]:
    words: list[Word] = []
    t = start
    for line in lines:
        for tok in line.split():
            words.append(Word(t, t + word_dur, tok))
            t += word_dur + gap
        t += pause
    return words


SAMPLE_LINES = [
    "Hallo allemaal en welkom bij een nieuwe vlog.",
    "Vandaag gaan we naar de supermarkt.",
    "Nou eh dus we lopen hier gewoon een beetje rond.",
    "Wacht wat?! Dit is echt niet normaal, kijk dan!",
    "Die gast heeft duizend euro betaald voor een banaan hahaha.",
    "Ik zweer het, dit is het ergste wat ik ooit heb gezien.",
    "Waarom zou iemand dat doen?",
    "Toen bleek opeens dat het een grap was van zijn vrienden.",
    "Hahaha iedereen lag helemaal kapot.",
    "Oké, we gaan verder.",
    "Dit is een rustig stuk waar niet veel gebeurt en we praten over het weer.",
    "Het weer is best oké vandaag, beetje bewolkt en een graad of vijftien.",
    "We lopen naar de kassa en rekenen de boodschappen af.",
    "Eerlijk gezegd vind ik dat hele gedoe met influencers echt belachelijk.",
    "Iedereen doet maar alsof alles perfect is, dat is gewoon fake.",
    "Wat vinden jullie daarvan? Laat het me weten.",
    "Mijn moeder belde gisteren en ze had nieuws.",
    "Ze heeft de loterij gewonnen, een miljoen euro!",
    "Ik schreeuwde zo hard dat de buren kwamen kijken.",
    "Vergeet niet te abonneren en tot de volgende keer, doei!",
]


@pytest.fixture()
def sample_words() -> list[Word]:
    return make_words(SAMPLE_LINES)


@pytest.fixture(scope="session")
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture()
def test_video(tmp_path, ffmpeg_available) -> Path:
    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    import subprocess

    out = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=30",
            "-f", "lavfi", "-i", "sine=f=220:d=30,volume='if(between(t,9,11),1.0,0.15)':eval=frame",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
        ],
        check=True,
    )
    return out
