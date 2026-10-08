"""Auto Edit: prompt -> local ffmpeg edit. No AI service, no transcription."""

import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from app.config import get_settings
from app.edit.analysis import analyze_footage, detect_beats
from app.edit.editor import build_plan, match_score, parse_prompt
from app.models import ApiUsage, Job, JobType, Video, VideoStatus
from app.video import ffmpeg
from app.worker.runner import Worker

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SCENE = 4.0  # seconds per scene in the test footage


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> Path:
    """16 s of 'match footage': four scenes with movement (and a camera cut between them), with sound."""
    out = tmp_path_factory.mktemp("footage") / "neymar.mp4"
    d = SCENE
    ffmpeg.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=s=960x540:r=25:d={d}",
        "-f", "lavfi", "-i", f"life=s=960x540:r=25:mold=10:ratio=0.1:death_color=#202060:life_color=#40ff80,trim=duration={d}",
        "-f", "lavfi", "-i", f"mandelbrot=s=960x540:r=25,trim=duration={d}",
        "-f", "lavfi", "-i", f"testsrc=s=960x540:r=25:d={d}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={4 * d}",
        "-filter_complex", "".join(f"[{i}:v]setsar=1,format=yuv420p[v{i}];" for i in range(4))
        + "[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]",
        "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
    ], timeout=300)
    return out


def _beat_track(path: Path, seconds: float = 30) -> Path:
    """A 120 BPM beat: a kick every 0.5 s."""
    ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"aevalsrc='0.8*sin(2*PI*60*t)*exp(-12*mod(t,0.5))+0.3*sin(2*PI*1000*t)*exp(-40*mod(t+0.25,0.5))':s=44100:d={seconds}",
                str(path)], timeout=120)
    return path


@pytest.fixture
def music_dir():
    d = get_settings().music_dir
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for f in d.iterdir():
        f.unlink()


@pytest.mark.parametrize(
    ("prompt", "subject", "style", "explicit"),
    [
        ("Maak een edit van Neymar", "Neymar", "football", False),  # a footballer: the football style by itself
        ("Maak een cinematic edit van Neymar", "Neymar", "cinematic", True),
        ("Maak een snelle voetbal edit van Mbappé", "Mbappé", "fast", True),
        ("Maak een hype edit van Ronaldo", "Ronaldo", "hype", True),
        ("Maak een cinematic edit van Messi", "Messi", "cinematic", True),
        ("Maak een clean edit van mijn hond Max", "mijn hond Max", "clean", True),
        ("Maak een edit van mijn vakantie", "mijn vakantie", "hype", False),
        ("Neymar edit", "Neymar", "football", False),
    ],
)
def test_the_prompt_is_read_without_ai(prompt, subject, style, explicit):
    req = parse_prompt(prompt)
    assert (req.subject, req.style, req.style_explicit, req.duration) == (subject, style, explicit, 15.0)


def test_duration_and_matching():
    assert parse_prompt("Maak een hype edit van Ronaldo van 20 seconden").duration == 20
    assert parse_prompt("Maak een edit van Messi 8s").duration == 8
    assert match_score("Mbappé", "mbappe skills 2024") > 0  # accents do not matter
    assert match_score("Neymar", "Messi goals compilatie") == 0
    assert match_score("Cristiano Ronaldo", "cristiano ronaldo goals") > match_score("Cristiano Ronaldo", "ronaldo nazario")


def test_beats_are_found_locally(tmp_path):
    music = detect_beats(_beat_track(tmp_path / "beat.wav"))
    assert 110 <= music.bpm <= 130
    assert abs(float(np.median(np.diff(music.beats))) - 0.5) < 0.03


def test_footage_analysis_finds_the_camera_cuts_and_the_action(footage):
    fx = analyze_footage(footage, 1)
    assert [round(c) for c in fx.cuts] == [4, 8, 12]
    assert fx.motion.max() > 0.01 and 0 <= fx.cx.min() <= fx.cx.max() <= 1
    assert len(fx.shots()) == 4


def test_cuts_follow_the_beat_and_regenerating_varies_the_edit(footage, tmp_path):
    fx = analyze_footage(footage, 7)
    music = detect_beats(_beat_track(tmp_path / "beat.wav"))
    plan = build_plan("Neymar", "hype", 8.0, [fx], seed=1, music=music)
    start = plan["music"]["start"]
    cuts = np.cumsum([s["out"] for s in plan["shots"]])[:-1] + start
    assert all(np.min(np.abs(music.beats - c)) < 0.02 for c in cuts), "every cut on a beat"
    assert abs(plan["duration"] - 8.0) < 0.6 and len(plan["shots"]) >= 6
    other = build_plan("Neymar", "hype", 8.0, [fx], seed=2, music=music)
    key = lambda p: [(s["start"], s["out"], s["transition"], s["zoom"]) for s in p["shots"]]  # noqa: E731
    assert key(other) != key(plan)
    no_music = build_plan("Neymar", "hype", 8.0, [fx], seed=1, music=None)
    assert no_music["music"] is None and abs(no_music["duration"] - 8.0) < 0.01


