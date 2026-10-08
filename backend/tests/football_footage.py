"""Synthetic 'match footage' for the Auto Edit tests: a textured pitch, a player (red) with a ball (white) who
does a dribble to the right (3.0-5.5 s) and, after a camera cut at 8 s (another camera, another part of the
pitch), a sprint to the left (9.5-12 s) with the camera panning along. Calm in between. A static score bar
(top left) stays on screen while the camera pans, like in a broadcast."""

import math
from pathlib import Path

from app.video import ffmpeg

DRIBBLE = (3.0, 5.5)
SPRINT = (9.5, 12.0)
CUT = 8.0
LENGTH = 14.0

# player x (left edge, px in the 1280 wide picture) over time
PLAYER_X = (
    "if(lt(t,3),560,if(lt(t,5.5),560+(t-3)*200+50*sin((t-3)*10),"
    "if(lt(t,8),1060,if(lt(t,9.5),1000,if(lt(t,12),1000-(t-9.5)*300,250)))))"
)


def player_x(t: float) -> float:
    if t < 3:
        return 560
    if t < 5.5:
        return 560 + (t - 3) * 200 + 50 * math.sin((t - 3) * 10)
    if t < 8:
        return 1060
    if t < 9.5:
        return 1000
    if t < 12:
        return 1000 - (t - 9.5) * 300
    return 250


def player_cx(t: float) -> float:
    """Horizontal centre of player + ball, 0..1."""
    return (player_x(t) + 37) / 1280


def make_football_footage(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    pitches = []
    for k, (fx, fy, base) in enumerate(((9, 7, 90), (13, 5, 120))):
        p = out.parent / f"pitch{k}.png"
        ffmpeg.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=2600x720:d=0.04", "-vf",
            f"geq=lum='{base}+14*sin(X/{fx})*sin(Y/{fy})+10*sin((X*0.7+Y)/23)+50*random({k + 1})':cb=100:cr=95,gblur=sigma=2",
            "-frames:v", "1", str(p),
        ], timeout=120)
        pitches.append(p)
    second = LENGTH - CUT
    graph = (
        "[0:v]crop=w=1280:h=720:x=200:y=0,setsar=1[a];"
        "[1:v]crop=w=1280:h=720:x='if(lt(t,1.5),1300,if(lt(t,4),1300-(t-1.5)*120,1000))':y=0,setsar=1[b];"
        "[a][b]concat=n=2:v=1:a=0,setpts=N/25/TB[pitch];"
        "color=c=red:s=50x110:r=25[player];color=c=white:s=16x16:r=25[ball];"
        f"[pitch][player]overlay=x='{PLAYER_X}':y=420:shortest=1[p1];"
        f"[p1][ball]overlay=x='{PLAYER_X}+58':y=512:shortest=1,"
        "drawbox=x=40:y=30:w=240:h=46:color=black@1:t=fill,drawbox=x=48:y=38:w=90:h=30:color=yellow@1:t=fill,"
        "format=yuv420p[v]"
    )
    ffmpeg.run([
        "ffmpeg", "-v", "error", "-y",
        "-loop", "1", "-framerate", "25", "-t", str(CUT), "-i", str(pitches[0]),
        "-loop", "1", "-framerate", "25", "-t", str(second), "-i", str(pitches[1]),
        "-f", "lavfi", "-i", f"sine=frequency=200:duration={LENGTH}",
        "-filter_complex", graph, "-map", "[v]", "-map", "2:a", "-t", str(LENGTH),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-c:a", "aac", "-shortest", str(out),
    ], timeout=300)
    return out
