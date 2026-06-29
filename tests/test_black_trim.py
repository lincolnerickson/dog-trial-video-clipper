"""Trailing-black trim test (the DJI auto-split quirk).

Synthesizes a clip that ends in black, checks detect_trailing_black finds where
the black starts, and confirms a concat with that outpoint removes the black at
the splice while an untrimmed concat leaves it. Requires ffmpeg (any build with
libx264 + the blackdetect filter — the bundled imageio-ffmpeg has both).

Run:  .venv\\Scripts\\python tests\\test_black_trim.py
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clipper.ffmpeg_tools import (  # noqa: E402
    concat_videos,
    detect_trailing_black,
    find_ffmpeg,
)

_S = re.compile(r"black_start:([0-9.]+)")
_E = re.compile(r"black_end:([0-9.]+)")


def gen(ff, path, *, content=5.0, black=0.0):
    """A `content`-second testsrc clip, optionally with `black` seconds of black
    appended (one 320x240 30fps H.264 file, 1s GOP)."""
    if black > 0:
        args = [
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={content}",
            "-f", "lavfi", "-i", f"color=c=black:size=320x240:rate=30:duration={black}",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
        ]
    else:
        args = ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={content}"]
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error", *args,
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "30", str(path)],
        check=True,
    )


def black_intervals(ff, path):
    proc = subprocess.run(
        [ff, "-hide_banner", "-i", str(path), "-an",
         "-vf", "blackdetect=d=0.04:pix_th=0.10", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    return list(zip([float(x) for x in _S.findall(proc.stderr)],
                    [float(x) for x in _E.findall(proc.stderr)]))


def main():
    ff = find_ffmpeg()
    tmp = Path(__file__).resolve().parent / "_tmp_black"
    tmp.mkdir(exist_ok=True)
    black_clip = tmp / "black_tail.mp4"
    plain_clip = tmp / "plain.mp4"
    gen(ff, black_clip, content=5.0, black=0.5)   # 5.0s content + 0.5s black
    gen(ff, plain_clip, content=5.0, black=0.0)   # no black at all

    # 1) Detection: trailing black found at ~5.0s; a clean clip reports None.
    op = detect_trailing_black(ff, black_clip)
    assert op is not None and abs(op - 5.0) < 0.3, f"trailing-black outpoint wrong: {op}"
    assert detect_trailing_black(ff, plain_clip) is None, "false positive on a clean clip"
    print(f"OK: detected trailing black starting at {op:.3f}s; clean clip -> None")

    # 2) Trimmed concat: no black at the splice (the hour boundary).
    joined_trim = tmp / "joined_trim.mp4"
    res = concat_videos(ff, [black_clip, black_clip], joined_trim, outpoints=[op, op])
    assert res.ok, f"trimmed concat failed: {res.stderr}"
    near_splice = [(s, e) for s, e in black_intervals(ff, joined_trim)
                   if s < op + 0.4 and e > op - 0.2]
    assert not near_splice, f"black still at the splice after trim: {near_splice}"
    print(f"OK: trimmed join has no black at the splice (~{op:.1f}s)")

    # 3) Control: an untrimmed concat *does* leave black at the splice.
    joined_full = tmp / "joined_full.mp4"
    assert concat_videos(ff, [black_clip, black_clip], joined_full).ok
    has_splice_black = any(abs(s - 5.0) < 0.4 for s, e in black_intervals(ff, joined_full))
    assert has_splice_black, "control join unexpectedly had no black at the splice"
    print("OK: untrimmed control still shows the black flash at the splice")

    for p in (black_clip, plain_clip, joined_trim, joined_full):
        p.unlink()
    tmp.rmdir()
    print("BLACK TRIM OK: trailing black detected and trimmed losslessly at each join")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
