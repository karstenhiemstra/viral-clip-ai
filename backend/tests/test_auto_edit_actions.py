"""Auto Edit quality: complete actions, the player (and ball) in frame, calm composition, the name at the end.

Uses synthetic match footage (tests/football_footage.py): a player with a ball does a dribble and, after a camera
cut, a sprint with the camera panning along; a static score bar stays on screen."""

import math
import shutil
from pathlib import Path

import numpy as np
import pytest

from app.edit.analysis import Footage, analyze_footage, detect_actions, detect_beats
from app.edit.editor import STRONG_TRANSITIONS, STYLES, _framing, build_plan, source_seconds
from app.edit.render import render_edit, title_window
from app.video import ffmpeg
from tests.football_footage import CUT, DRIBBLE, SPRINT, make_football_footage, player_cx

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def match(tmp_path_factory) -> Path:
    return make_football_footage(tmp_path_factory.mktemp("match") / "match.mp4")


@pytest.fixture(scope="module")
def fx(match) -> Footage:
    return analyze_footage(match, 1)


@pytest.fixture(scope="module")
def beat(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("music") / "beat.wav"
    ffmpeg.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "aevalsrc='0.8*sin(2*PI*60*t)*exp(-12*mod(t,0.5))':s=44100:d=30", str(out)], timeout=120)
    return out


def test_whole_actions_are_found_with_context_before_and_after(fx):
    assert [round(c) for c in fx.cuts] == [round(CUT)]
    actions = sorted(detect_actions(fx), key=lambda a: a.start)
    assert len(actions) == 2
    for act, (a, b) in zip(actions, (DRIBBLE, SPRINT), strict=True):
        assert abs(act.start - a) <= 0.5 and abs(act.end - b) <= 0.7, (act.start, act.end)
        assert act.checks["complete"] and act.checks["visible"] and act.checks["size_ok"]
        assert act.win_start <= act.start - 0.3, "context before the action (the run-up)"
        assert act.win_end >= act.end + 0.2, "and after it"
        assert act.shot_start <= act.win_start and act.win_end <= act.shot_end, "never across the camera cut"


def test_the_action_is_measured_without_the_camera_movement(fx):
    """During the sprint the camera pans along; the measured position still follows the player, and the
    static score bar (which 'moves' relative to the pitch during the pan) is ignored."""
    for t in (10.0, 10.5, 11.0, 11.5):
        assert abs(float(fx.cx[fx.idx(t)]) - player_cx(t)) < 0.08, t
    assert float(np.median(fx.activity[: fx.idx(2.5)])) == 0.0, "standing still is no action"


def _strong(shot) -> bool:
    return shot.get("moment") is not None or shot["transition"] in STRONG_TRANSITIONS


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_plan_shows_whole_actions_and_stays_calm(fx, beat, seed):
    music = detect_beats(beat)
    plan = build_plan("Neymar", "football", 8.0, [fx], seed, music=music)
    shots = plan["shots"]
    assert plan["quality"]["complete_actions"] == 2 and len(shots) >= 2
    first = shots[0]
    assert first["checks"]["complete"] and first["transition"] == "cut", "open on a strong, whole action"
    for shot in shots:
        if shot.get("action"):  # the whole action is in the shot, with context
            a, b = shot["action"]
            assert shot["start"] <= a - 0.25 + 1e-6
            assert shot["start"] + source_seconds(shot) >= b
    strong = [_strong(s) for s in shots]
    assert not any(x and y for x, y in zip(strong, strong[1:], strict=False)), "never two striking shots in a row"
    assert all(not (s.get("moment") and s["transition"] in STRONG_TRANSITIONS) for s in shots), "one effect per shot"
    assert sum(strong) <= math.ceil(len(shots) / 2)
    cuts = np.cumsum([s["out"] for s in shots])[:-1] + plan["music"]["start"]
    assert all(np.min(np.abs(music.beats - c)) < 0.02 for c in cuts), "the cuts are on the beat"


