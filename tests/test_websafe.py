"""Web-safe test: every exported clip is a browser-playable H.264 mp4.

Browsers need H.264 (not H.265) and a front-loaded ``moov`` atom (fast-start) to
preview an mp4 without downloading it whole. This checks both:
  * an H.265 source is re-encoded to H.264 (it would not play in Chrome/Firefox
    otherwise) and the result decodes cleanly, and
  * an H.264 source is stream-copied (not needlessly re-encoded) but still made
    web-optimised,
and that in both cases the output is fast-start.

Requires ffmpeg + the sample. Run:
    .venv\\Scripts\\python tests\\test_websafe.py
"""

import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clipper.ffmpeg_tools import (  # noqa: E402
    find_ffmpeg,
    find_h264_encoder,
    probe_streams,
    run_cut,
)


def _is_faststart(path: Path) -> bool:
    """True if the mp4's moov atom comes before mdat (web progressive playback)."""
    data = path.read_bytes()
    order, i = [], 0
    while i + 8 <= len(data):
        size = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8].decode("latin1")
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = len(data) - i
        order.append(typ)
        if size < hdr:
            break
        i += size
    return "moov" in order and "mdat" in order and order.index("moov") < order.index("mdat")


def _decode_is_clean(ff: str, path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [ff, "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    bad = [
        ln for ln in (proc.stderr or "").splitlines()
        if ln.strip() and "dts to muxer" not in ln and "non monoton" not in ln.lower()
    ]
    return not bad, "\n".join(bad[:3])


def main():
    root = Path(__file__).resolve().parent.parent
    sample = root / "sample" / "trial_4k.mp4"
    ff = find_ffmpeg()
    h264_enc = find_h264_encoder(ff)
    print(f"h264 encoder picked: {h264_enc}")

    tmp = root / "tests" / "_tmp_websafe"
    tmp.mkdir(exist_ok=True)

    # Build a small H.265 source (the case browsers can't preview).
    hevc_src = tmp / "src_hevc.mp4"
    subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-y", "-t", "3", "-i", str(sample),
         "-vf", "scale=640:-2", "-c:v", "libx265", "-tag:v", "hvc1", "-crf", "30",
         "-preset", "ultrafast", "-c:a", "aac", str(hevc_src)],
        check=True,
    )
    hevc_info = probe_streams(ff, hevc_src)
    assert hevc_info.vcodec == "hevc", f"expected an HEVC source, got {hevc_info.vcodec}"

    # Web-safe it: must become H.264, fast-start, and decode cleanly.
    out_hevc = tmp / "hevc_websafe.mp4"
    res = run_cut(ff, hevc_src, 0.0, 3.0, out_hevc,
                  web_safe=True, encoder=h264_enc, src_info=hevc_info)
    assert res.ok, f"web-safe re-encode failed: {res.stderr}"
    info = probe_streams(ff, out_hevc)
    assert info.vcodec == "h264", f"HEVC was not converted to H.264 (got {info.vcodec})"
    assert _is_faststart(out_hevc), "web-safe output is not fast-start"
    clean, errs = _decode_is_clean(ff, out_hevc)
    assert clean, f"web-safe output did not decode cleanly:\n{errs}"
    print("HEVC source -> H.264, fast-start, clean decode")

    # Web-safe an already-H.264 source: stays H.264, fast-start, and is copied
    # (so it is quick -- no needless re-encode).
    src_info = probe_streams(ff, sample)
    out_h264 = tmp / "h264_websafe.mp4"
    res2 = run_cut(ff, sample, 5.0, 9.0, out_h264,
                   web_safe=True, encoder="libx264", src_info=src_info)
    assert res2.ok, f"web-safe copy failed: {res2.stderr}"
    info2 = probe_streams(ff, out_h264)
    assert info2.vcodec == "h264", info2.vcodec
    assert _is_faststart(out_h264), "web-safe (copy) output is not fast-start"
    assert "-c:v" in res2.command and res2.command[res2.command.index("-c:v") + 1] == "copy", \
        "an H.264 source should be stream-copied, not re-encoded"
    print("H.264 source -> stream-copied, fast-start (no needless re-encode)")

    # Smaller (HEVC) delivery re-encode: the source becomes HEVC at the chosen
    # CRF, fast-start, decodes clean, and is clearly smaller than a stream copy.
    out_copy = tmp / "copy.mp4"
    assert run_cut(ff, sample, 5.0, 8.0, out_copy, video_mode="copy").ok
    out_smaller = tmp / "smaller_hevc.mp4"
    res3 = run_cut(ff, sample, 5.0, 8.0, out_smaller,
                   video_mode="hevc", crf=28, preset="ultrafast",
                   encoder="libx265", src_info=src_info)
    assert res3.ok, f"hevc re-encode failed: {res3.stderr}"
    info3 = probe_streams(ff, out_smaller)
    assert info3.vcodec == "hevc", f"expected HEVC, got {info3.vcodec}"
    assert _is_faststart(out_smaller), "hevc output is not fast-start"
    clean3, errs3 = _decode_is_clean(ff, out_smaller)
    assert clean3, f"hevc output did not decode cleanly:\n{errs3}"
    assert out_smaller.stat().st_size < out_copy.stat().st_size, \
        "HEVC re-encode should be smaller than a stream copy"
    print("source -> smaller HEVC, fast-start, clean decode")

    for p in (hevc_src, out_hevc, out_h264, out_copy, out_smaller):
        p.unlink()
    tmp.rmdir()
    print("WEB-SAFE OK: H.264 web-safe + smaller-HEVC re-encode both clean & fast-start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
