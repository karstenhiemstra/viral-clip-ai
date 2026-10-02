"""Editing the captions of a clip: text, timing, split/merge, add/delete, saving and rendering."""

import pytest

from app.models import Clip, ClipStatus
from app.video.captions import (
    PRESETS,
    CaptionError,
    CaptionWord,
    auto_cues,
    build_ass,
    cues_to_groups,
    group_words,
    validate_cues,
)
from app.worker.runner import Worker

SPOKEN = "Dit is echt heel grappig jongens. Wacht maar tot je dit ziet".split()


def _words(start: float = 0.0) -> list[CaptionWord]:
    out, t = [], start
    for w in SPOKEN:
        out.append(CaptionWord(round(t, 2), round(t + 0.3, 2), w))
        t += 0.4
    return out


def _dialogues(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue: 0,")]


def test_automatic_cues_are_the_captions_that_get_rendered():
    words = _words()
    cues = auto_cues(words, "dynamic")
    assert [c["text"] for c in cues] == [" ".join(w.text for w in g) for g in group_words(words, PRESETS["dynamic"])]
    assert all(0 <= c["start"] < c["end"] for c in cues)
    assert all(a["end"] <= b["start"] for a, b in zip(cues, cues[1:], strict=False))  # never overlapping


def test_automatic_cues_can_always_be_saved_unchanged():
    """Words out of order or two people at once (a "ja." inside someone else's sentence) or words running past
    the clip end must still give valid cues."""
    words = [CaptionWord(0.0, 0.3, "Hallo"), CaptionWord(2.0, 2.1, "ja."), CaptionWord(0.4, 0.8, "allemaal."),
             CaptionWord(2.04, 2.3, "en"), CaptionWord(2.35, 2.6, "toen"),
             CaptionWord(3.6, 3.9, "einde"), CaptionWord(4.2, 4.6, "erna")]
    cues = auto_cues(words, "dynamic", duration=4.0)
    assert validate_cues(cues, duration=4.0) == cues
    assert [c["text"] for c in cues] == ["Hallo allemaal.", "ja. en toen", "einde"]
    assert cues[-1]["end"] <= 4.0


@pytest.mark.parametrize(
    ("cue", "message"),
    [
        ({"start": -0.5, "end": 1.0, "text": "Hoi"}, "vóór het begin"),
        ({"start": 2.0, "end": 1.0, "text": "Hoi"}, "eindtijd moet na de starttijd"),
        ({"start": 1.0, "end": 1.0, "text": "Hoi"}, "eindtijd moet na de starttijd"),
        ({"start": 1.0, "end": 9.5, "text": "Hoi"}, "na het einde van de clip"),
        ({"start": 1.0, "end": 2.0, "text": "   "}, "is leeg"),
        ({"start": 1.0, "end": 2.0, "text": "x" * 201}, "te lang"),
    ],
)
def test_invalid_caption_timing_or_text_is_refused(cue, message):
    with pytest.raises(CaptionError, match=message):
        validate_cues([cue], duration=8.0)


def test_valid_captions_are_cleaned_sorted_and_must_not_overlap():
    cues = validate_cues([
        {"start": 3.0, "end": 4.0, "text": "tweede\n  regel "},
        {"start": 0.0, "end": 3.05, "text": "Eerste"},  # a hair of overlap: trimmed
    ], duration=8.0)
    assert cues == [{"start": 0.0, "end": 3.0, "text": "Eerste"}, {"start": 3.0, "end": 4.0, "text": "tweede regel"}]
    with pytest.raises(CaptionError, match="overlappen"):
        validate_cues([{"start": 0.0, "end": 3.0, "text": "a"}, {"start": 2.0, "end": 4.0, "text": "b"}], duration=8.0)


def test_edited_captions_render_with_the_existing_style_and_their_own_times():
    words = _words()
    cues = [
        {"start": 0.0, "end": 1.1, "text": "Dit is echt"},  # split ...
        {"start": 1.2, "end": 2.0, "text": "heel grappig"},  # ... of "Dit is echt heel grappig"
        {"start": 2.0, "end": 3.1, "text": "jongens! Wacht maar"},  # merged and a typo fixed
        {"start": 5.0, "end": 6.0, "text": "nieuw toegevoegd"},  # added where nothing was said
    ]
    groups = cues_to_groups(cues, words)
    assert [" ".join(w.text for w in g) for g in groups] == [c["text"] for c in cues]
    assert [(g[0].start, g[-1].end) for g in groups] == [(c["start"], c["end"]) for c in cues]
    # same number of words as spoken in that time: each word keeps its spoken timing (karaoke stays in sync)
    assert [w.start for w in groups[1]] == [1.2, 1.6] and groups[0][1].start == words[1].start
    # more/other words: the cue's time is shared out, in order and inside the cue
    assert all(g[i].end <= g[i + 1].start + 1e-9 for g in groups for i in range(len(g) - 1))

    ass = build_ass(words, "dynamic", y=1300, groups=groups)
    lines = _dialogues(ass)
    assert len(lines) == sum(len(g) for g in groups)  # karaoke: one event per word, as before
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,") and "DIT" in lines[0]  # uppercase style kept
    assert any("0:00:06.00,Cap" in line and "TOEGEVOEGD" in line for line in lines)  # ends exactly at its end
    assert "JONGENS!" in ass and "Style: Cap,Montserrat Black,98" in ass  # same font/size/colours as before
    assert not any("TOT" in line for line in lines)  # deleted words are gone
    assert _dialogues(build_ass(words, "dynamic", y=1300, groups=[])) == []  # all captions deleted


def test_automatic_captions_unchanged_without_edits():
    words = _words()
    assert build_ass(words, "bold_white", y=1300) == build_ass(words, "bold_white", y=1300, groups=None)
    assert len(_dialogues(build_ass(words, "bold_white", y=1300))) == len(group_words(words, PRESETS["bold_white"]))


def _clip_on_test_video(client, db, test_video) -> Clip:
    r = client.post("/api/videos/upload", files={"file": ("bron.mp4", test_video.open("rb"), "video/mp4")}, data={"title": "Bron"})
    assert r.status_code == 201, r.text
    words = [[w.start + 5.0, w.end + 5.0, w.text] for w in _words()]
    clip = Clip(
        video_id=r.json()["id"], start_time=5.0, end_time=11.0, duration=6.0, segments=[[5.0, 11.0]], words=words,
        transcript_text=" ".join(SPOKEN), viral_score=80, caption_preset="dynamic", layout="center",
        status=ClipStatus.PENDING_RENDER,
    )
    db.add(clip)
    db.commit()
    return clip


def test_caption_editor_api_save_preview_and_render(client, db, test_video, monkeypatch):
    import app.video.render as render_mod

    rendered: list[str] = []
    original = render_mod.build_ass

    def spy(*args, **kwargs):
        ass = original(*args, **kwargs)
        rendered.append(ass)
        return ass

    monkeypatch.setattr(render_mod, "build_ass", spy)
    clip = _clip_on_test_video(client, db, test_video)
    client.post(f"/api/clips/{clip.id}/render")
    Worker("t").drain()  # first render with the automatic captions
    assert client.get(f"/api/clips/{clip.id}").json()["status"] == "ready"
    assert "GRAPPIG" in rendered[-1]

    auto = client.get(f"/api/clips/{clip.id}/captions").json()
    assert auto["custom"] is False and auto["duration"] == 6.0 and auto["caption_preset"] == "dynamic"
    assert auto["captions"][0]["start"] == 0.0 and auto["captions"][0]["text"].startswith("Dit is")

    edited = [
        {"start": 0.0, "end": 1.1, "text": "Dit is echt"},
        {"start": 1.2, "end": 2.0, "text": "heel grappig"},
        {"start": 2.0, "end": 3.4, "text": "jongens, wacht maar"},
        {"start": 4.5, "end": 5.5, "text": "Zelf toegevoegd"},
    ]
    r = client.put(f"/api/clips/{clip.id}/captions", json={"captions": edited})
    assert r.status_code == 200, r.text
    assert r.json()["custom"] is True and r.json()["captions"] == edited
    # stored on the clip: still there on a new request (page refresh)
    assert client.get(f"/api/clips/{clip.id}/captions").json()["captions"] == edited
    assert client.get(f"/api/clips/{clip.id}").json()["captions_custom"] is True

    bad = client.put(f"/api/clips/{clip.id}/captions", json={"captions": [{"start": 2.0, "end": 1.0, "text": "x"}]})
    assert bad.status_code == 422 and "eindtijd" in bad.json()["detail"]
    assert client.get(f"/api/clips/{clip.id}/captions").json()["captions"] == edited  # nothing changed

    # quick preview with (unsaved) changes: the clip itself is not touched
    preview = client.post(f"/api/clips/{clip.id}/captions/preview", json={"captions": edited[:2]})
    assert preview.status_code == 200, preview.text
    media = client.get(preview.json()["preview_url"])
    assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"
    assert "GRAPPIG" in rendered[-1] and "TOEGEVOEGD" not in rendered[-1]
    assert client.get(f"/api/clips/{clip.id}").json()["status"] == "ready"

    # final render uses the saved captions
    client.post(f"/api/clips/{clip.id}/render")
    Worker("t").drain()
    assert client.get(f"/api/clips/{clip.id}").json()["status"] == "ready"
    final = rendered[-1]
    assert "TOEGEVOEGD" in final and "JONGENS" in final and "TOT" not in final  # (style drops commas)
    assert "0:00:04.50" in final and "0:00:05.50" in final

    # back to the automatic captions
    reset = client.put(f"/api/clips/{clip.id}/captions", json={"captions": None}).json()
    assert reset["custom"] is False and reset["captions"] == auto["captions"]


def test_changing_start_or_end_resets_edited_captions(client, db, test_video):
    clip = _clip_on_test_video(client, db, test_video)
    client.put(f"/api/clips/{clip.id}/captions", json={"captions": [{"start": 0.0, "end": 1.0, "text": "Hoi"}]})
    client.patch(f"/api/clips/{clip.id}", json={"start_time": 5.5})
    assert client.get(f"/api/clips/{clip.id}/captions").json()["custom"] is False
