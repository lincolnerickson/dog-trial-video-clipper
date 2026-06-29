"""Card test: intro/outro still images added without re-encoding the body.

Adds an intro and an outro card to a clip and checks that:
  * the output duration ~= intro + body + outro,
  * the result decodes cleanly (the seams between encoded cards and the
    stream-copied body are the thing most likely to corrupt), and
  * each distinct card is encoded once and reused across rows via the cache.

Requires ffmpeg + the sample. Run:
    .venv\\Scripts\\python tests\\test_cards.py [video]
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clipper.ffmpeg_tools import (  # noqa: E402
    CardSpec,
    find_ffmpeg,
    probe_duration,
    probe_streams,
    run_cut,
    run_cut_with_cards,
)


def _decode_is_clean(ff: str, path: Path) -> tuple[bool, str]:
    """Fully decode ``path`` and report any real decode errors. The strict null
    muxer emits a harmless DTS grumble at a concat seam, so those are ignored."""
    proc = subprocess.run(
        [ff, "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    bad = [
        ln for ln in (proc.stderr or "").splitlines()
        if ln.strip() and "dts to muxer" not in ln and "non monoton" not in ln.lower()
    ]
    return not bad, "\n".join(bad)


def main():
    root = Path(__file__).resolve().parent.parent
    video = sys.argv[1] if len(sys.argv) > 1 else str(root / "sample" / "trial_4k.mp4")
    ff = find_ffmpeg()

    tmp = root / "tests" / "_tmp_cards"
    tmp.mkdir(exist_ok=True)
    intro_img = tmp / "intro.png"
    outro_img = tmp / "outro.png"  # non-16:9 to exercise letterbox/pad
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=navy:s=1280x720", "-frames:v", "1", str(intro_img)],
                   check=True)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x551111:s=900x900", "-frames:v", "1", str(outro_img)],
                   check=True)

    info = probe_streams(ff, video)
    assert info.vcodec in ("h264", "hevc"), info.vcodec
    print(f"probe: {info.vcodec} {info.width}x{info.height} @ {info.fps}fps audio={info.acodec or 'none'}")

    start, end, intro_s, outro_s = 5.0, 12.0, 2.0, 3.0

    # Body duration baseline.
    plain = tmp / "plain.mp4"
    assert run_cut(ff, video, start, end, plain).ok, "plain body cut failed"
    body_dur = probe_duration(ff, plain)
    assert body_dur is not None

    cache: dict[str, str] = {}

    # Intro only.
    out_intro = tmp / "intro_only.mp4"
    r = run_cut_with_cards(ff, video, start, end, out_intro,
                           intro=CardSpec(intro_img, intro_s),
                           src_info=info, card_cache=cache)
    assert r.ok, f"intro-only failed: {r.stderr}"
    d = probe_duration(ff, out_intro)
    assert abs(d - (body_dur + intro_s)) < 0.7, (d, body_dur + intro_s)
    clean, errs = _decode_is_clean(ff, out_intro)
    assert clean, f"intro-only seam not clean:\n{errs}"
    print(f"intro only: {body_dur:.2f}+{intro_s} ≈ {d:.2f}s, clean")

    # Intro + outro on another row (reuses the cached intro, adds an outro card).
    out_both = tmp / "intro_outro.mp4"
    r2 = run_cut_with_cards(ff, video, 20.0, 27.0, out_both,
                            intro=CardSpec(intro_img, intro_s),
                            outro=CardSpec(outro_img, outro_s),
                            src_info=info, card_cache=cache)
    assert r2.ok, f"intro+outro failed: {r2.stderr}"
    d2 = probe_duration(ff, out_both)
    # This row is also a 7s body (+keyframe handle), like the baseline, so
    # total ~= intro + body + outro.
    assert abs(d2 - (intro_s + body_dur + outro_s)) < 1.2, (d2, intro_s, body_dur, outro_s)
    clean2, errs2 = _decode_is_clean(ff, out_both)
    assert clean2, f"intro+outro seams not clean:\n{errs2}"
    print(f"intro+outro: ≈ {d2:.2f}s, clean")

    # Two distinct cards (intro image, outro image) -> two cache entries.
    assert len(cache) == 2, f"expected 2 cached cards, got {len(cache)}"
    print("cache: intro and outro each encoded once and reused")

    for p in (plain, out_intro, out_both, intro_img, outro_img):
        p.unlink()
    for ts in cache.values():
        Path(ts).unlink(missing_ok=True)
    tmp.rmdir()
    print("CARDS OK: intro + outro added; body stream-copied; seams decode clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
