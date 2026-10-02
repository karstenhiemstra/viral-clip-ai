import numpy as np
import pytest

from app.video import ffmpeg
from app.video.audio_features import clip_features, compute_profile, find_peaks, load_profile
from app.video.captions import PRESETS, CaptionWord, ass_color, build_ass, caption_y, group_words
from app.video.reframe import (
    CropPlan,
    Face,
    Track,
    build_keyframes,
    build_tracks,
    choose_speakers,
    speaker_evidence,
)
from app.video.render import render_clip


def test_ass_color():
    assert ass_color("#FF0000") == "&H000000FF"
    assert ass_color("#3CFF6B", 0x80) == "&H806BFF3C"


def _events(ass: str, style: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue:") and f",{style}," in line]


def test_caption_grouping_and_ass_output():
    words = [CaptionWord(i * 0.4, i * 0.4 + 0.35, t) for i, t in enumerate("Wacht wat?! Dit is echt niet normaal jongens".split())]
    groups = group_words(words, PRESETS["capcut"])
    assert [" ".join(w.text for w in g) for g in groups][0] == "Wacht wat?!"
    assert all(len(g) <= 4 for g in groups)
    ass = build_ass(words, "capcut", y=1459, emphasis=["normaal"], title="Hook {titel}")
    assert "PlayResY: 1920" in ass and "WACHT" in ass and "WAT?!" in ass
    assert len(_events(ass, "Cap")) == len(words)  # every word placed on its own
    assert len(_events(ass, "Box")) == len(words)  # and gets the box while it is spoken
    assert "{titel}" not in ass  # braces are escaped
    # the old styles are gone: older clips/settings render with the CapCut style
    for legacy in ("dynamic", "bold_white", "minimal"):
        assert build_ass(words, legacy, y=1459) == build_ass(words, "capcut", y=1459)
    assert "Montserrat" not in ass and "Style: Cap,Poppins ExtraBold,130,&H00FFFFFF" in ass


def test_caption_position_matches_template():
    assert caption_y(0.45, 0.2, "face") == caption_y(0.9, 0.4, "face") == int(1920 * 0.76)
    assert caption_y(None, None, "fit_blur") == int(1920 * 0.76)
    assert caption_y(None, None, "split") == 960


def test_highlight_box_sits_exactly_behind_the_spoken_word():
    import re

    words = [CaptionWord(0.0, 0.3, "Ik"), CaptionWord(0.3, 0.6, "heb"), CaptionWord(0.6, 0.9, "het"), CaptionWord(0.9, 1.6, "allemaal.")]
    ass = build_ass(words, "capcut", y=1459)
    texts = [(float(m[0]), float(m[1]), m[2]) for m in re.findall(r"\\pos\(([\d.]+),([\d.]+)\)\}([^\n]+)", "\n".join(_events(ass, "Cap")))]
    assert [t for _, _, t in texts] == ["IK", "HEB", "HET", "ALLEMAAL"]  # uppercase, no trailing full stop
    assert texts[0][0] < texts[1][0] < texts[2][0] < texts[3][0]
    assert abs((texts[0][0] + texts[-1][0]) / 2 - 540) < 120  # one line, centred
    boxes = _events(ass, "Box")
    assert [b.split(",")[1:3] for b in boxes] == [["0:00:00.00", "0:00:00.30"], ["0:00:00.30", "0:00:00.60"],
                                                    ["0:00:00.60", "0:00:00.90"], ["0:00:00.90", "0:00:01.95"]]
    for (cx, _cy, _), box in zip(texts, boxes, strict=True):
        x, y = map(int, re.search(r"\\pos\((\d+),(\d+)\)", box).groups())
        w = int(re.search(r" l (\d+) 0 ", box).group(1)) + 7  # right edge (minus the corner radius) + radius
        assert abs(x + w / 2 - cx) <= 2  # box centred on its word
        assert 1400 < y < 1459 < y + 80  # around the caption line at 76% of the height


def test_template_font_and_colours_are_rendered(tmp_path, ffmpeg_available):
    """Burn one caption into a frame: Poppins (bundled) renders, the spoken word sits on the blue box."""
    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    import numpy as np

    from app.config import get_settings

    words = [CaptionWord(0.0, 0.5, "Ik"), CaptionWord(0.5, 1.0, "heb"), CaptionWord(1.0, 1.5, "het"), CaptionWord(1.5, 2.5, "allemaal")]
    (tmp_path / "c.ass").write_text(build_ass(words, "capcut", y=1459), encoding="utf-8")
    fonts = ffmpeg.escape_filter_path(get_settings().fonts_dir)
    ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x404040:s=1080x1920:d=2", "-vf",
                f"ass=filename={ffmpeg.escape_filter_path(tmp_path / 'c.ass')}:fontsdir={fonts}", "-ss", "1.8",
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(tmp_path / "f.rgb")])
    img = np.frombuffer((tmp_path / "f.rgb").read_bytes(), dtype=np.uint8).reshape(1920, 1080, 3).astype(int)
    band = img[1400:1520]
    blue = (band[..., 2] > 200) & (band[..., 0] < 80) & (band[..., 1] > 130)
    white = (band > 235).all(axis=-1)
    assert blue.sum() > 5000 and white.sum() > 5000
    cols = np.where(blue.any(axis=0))[0]
    assert cols.min() > 540 - 50  # the box is on the last word (right half), not on the whole line


