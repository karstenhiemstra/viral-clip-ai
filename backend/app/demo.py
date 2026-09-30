"""Load demo data so the dashboard can be explored without any API keys.

    python -m app.demo

Generates a synthetic 16:9 test video (ffmpeg) with a Dutch transcript, runs it through the full
pipeline in heuristic mode (or with your LLM if keys are configured) and renders vertical clips.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from app.db import SessionLocal
from app.migrate import upgrade_db
from app.models import Video, VideoStatus
from app.services.media import attach_media, import_subtitles
from app.video import ffmpeg
from app.worker.runner import Worker

LINES = [
    "Hallo allemaal en welkom bij een nieuwe vlog.",
    "Vandaag gaan we naar de supermarkt om boodschappen te doen.",
    "Nou eh dus we lopen hier gewoon een beetje rond.",
    "Wacht wat?! Dit is echt niet normaal, kijk dan!",
    "Die gast heeft duizend euro betaald voor een banaan hahaha.",
    "Ik zweer het, dit is het ergste wat ik ooit heb gezien.",
    "Waarom zou iemand dat in hemelsnaam doen?",
    "Toen bleek opeens dat het een grap was van zijn vrienden.",
    "Hahaha iedereen lag helemaal kapot in de winkel.",
    "Oké, we gaan verder met de boodschappen.",
    "Dit is een rustig stuk waar niet veel gebeurt en we praten over het weer.",
    "Het weer is best oké vandaag, beetje bewolkt en een graad of vijftien.",
    "We lopen naar de kassa en rekenen de boodschappen af.",
    "Eerlijk gezegd vind ik dat hele gedoe met influencers echt belachelijk.",
    "Iedereen doet maar alsof alles perfect is, dat is gewoon fake.",
    "Wat vinden jullie daarvan? Laat het me weten in de comments.",
    "Mijn moeder belde gisteren en ze had groot nieuws.",
    "Ze heeft de loterij gewonnen, een miljoen euro!",
    "Ik schreeuwde zo hard dat de buren kwamen kijken.",
    "Ik heb nog nooit zo hard gehuild van geluk, echt waar.",
    "Vergeet niet te abonneren en tot de volgende keer, doei!",
]
LOUD = {3, 4, 8, 17, 18}


def _timeline(word_dur: float = 0.34, gap: float = 0.07, pause: float = 0.8) -> tuple[list[tuple[float, float, str]], float]:
    cues = []
    t = 1.0
    for line in LINES:
        n = len(line.split())
        dur = n * (word_dur + gap)
        cues.append((t, t + dur, line))
        t += dur + pause
    return cues, t + 1.0


def _srt(cues: list[tuple[float, float, str]]) -> str:
    def ts(x: float) -> str:
        h, rem = divmod(x, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")

    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, start=1))


def build_video(out: Path, cues: list[tuple[float, float, str]], duration: float) -> Path:
    loud = "+".join(f"between(t,{a:.2f},{b:.2f})" for i, (a, b, _) in enumerate(cues) if i in LOUD) or "0"
    speech = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b, _ in cues)
    volume = f"volume='0.02+0.25*({speech})+0.6*({loud})':eval=frame"
    subprocess.run(
        [
            ffmpeg.ffmpeg_bin(), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"gradients=s=1920x1080:r=30:d={duration:.2f}:speed=0.015:c0=0x2b1055:c1=0x7597de:c2=0xff5a2e:c3=0x0b0b10",
            "-f", "lavfi", "-i", f"sine=f=190:d={duration:.2f},{volume}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out),
        ],
        check=True,
    )
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not ffmpeg.available():
        raise SystemExit("ffmpeg is vereist voor de demo")
    upgrade_db()
    cues, duration = _timeline()
    with SessionLocal() as db, tempfile.TemporaryDirectory() as tmp:
        video = Video(title="Demo: De duurste banaan ooit (supermarkt vlog)", source="upload", status=VideoStatus.DISCOVERED,
                      channel_title="Demo creator")
        db.add(video)
        db.commit()
        import_subtitles(db, video, _srt(cues), "demo.srt", queue_analysis=False)
        src = build_video(Path(tmp) / "demo.mp4", cues, duration)
        attach_media(db, video, src, "upload")
        video_id = video.id
    n = Worker("demo").drain()
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        print(f"\nDemo klaar: {n} jobs verwerkt, video #{v.id} status={v.status}, {len(v.clips)} clips.")
        for c in sorted(v.clips, key=lambda c: -c.viral_score):
            print(f"  #{c.rank}  Viral Score {c.viral_score:5.1f}  {c.start_time:6.1f}-{c.end_time:6.1f}s  {c.status:14s} {c.transcript_text[:70]}")


if __name__ == "__main__":
    main()