def test_the_crop_follows_the_player(fx):
    plan = build_plan("Neymar", "football", 8.0, [fx], 1)
    sprint = next(s for s in plan["shots"] if s.get("action") and s["action"][0] > CUT)
    xs = [k[1] for k in sprint["framing"]["track"]]
    assert sprint["framing"]["mode"] == "crop"
    assert xs[0] > 0.6 and xs[-1] < 0.35 and all(b <= a + 0.02 for a, b in zip(xs, xs[1:], strict=False)), xs
    dribble = next(s for s in plan["shots"] if s.get("action") and s["action"][1] < CUT)
    assert [k[1] for k in dribble["framing"]["track"]][-1] > 0.75  # ... to the right during the dribble
    assert sprint["framing"]["zoom"] > 1.0, "a small (far away) player is shown a little closer"


def test_player_and_ball_stay_in_the_picture_and_the_name_comes_last(match, fx, tmp_path):
    plan = build_plan("Neymar", "football", 8.0, [fx], 1)
    out = tmp_path / "edit.mp4"
    meta = render_edit(plan, {1: (match, ffmpeg.probe(match))}, out, tmp_path / "t.jpg", tmp_path / "w")
    assert (ffmpeg.probe(out).width, ffmpeg.probe(out).height) == (1080, 1920)
    raw = ffmpeg.run([ffmpeg.ffmpeg_bin(), "-v", "error", "-i", str(out), "-vf", "fps=10,scale=270:480",
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).stdout
    f = np.frombuffer(raw, np.uint8).reshape(-1, 480, 270, 3).astype(int)[3:-3]  # (fades at both ends)
    red = (f[..., 0] > 150) & (f[..., 1] < 90) & (f[..., 2] < 90)
    white = (f[..., 0] > 200) & (f[..., 1] > 200) & (f[..., 2] > 200)
    assert red.sum(axis=(1, 2)).min() > 100, "the player is in every frame"
    centre = np.array([np.nonzero(r)[1].mean() / 270 for r in red])
    assert np.mean((centre > 0.2) & (centre < 0.8)) > 0.95, "and (roughly) in the middle"
    assert (white.sum(axis=(1, 2)) > 4).mean() > 0.95, "the ball too"
    assert meta["title_at"][0] >= meta["shot_starts"][-1] > 0, "the name only on the last shot"
    assert "action_tracking" in meta["effects"]


def test_too_wide_action_shows_the_whole_frame_and_far_action_comes_closer():
    n = 50
    base = dict(video_id=1, fps=5.0, duration=10.0, width=1920, height=1080, motion=np.zeros(n), cx=np.full(n, 0.5),
                bright=np.full(n, 0.4), audio=np.zeros(n), activity=np.zeros(n), cy=np.full(n, 0.5))
    wide = Footage(**base, cover=np.full(n, 0.25), extent=np.full(n, 0.7))
    assert _framing(wide, 2.0, 5.0, STYLES["football"])["mode"] == "fit"  # e.g. a long pass across the pitch
    far = Footage(**base, cover=np.full(n, 0.95), extent=np.full(n, 0.04))
    fr = _framing(far, 2.0, 5.0, STYLES["football"])
    assert fr["mode"] == "crop" and 1.2 <= fr["zoom"] <= 1.4
    close = Footage(**base, cover=np.full(n, 0.9), extent=np.full(n, 0.25))
    assert _framing(close, 2.0, 5.0, STYLES["football"])["zoom"] == 1.0  # never zoom in on a big action


def test_the_same_picture_twice_is_skipped():
    """Two actions that look exactly alike (e.g. the same replay twice): only one goes in the edit."""
    n, fps = 100, 5.0
    act = np.zeros(n)
    act[15:25] = 0.02  # 3-5 s
    act[65:75] = 0.02  # 13-15 s
    fx = Footage(1, fps, 20.0, 1920, 1080, act, np.full(n, 0.5), np.full(n, 0.4), np.zeros(n), [10.0], activity=act,
                 cy=np.full(n, 0.5), cover=np.full(n, 0.9), extent=np.full(n, 0.2), pan=np.zeros(n),
                 thumbs=[[0.4] * 48 for _ in range(20)])
    plan = build_plan("Neymar", "football", 6.0, [fx], 1)
    actions = [s["action"] for s in plan["shots"] if s.get("checks", {}).get("complete")]
    assert len(actions) == 1


def test_the_name_is_never_on_the_first_shot():
    start, end = title_window([0.0, 3.5, 6.0], 8.0)
    assert start >= 6.0 and end <= 8.0
    assert title_window([0.0], 6.0)[0] >= 0.8  # a single shot: not right at the start either