def test_audio_profile_and_peaks():
    sr = 8000
    t = np.arange(sr * 20) / sr
    amp = np.where((t > 10) & (t < 11), 0.9, 0.1)
    samples = (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    prof = compute_profile(samples, sr)
    peaks = find_peaks(prof)
    assert peaks and 9.5 <= peaks[0][0] <= 11.5
    f = clip_features(prof, 8, 14)
    assert f["peak_z"] > 2 and 0.2 < f["peak_pos"] < 0.7
    assert clip_features(None, 0, 5)["available"] == 0.0


def test_tracks_bridge_missed_detections_and_restart_at_scene_cuts():
    frames = [[Face(50, 50, 40, 40), Face(350, 60, 40, 40)] for _ in range(60)]
    for fi in range(20, 30):  # left face not detected for 0.4 s (head turned, hand in front)
        frames[fi] = [Face(350, 60, 40, 40)]
    shots = [0] * 40 + [1] * 20
    tracks = build_tracks(frames, 480, shots, fps=25)
    assert sorted((t.shot, round(t.median_cx)) for t in tracks) == [(0, 70), (0, 370), (1, 70), (1, 370)]
    left = next(t for t in tracks if t.shot == 0 and t.median_cx < 200)
    assert all(fi in left.faces for fi in range(0, 40))  # the gap is bridged...
    assert not any(fi in left.detected for fi in range(20, 30))  # ...but not counted as detections
    assert max(left.faces) < 40  # a track never crosses a scene cut


def _two_tracks(n: int) -> list[Track]:
    tracks = [Track(0), Track(1)]
    for fi in range(n):
        tracks[0].faces[fi] = Face(50, 50, 40, 40)
        tracks[1].faces[fi] = Face(350, 60, 40, 40)
    return tracks


def test_speaker_switch_needs_evidence_and_ignores_short_interruptions():
    fps, n = 25, 300
    tracks = _two_tracks(n)
    ev = np.zeros((2, n), dtype=np.float32)
    ev[0, 0:140], ev[1, 0:140] = 0.8, -0.4  # A talks
    ev[0, 60:66], ev[1, 60:66] = -0.4, 0.8  # B: a quick "ja" (0.24 s) -> must not steal the frame
    ev[:, 140:160] = 0.0  # pause: nobody talks -> keep A
    ev[0, 160:], ev[1, 160:] = -0.4, 0.8  # B takes over
    chosen = choose_speakers(tracks, ev, [0] * n, fps, 480)
    assert all(c == 0 for c in chosen[:140])
    assert all(c == 1 for c in chosen[175:])
    switch = next(i for i, c in enumerate(chosen) if c == 1)
    assert 140 <= switch <= 175  # around where B starts (somewhere in the pause), never during A's turn
    assert sum(1 for a, b in zip(chosen, chosen[1:], strict=False) if a != b) == 1


def test_speaker_choice_without_any_evidence_keeps_one_logical_face():
    fps, n = 25, 100
    tracks = _two_tracks(n)
    tracks[1].faces = {fi: Face(220, 60, 60, 60) for fi in range(n)}  # bigger and more central
    chosen = choose_speakers(tracks, np.zeros((2, n), dtype=np.float32), [0] * n, fps, 480)
    assert set(chosen) == {1}
    for fi in range(50, n):  # it leaves the picture -> stay on a face that is there (no ping-pong)
        del tracks[1].faces[fi]
    chosen = choose_speakers(tracks, np.zeros((2, n), dtype=np.float32), [0] * n, fps, 480)
    assert set(chosen[60:]) == {0} and sum(1 for a, b in zip(chosen, chosen[1:], strict=False) if a != b) <= 1


def test_lips_in_sync_with_the_voice_beat_more_motion():
    """A silent person chewing (more mouth motion) must lose against the one whose lips follow the voice."""
    fps, n = 25, 250
    rng = np.random.default_rng(3)
    syll = np.repeat(rng.uniform(0, 1, n // 5) > 0.35, 5).astype(float)  # 0.2 s syllables and gaps
    env = np.where(syll > 0, -18.0, -60.0) + rng.normal(0, 1.5, n)
    t = np.arange(n) / fps
    tracks = _two_tracks(n)
    for fi in range(n):
        tracks[0].openness[fi] = 0.1 + 0.5 * syll[max(0, fi - 1)] + rng.normal(0, 0.03)
        tracks[0].activity[fi] = 0.05 + 0.1 * abs(syll[fi] - syll[max(0, fi - 1)])
        tracks[1].openness[fi] = 0.3 + 0.3 * np.sin(2 * np.pi * 1.7 * t[fi]) + rng.normal(0, 0.03)  # chewing
        tracks[1].activity[fi] = 0.12
    speech = syll > 0
    ev = speaker_evidence(tracks, n, speech, env, fps)
    assert ev[0, speech].mean() > ev[1, speech].mean() + 0.5
    assert np.all(ev[:, ~speech] == 0)  # no evidence while nobody talks
    assert choose_speakers(tracks, ev, [0] * n, fps, 480).count(0) == n


def test_camera_path_eases_between_speakers_and_cuts_with_the_video():
    src_w, crop_w = 1920, 608
    # output time = frame / 25; speaker on the left, then on the right, then a scene cut
    times = [i * 0.04 for i in range(400)]
    targets = [(0, 150.0, True), (100, 500.0, False), (250, 120.0, True)]  # analysis px (scale 3)
    kf = build_keyframes(targets, times, 3.0, src_w, crop_w)
    plan = CropPlan("face", src_w, 1080, crop_w, 1080, keyframes=kf)
    xs = [plan.x_at(i / 100) for i in range(1600)]
    assert all(0 <= x <= src_w - crop_w for x in xs)  # never outside the picture (no black bars)
    assert abs(xs[150] + crop_w / 2 - 450) < 4  # centred on the first speaker
    assert abs(xs[900] + crop_w / 2 - 1500) < 4  # ...then on the second
    steps = [abs(b - a) for a, b in zip(xs, xs[1:], strict=False)]
    cut = 1000  # t = 10.0 s
    assert max(steps[: cut - 1]) < 0.035 * crop_w  # smooth pan (< ~3.5 crop widths per second), no jumps
    assert steps[cut - 1] > 0.5 * crop_w  # a real scene cut is followed with a cut, not a pan
    pan_start = next(i for i, x in enumerate(xs) if x != xs[0]) / 100
    assert 3.8 <= pan_start <= 4.0  # starts just before the new speaker (t = 4.0 s)


def test_scene_cut_during_a_pan_cuts_the_pan_short():
    times = [i * 0.04 for i in range(200)]
    # a pan to the right starts at frame 50 (2.0 s); the video cuts at frame 60 (2.4 s), mid-pan
    kf = build_keyframes([(0, 100.0, True), (50, 500.0, False), (60, 300.0, True)], times, 3.0, 1920, 608)
    plan = CropPlan("face", 1920, 1080, 608, 1080, keyframes=kf)
    assert plan.x_at(2.4) == 900 - 304 and plan.x_at(4.0) == 900 - 304  # right after the cut: the new target
    xs = [plan.x_at(i / 100) for i in range(190, 240)]
    assert xs == sorted(xs) and xs[-1] < 900  # before the cut: still easing towards the old target


def test_crop_expression_matches_camera_path():
    plan = CropPlan("face", 1920, 1080, 608, 1080, keyframes=[(0.0, 100), (3.5, 900), (7.0, 400)])
    assert plan.x_expression() == "if(lt(t,3.500),100,if(lt(t,7.000),900,400))"  # hard cuts (old plans)
    assert CropPlan("face", 1920, 1080, 608, 1080).x_expression() == "656"
    eased = CropPlan("face", 1920, 1080, 608, 1080, keyframes=[(0.0, 100), (2.0, 900, 0.8), (6.0, 400)])
    expr = eased.x_expression()

    def ffmpeg_eval(t: float) -> float:  # tiny evaluator for the two functions the expression uses
        env = {"_if": lambda c, a, b: a if c else b, "lt": lambda a, b: a < b, "t": t}
        return float(eval(expr.replace("if(", "_if("), env))

    for t in (0.0, 1.99, 2.0, 2.2, 2.4, 2.79, 2.8, 4.0, 5.99, 6.0, 9.0):
        assert abs(ffmpeg_eval(t) - eased.x_at(t)) < 1.0, t
    assert eased.x_at(2.4) == pytest.approx(500, abs=1)  # half way at half time
    assert eased.x_at(2.04) - eased.x_at(2.0) < eased.x_at(2.44) - eased.x_at(2.40)  # eases in


def test_ffmpeg_accepts_the_eased_crop_expression(tmp_path, ffmpeg_available):
    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    plan = CropPlan("face", 640, 360, 202, 360, keyframes=[(0.0, 0), (0.4, 400, 0.6), (1.5, 100)])
    out = tmp_path / "pan.mp4"
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
        "-vf", f"crop=w={plan.crop_w}:h={plan.crop_h}:x='{plan.x_expression()}':y=0", "-c:v", "libx264", str(out),
    ])
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (202, 360)


@pytest.mark.parametrize("preset,layout", [("capcut", "auto"), ("dynamic", "center"), ("capcut", "fit_blur"), ("none", "center")])
def test_render_vertical_clip(test_video, tmp_path, preset, layout):
    info = ffmpeg.probe(test_video)
    words = [(5.0 + i * 0.4, 5.35 + i * 0.4, w) for i, w in enumerate("Wacht wat dit is echt niet normaal".split())]
    res = render_clip(
        test_video, info, [(4.9, 8.0), (9.0, 12.0)], words, tmp_path / "o.mp4", tmp_path / "o.jpg",
        caption_preset=preset, layout=layout, emphasis=["normaal"], title="Test titel",
    )
    out = ffmpeg.probe(res.video)
    assert (out.width, out.height) == (1080, 1920)
    assert abs(out.duration - 6.1) < 0.25
    assert out.has_audio
    assert res.thumbnail.exists()


def test_load_profile_from_file(test_video):
    prof = load_profile(test_video)
    assert 29 <= prof.duration <= 31
    assert any(8.5 <= t <= 11.5 for t, _ in find_peaks(prof))


def test_real_face_on_flat_background_is_tracked(tmp_path):
    """Regression: global histogram equalisation washed faces out on flat backgrounds (-> no crop).

    tests/fixtures/face.jpg is a crop of NASA's public-domain portrait of Eileen Collins.
    """
    from pathlib import Path

    from app.video.reframe import plan_crop

    face = Path(__file__).parent / "fixtures" / "face.jpg"
    src = tmp_path / "flat.mp4"
    # 1920x1080, flat background, face on the right for 3 s, then (scene cut) on the left for 3 s
    ffmpeg.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x2f4f6b:s=1920x1080:r=25:d=6",
        "-loop", "1", "-i", str(face), "-filter_complex",
        "[1:v]scale=380:380[f];[0:v][f]overlay=x='if(lt(t,3),1400,150)':y=300[v]",
        "-map", "[v]", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src),
    ])
    plan = plan_crop(src, ffmpeg.probe(src), [(0.0, 6.0)], "auto", scene_cuts=[3.0])
    assert plan.layout == "face"
    xs = [k[1] for k in plan.keyframes]
    face_cx_right, face_cx_left = 1400 + 190, 150 + 190
    assert abs(xs[0] + plan.crop_w / 2 - face_cx_right) < 200
    assert abs(xs[-1] + plan.crop_w / 2 - face_cx_left) < 200
    assert 2.5 <= plan.keyframes[-1][0] <= 3.5  # the crop follows the scene cut