def _frame(path: Path, t: float) -> np.ndarray:
    raw = ffmpeg.run([ffmpeg.ffmpeg_bin(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                      "-f", "rawvideo", "-pix_fmt", "gray", "-"]).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(1920, 1080).astype(float)


@pytest.fixture
def no_ai(monkeypatch):
    """Any AI or transcription call fails the test."""
    import openai

    def boom(*a, **k):
        raise AssertionError("Auto Edit mag geen AI/transcriptie aanroepen")

    monkeypatch.setattr("app.ai.llm.get_llm", boom)
    monkeypatch.setattr("app.ai.pipeline.get_transcriber", boom)
    monkeypatch.setattr(openai.OpenAI, "__init__", boom)
    try:
        import anthropic

        monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    except ImportError:
        pass


def _upload(client, footage, title="Neymar skills compilatie") -> int:
    r = client.post("/api/edits/footage", files={"file": ("neymar.mp4", footage.open("rb"), "video/mp4")}, data={"title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _ready(client, edit_id) -> dict:
    Worker("t").drain()
    e = client.get(f"/api/edits/{edit_id}").json()
    assert e["status"] == "ready", e
    return e


def _download(client, e, tmp_path, name="edit.mp4") -> Path:
    r = client.get(e["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    out = tmp_path / name
    out.write_bytes(r.content)
    return out


def test_make_an_edit_of_neymar_end_to_end(client, db, footage, tmp_path, music_dir, no_ai):
    vid = _upload(client, footage)
    video = db.get(Video, vid)
    assert video.status == VideoStatus.SKIPPED  # footage only: no clip analysis, so never a transcription
    assert not db.query(Job).filter(Job.type == JobType.ANALYZE_VIDEO).count()
    up = client.post("/api/edits/music", files={"file": ("beat.wav", _beat_track(tmp_path / "beat.wav").open("rb"), "audio/wav")})
    assert up.status_code == 201
    assert [m["name"] for m in client.get("/api/edits/sources").json()["music"]] == ["beat.wav"]

    r = client.post("/api/edits", json={"prompt": "Maak een edit van Neymar", "duration": 8})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["style"] == "football" and created["style_auto"] is True and created["status"] == "queued"
    assert [s["id"] for s in created["sources"]] == [vid]

    e = _ready(client, created["id"])
    out = _download(client, e, tmp_path)
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert abs(info.duration - e["result"]["duration"]) < 0.25 and 6.5 <= info.duration <= 9.0
    assert e["result"]["music"] == "beat.wav" and e["plan"]["music"]["bpm"] > 100
    assert len(e["plan"]["shots"]) >= 4 and len(e["result"]["effects"]) >= 4
    assert client.get(e["video_url"]).status_code == 200 and client.get(e["thumbnail_url"]).status_code == 200
    assert db.query(ApiUsage).count() == 0  # no paid API used

    # "Edit aanpassen": order, delete, shorten, transition, effects, music off -> exactly that edit
    shots = e["plan"]["shots"][:3]
    shots = [shots[1], shots[0], shots[2]]
    for s in shots:
        s.update(out=1.5, speed=1.0, ramp=False, freeze=0.0, transition="cut", zoom=None, shake=False)
    shots[1]["transition"] = "flash"
    shots[2]["freeze"] = 0.6
    r = client.put(f"/api/edits/{e['id']}/plan", json={"shots": shots, "music": False})
    assert r.status_code == 200, r.text
    e2 = _ready(client, e["id"])
    assert e2["version"] == e["version"] + 1 and e2["result"]["music"] is None
    out2 = _download(client, e2, tmp_path, "edit2.mp4")
    assert abs(ffmpeg.probe(out2).duration - 4.5) < 0.2
    # the white flash is really in the video at the start of the 2nd shot, and the freeze frame is still
    flash, before = _frame(out2, 1.5 + 1 / 60), _frame(out2, 1.2)
    assert flash.mean() > 200 and flash.mean() > before.mean() + 40
    a, b = _frame(out2, 3.93), _frame(out2, 4.03)  # (the fade-out starts at 4.05)
    assert np.abs(a - b).mean() < 1.0, "freeze frame"
    from app.models import Edit

    db.expire_all()
    assert any("color=white" in g for g in db.get(Edit, e["id"]).render_meta["filters"])


def test_cinematic_edit_has_letterbox_and_regenerate_makes_another(client, footage, tmp_path, no_ai):
    _upload(client, footage)
    r = client.post("/api/edits", json={"prompt": "Maak een cinematic edit van Neymar", "duration": 7, "music": False})
    e = _ready(client, r.json()["id"])
    assert e["style"] == "cinematic" and e["style_auto"] is False
    assert "letterbox" in e["result"]["effects"] and "colour_grade" in e["result"]["effects"]
    f = _frame(_download(client, e, tmp_path), 3.0)
    assert f[:150].mean() < 12 and f[-150:].mean() < 12, "black bars top and bottom"
    assert f[600:1300].mean() > 25
    first = [(s["start"], s["out"]) for s in e["plan"]["shots"]]
    r = client.post(f"/api/edits/{e['id']}/regenerate")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    again = _ready(client, e["id"])
    assert again["version"] == e["version"] + 1
    assert [(s["start"], s["out"]) for s in again["plan"]["shots"]] != first


def test_without_footage_the_page_says_what_to_do(client, footage):
    r = client.post("/api/edits", json={"prompt": "Maak een edit van Neymar"})
    assert r.status_code == 422 and "Voeg eerst" in r.json()["detail"]
    _upload(client, footage, title="Vakantie Spanje")
    r = client.post("/api/edits", json={"prompt": "Maak een edit van Neymar"})
    assert r.status_code == 422 and "Neymar" in r.json()["detail"] and "Voeg eerst" in r.json()["detail"]
    assert client.post("/api/edits", json={"prompt": "Maak een edit"}).status_code == 422


def test_the_editor_code_never_uses_an_ai_service():
    root = Path(__file__).resolve().parents[1] / "app"
    for f in [*sorted((root / "edit").glob("*.py")), root / "api" / "edits.py"]:
        text = f.read_text()
        assert not re.search(r"openai|anthropic|whisper|app\.ai\.(llm|transcription|pipeline)", text, re.I), f.name
