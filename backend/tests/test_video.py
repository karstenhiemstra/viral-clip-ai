import numpy as np
import pytest

from app.video import ffmpeg
from app.video.audio_features import clip_features, compute_profile, find_peaks, load_profile
from app.video.captions import PRESETS, CaptionWord, ass_color, build_ass, caption_y, group_words
from app.video.reframe import CropPlan, Face, _choose_subjects, _segments_from_targets, build_tracks
from app.video.render import render_clip


def test_ass_color():
    assert ass_color("#FF0000") == "&H000000FF"
    assert ass_color("#3CFF6B", 0x80) == "&H806BFF3C"


def test_caption_grouping_and_ass_output():
    words = [CaptionWord(i * 0.4, i * 0.4 + 0.35, t) for i, t in enumerate("Wacht wat?! Dit is echt niet normaal jongens".split())]
    groups = group_words(words, PRESETS["dynamic"])
    assert [" ".join(w.text for w in g) for g in groups][0] == "Wacht wat?!"
    assert all(len(g) <= PRESETS["dynamic"].max_words for g in groups)
    ass = build_ass(words, "dynamic", y=1300, emphasis=["normaal"], title="Hook {titel}")
    assert "PlayResY: 1920" in ass and "WACHT" in ass
    assert ass.count("Dialogue:") >= len(words)  # karaoke: one event per word (+ title)
    assert "\\pos(540,1300)" in ass
    assert "{titel}" not in ass  # braces are escaped
    minimal = build_ass(words, "minimal", y=1300)
    assert "Wacht wat?!" in minimal and minimal.count("Dialogue:") == len(group_words(words, PRESETS["minimal"]))


def test_caption_position_avoids_faces():
    assert caption_y(0.45, 0.2, "face") == int(1920 * 0.68)
    assert caption_y(0.7, 0.3, "face") > int(1920 * 0.68)
    assert caption_y(0.9, 0.4, "face") < 1920 * 0.3  # close-up -> captions above the face
    assert caption_y(None, None, "split") == 960


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


def test_face_tracking_and_speaker_hysteresis():
    frames = []
    for _ in range(40):
        frames.append([Face(50, 50, 40, 40), Face(350, 60, 40, 40)])
    tracks = build_tracks(frames, 480)
    assert len(tracks) == 2
    # Left face talks for the first half, right face for the second half.
    for fi in range(40):
        tracks[0].activity[fi] = 0.08 if fi < 20 else 0.0
        tracks[1].activity[fi] = 0.0 if fi < 20 else 0.08
    chosen = _choose_subjects(tracks, 40, 480)
    assert chosen[5] == tracks[0].tid and chosen[35] == tracks[1].tid
    switches = sum(1 for a, b in zip(chosen, chosen[1:], strict=False) if a != b)
    assert switches == 1


def test_segments_from_targets_are_stable():
    targets = [0.2] * 10 + [0.21, 0.19] * 5 + [0.8] * 12
    times = [i * 0.25 for i in range(len(targets))]
    segs = _segments_from_targets(targets, times, [], min_move=0.08, min_len=1.2)
    assert len(segs) == 2
    assert abs(segs[0][1] - 0.2) < 0.02 and abs(segs[1][1] - 0.8) < 0.02


def test_crop_expression():
    plan = CropPlan("face", 1920, 1080, 608, 1080, keyframes=[(0.0, 100), (3.5, 900), (7.0, 400)])
    assert plan.x_expression() == "if(lt(t,3.500),100,if(lt(t,7.000),900,400))"
    assert CropPlan("face", 1920, 1080, 608, 1080).x_expression() == "656"


@pytest.mark.parametrize("preset,layout", [("dynamic", "auto"), ("bold_white", "center"), ("minimal", "fit_blur"), ("none", "center")])
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
    xs = [x for _, x in plan.keyframes]
    face_cx_right, face_cx_left = 1400 + 190, 150 + 190
    assert abs(xs[0] + plan.crop_w / 2 - face_cx_right) < 200
    assert abs(xs[-1] + plan.crop_w / 2 - face_cx_left) < 200
    assert 2.5 <= plan.keyframes[-1][0] <= 3.5  # the crop follows the scene cut