def _talking_pair(tmp_path, ffmpeg_available):
    """640x360 video of two people (copies of the public-domain fixture face): A talks 0.3-2.9 s while B
    chews silently, then B talks 3.3-5.9 s. Mouths open with the loudness of their own voice; both sway."""
    import subprocess
    import wave
    from pathlib import Path

    import cv2

    if not ffmpeg_available:
        pytest.skip("ffmpeg not installed")
    fps, dur, sr = 25, 6.2, 16000
    rng = np.random.default_rng(7)
    loud = np.zeros(int(dur * 1000))  # per millisecond: 0..1
    for a, b in ((0.3, 2.9), (3.3, 5.9)):
        t = a
        while t < b:
            d = rng.uniform(0.12, 0.26)
            loud[int(t * 1000): int(min(b, t + d) * 1000)] = rng.uniform(0.6, 1.0)
            t += d + rng.uniform(0.05, 0.14)
    ts = np.arange(int(dur * sr)) / sr
    amp = loud[np.minimum((ts * 1000).astype(int), len(loud) - 1)]
    voice = sum(np.sin(2 * np.pi * 140 * k * ts) / k for k in range(1, 6)) + 0.3 * rng.normal(0, 1, len(ts))
    pcm = (0.25 * np.convolve(amp, np.ones(160) / 160, mode="same") * voice).clip(-1, 1)
    wav = tmp_path / "pair.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes((pcm * 32767).astype(np.int16).tobytes())

    face = cv2.resize(cv2.imread(str(Path(__file__).parent / "fixtures" / "face.jpg")), (200, 200))
    src = tmp_path / "pair.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "640x360", "-r", str(fps),
           "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-shortest", str(src)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(int(dur * fps)):
        t = i / fps
        frame = np.full((360, 640, 3), (107, 79, 47), np.uint8)
        for who, x0, turn in (("A", 40, (0.3, 2.9)), ("B", 400, (3.3, 5.9))):
            img = face.copy()
            if turn[0] <= t < turn[1]:
                opening = float(loud[min(len(loud) - 1, int((t + 0.02) * 1000))])
            elif who == "B" and t < 2.9:
                opening = 0.5 + 0.5 * np.sin(2 * np.pi * 1.7 * t)  # chewing: lots of motion, no voice
            else:
                opening = 0.0
            if opening > 0.05:
                cv2.ellipse(img, (92, int(80 + 3 * opening)), (9, max(1, int(7 * opening))), 0, 0, 360, (40, 25, 45), -1)
            dx, dy = int(round(2 * np.sin(2.1 * t + x0))), int(round(2 * np.sin(1.3 * t)))
            frame[60 + dy: 260 + dy, x0 + dx: x0 + dx + 200] = img
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return src


