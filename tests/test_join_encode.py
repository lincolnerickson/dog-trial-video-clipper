"""Encode-during-join: concat_videos can re-encode a recording to delivery-quality
HEVC (at a target bitrate, with a short GOP) so that clips cut from the result are
an instant stream copy -- the whole point of preparing recordings overnight.

Run: python tests/test_join_encode.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipper.ffmpeg_tools import (
    concat_videos,
    find_ffmpeg,
    find_hevc_encoder,
    probe_streams,
    run_cut,
)

SRC = Path(__file__).resolve().parents[1] / "test.MP4"


def main() -> int:
    if not SRC.exists():
        print(f"SKIP: no sample at {SRC}")
        return 0
    ff = find_ffmpeg()
    enc = find_hevc_encoder(ff)
    tmp = Path(tempfile.mkdtemp())
    ch1, ch2 = tmp / "c1.mp4", tmp / "c2.mp4"
    out, clip = tmp / "delivery.mp4", tmp / "clip.mp4"
    try:
        # Two "chapters" of one recording (lossless copies, so codecs match).
        run_cut(ff, SRC, 100.0, 106.0, ch1, video_mode="copy")
        run_cut(ff, SRC, 200.0, 206.0, ch2, video_mode="copy")

        # Lossless copy-join stays copy (no encoder args) ...
        cp = concat_videos(ff, [ch1, ch2], tmp / "copy.mp4")
        assert cp.ok, cp.stderr

        # ... and the encode path produces HEVC at the target with a short GOP.
        res = concat_videos(ff, [ch1, ch2], out, encoder=enc, bitrate=30000, gop=60)
        assert res.ok, f"encode-join failed: {res.stderr}"
        info = probe_streams(ff, out)
        assert info.vcodec == "hevc", f"expected hevc, got {info.vcodec}"

        # A clip cut from the encoded join is an instant stream copy, still HEVC.
        c = run_cut(ff, out, 3.0, 10.0, clip, video_mode="copy")
        assert c.ok, c.stderr
        assert probe_streams(ff, clip).vcodec == "hevc"

        print("JOIN-ENCODE OK: recording re-encoded to HEVC; clips copy out instantly")
        return 0
    finally:
        for p in tmp.glob("*"):
            p.unlink()
        tmp.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
