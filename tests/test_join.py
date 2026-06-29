"""Join test: stream-copy several parts into one file (the GoPro-chapter case).

Cuts the sample into two parts, concatenates them with the concat demuxer, and
checks the joined duration ~= the sum of the parts and the output is non-empty.
Also checks the list-file single-quote escaping. Requires ffmpeg + the sample.

Run:  .venv\\Scripts\\python tests\\test_join.py [video]
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clipper.ffmpeg_tools import (  # noqa: E402
    build_concat_listfile,
    concat_videos,
    find_ffmpeg,
    probe_duration,
    run_cut,
)


def main():
    root = Path(__file__).resolve().parent.parent
    video = sys.argv[1] if len(sys.argv) > 1 else str(root / "sample" / "trial_4k.mp4")
    ff = find_ffmpeg()

    # list-file escaping is correct for awkward paths (unit-level, no ffmpeg).
    body = build_concat_listfile([r"C:\a b\o'brien.mp4"])
    assert "o'\\''brien.mp4" in body, body
    assert body.startswith("file '"), body
    print("list-file escaping OK")

    tmp = root / "tests" / "_tmp_join"
    tmp.mkdir(exist_ok=True)
    p1, p2 = tmp / "part1.mp4", tmp / "part2.mp4"
    assert run_cut(ff, video, 0.0, 5.0, p1).ok, "cut part1 failed"
    assert run_cut(ff, video, 5.0, 12.0, p2).ok, "cut part2 failed"
    d1, d2 = probe_duration(ff, p1), probe_duration(ff, p2)
    print(f"parts: {d1:.2f}s + {d2:.2f}s = {d1 + d2:.2f}s")

    ticks = []
    joined = tmp / "joined.mp4"
    res = concat_videos(
        ff, [p1, p2], joined,
        total_duration=(d1 + d2),
        progress=lambda s, t: ticks.append(s),
    )
    assert res.ok, f"concat failed: {res.stderr}"
    dj = probe_duration(ff, joined)
    print(f"joined: {dj:.2f}s (expected ~{d1 + d2:.2f}s); progress ticks={len(ticks)}")
    assert dj is not None and abs(dj - (d1 + d2)) < 1.0, (dj, d1 + d2)

    # Cleanup.
    for p in (p1, p2, joined):
        p.unlink()
    tmp.rmdir()
    print("JOIN OK: parts concatenated losslessly into one continuous file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