@pytest.mark.parametrize("detector", ["haar", "yunet"])
def test_reframe_follows_the_person_who_talks(tmp_path, ffmpeg_available, monkeypatch, detector):
    from pathlib import Path

    from app.config import get_settings
    from app.video.reframe import plan_crop

    if detector == "yunet":  # only where the (git-ignored) model was downloaded, e.g. a developer machine
        model = Path(__file__).resolve().parents[2] / "data" / "models" / "face_detection_yunet.onnx"
        if not model.exists():
            pytest.skip("YuNet model not downloaded")
        monkeypatch.setattr(get_settings(), "face_model_path", model)
    src = _talking_pair(tmp_path, ffmpeg_available)
    plan = plan_crop(src, ffmpeg.probe(src), [(0.0, 6.2)], "auto")
    assert plan.layout == "face" and plan.detector == detector
    centre = [plan.x_at(i / 25) + plan.crop_w / 2 for i in range(155)]
    a_cx, b_cx = 40 + 91, 400 + 91
    assert all(abs(c - a_cx) < 0.12 * plan.crop_w for c in centre[10:65])  # A talks (B chews): A in the middle
    assert all(abs(c - b_cx) < 0.12 * plan.crop_w for c in centre[100:150])  # B talks: B in the middle
    moved = next(i for i, c in enumerate(centre) if abs(c - centre[0]) > 10) / 25
    assert 2.6 <= moved <= 3.6  # the camera goes to B when B starts, not before
    assert max(abs(b - a) for a, b in zip(centre, centre[1:], strict=False)) < 0.12 * plan.crop_w  # no jumps
    assert plan.stats["speaker_switches"] == 1
    assert plan.stats["speaker_in_frame"] > 0.85  # the rest is the pan itself, while B starts talking
