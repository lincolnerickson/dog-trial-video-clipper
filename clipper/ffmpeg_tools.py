"""Locating ffmpeg and running the actual cuts.

Resolution order for the ffmpeg binary:
  1. ``$CLIPPER_FFMPEG`` environment variable (explicit override)
  2. ``ffmpeg`` on PATH (a real system install -- best codec support)
  3. the binary bundled by the ``imageio-ffmpeg`` pip package (works out of
     the box inside the venv, no system install needed)

Cutting strategy
----------------
Both paths use *input* seeking (``-ss`` before ``-i``) which jumps straight to
the nearest keyframe -- this is what makes a 4K cut take a fraction of a second
instead of decoding the whole file.

* Default (stream copy): ``-ss START -i SRC -t DUR -c copy``.  The clip starts
  at the keyframe at/just before the in-point, so it can begin up to ~1s early.
  That slight head handle is fine (even desirable) for run footage.

* Exact (re-encode): ``-ss START -i SRC -t DUR -c:v ENCODER ...``.  Modern
  ffmpeg decodes from the keyframe and discards pre-roll frames, so the cut is
  frame-accurate at the in-point.  Only the clip itself is re-encoded, so it is
  still fast.  Use this per-row when a clip must start exactly on the mark.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import timecode


class FFmpegNotFound(RuntimeError):
    pass


def find_ffmpeg() -> str:
    """Return a path to an ffmpeg executable or raise :class:`FFmpegNotFound`."""
    override = os.environ.get("CLIPPER_FFMPEG")
    if override and Path(override).exists():
        return override

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass

    raise FFmpegNotFound(
        "ffmpeg not found. Either install it (`winget install Gyan.FFmpeg` on "
        "Windows, `brew install ffmpeg` on macOS), set the CLIPPER_FFMPEG "
        "environment variable to its path, or `pip install imageio-ffmpeg` into "
        "this environment."
    )


@dataclass
class CutResult:
    ok: bool
    output: Path
    command: list[str]
    returncode: int = 0
    stderr: str = ""


def build_cut_command(
    ffmpeg: str,
    src: str | Path,
    start: float,
    end: float,
    out_path: str | Path,
    *,
    exact: bool = False,
    encoder: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    web_safe: bool = False,
    src_info: "StreamInfo | None" = None,
) -> list[str]:
    """Assemble the ffmpeg argument list for one cut (no I/O).

    ``web_safe`` forces a browser-playable H.264 mp4 (yuv420p, ``+faststart``):
    a source that is already web-safe H.264 is stream-copied; anything else
    (e.g. H.265) is re-encoded with ``encoder`` -- which should be an H.264
    encoder, GPU if available (see :func:`find_h264_encoder`). ``src_info`` lets
    the caller skip the re-encode when a copy will do.
    """
    duration = max(end - start, 0.0)
    start_tc = timecode.format_timecode(start)
    dur_tc = timecode.format_timecode(duration)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", start_tc,
        "-i", str(src),
        "-t", dur_tc,
        "-map", "0:v:0",
        "-map", "0:a?",       # include audio if present, don't fail if absent
    ]

    if web_safe:
        # Copy the video only if it is already a browser-safe H.264; otherwise
        # re-encode to H.264. `exact` forces a re-encode (frame-accurate start).
        if src_info is not None and _h264_safe_source(src_info) and not exact:
            audio = "aac" if (src_info.has_audio and src_info.acodec != "aac") else "copy"
            cmd += ["-c:v", "copy", "-c:a", audio, "-avoid_negative_ts", "make_zero"]
        else:
            cmd += (
                ["-c:v", encoder]
                + h264_quality_args(encoder, crf, preset)
                + ["-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac"]
            )
        cmd += ["-movflags", "+faststart"]
    elif exact:
        cmd += [
            "-c:v", encoder,
            "-crf", str(crf),
            "-preset", preset,
            "-c:a", "aac",
        ]
    else:
        cmd += [
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]

    cmd.append(str(out_path))
    return cmd


def run_cut(
    ffmpeg: str,
    src: str | Path,
    start: float,
    end: float,
    out_path: str | Path,
    *,
    exact: bool = False,
    encoder: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    web_safe: bool = False,
    src_info: "StreamInfo | None" = None,
    dry_run: bool = False,
) -> CutResult:
    """Run a single cut. Never raises on ffmpeg failure -- returns CutResult."""
    out_path = Path(out_path)
    cmd = build_cut_command(
        ffmpeg, src, start, end, out_path,
        exact=exact, encoder=encoder, crf=crf, preset=preset,
        web_safe=web_safe, src_info=src_info,
    )
    if dry_run:
        return CutResult(ok=True, output=out_path, command=cmd)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    ok = proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    return CutResult(
        ok=ok,
        output=out_path,
        command=cmd,
        returncode=proc.returncode,
        stderr=(proc.stderr or "").strip(),
    )


# ------------------------------------------------------- intro / outro cards
#
# Goal: put a still image (a branded title card) at the head (intro) and/or tail
# (outro) of every exported clip WITHOUT re-encoding the run footage -- the body
# must stay a fast, lossless stream copy like a normal cut.
#
# The hard part is concatenating a freshly-encoded card onto a stream-copied
# H.264/H.265 body. An mp4 stores the codec parameter sets (SPS/PPS/VPS) once in
# the container header, so a plain concat of two mp4s with different parameter
# sets makes the other segment decode as garbage. The fix is to route every
# piece through MPEG-TS, which carries the parameter sets in-band at every
# keyframe: each segment then decodes against its own parameter sets. So:
#
#   1. cut the body straight to a .ts (stream copy, + *_mp4toannexb bitstream
#      filter)  -- or re-encode it there for an `exact` / web-safe row;
#   2. encode each card image to a .ts matched to the body's codec / resolution /
#      frame-rate / pixel-depth (+ silent audio matched to the body's track);
#   3. concat-demux intro + body + outro (in order) into the final .mp4 (copy).
#
# Only H.264 and H.265 sources are supported (what trial cameras produce); other
# codecs are reported as a clear per-row error rather than silently mangled.

_INTRO_BSF = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}
_INTRO_ENCODER = {"h264": "libx264", "hevc": "libx265"}
_CHANNEL_LAYOUTS = {1: "mono", 2: "stereo", 4: "quad", 6: "5.1", 8: "7.1"}


@dataclass
class StreamInfo:
    """The handful of source stream facts the intro pipeline needs to match."""

    vcodec: str = ""          # 'h264' | 'hevc' | other | ''
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    has_audio: bool = False
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0


@dataclass
class CardSpec:
    """A still image shown for ``seconds`` seconds as a clip's intro or outro
    card (an intro is prepended; an outro is appended)."""

    image: str | Path
    seconds: float = 3.0


# Back-compat alias: the cards were originally intro-only.
IntroSpec = CardSpec


_VCODEC_RE = re.compile(r": Video:\s*([A-Za-z0-9_]+)")
_RES_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_FPS_RE = re.compile(r"([\d]+(?:\.[\d]+)?)\s*fps")
_PIXFMT_RE = re.compile(r": Video:[^,]+,\s*([a-z][a-z0-9]+)")
_ACODEC_RE = re.compile(r": Audio:\s*([A-Za-z0-9_]+)")
_HZ_RE = re.compile(r"(\d+)\s*Hz")


def _channels_from(text: str) -> int:
    t = text.lower()
    m = re.search(r"(\d+)\s*channels", t)
    if m:
        return int(m.group(1))
    for token, n in (("7.1", 8), ("5.1", 6), ("quad", 4), ("stereo", 2), ("mono", 1)):
        if token in t:
            return n
    return 0


def probe_streams(ffmpeg: str, path: str | Path) -> StreamInfo:
    """Best-effort first-video + first-audio stream facts, parsed from ffmpeg's
    ``-i`` banner (so no separate ffprobe is needed -- imageio-ffmpeg only ships
    ffmpeg). Missing fields stay at their dataclass defaults."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    info = StreamInfo()
    for line in (proc.stderr or "").splitlines():
        if ": Video:" in line and not info.vcodec:
            m = _VCODEC_RE.search(line)
            if m:
                info.vcodec = m.group(1).lower()
            r = _RES_RE.search(line)
            if r:
                info.width, info.height = int(r.group(1)), int(r.group(2))
            f = _FPS_RE.search(line)
            if f:
                try:
                    info.fps = float(f.group(1))
                except ValueError:
                    pass
            p = _PIXFMT_RE.search(line)
            if p:
                info.pix_fmt = p.group(1)
        elif ": Audio:" in line and not info.has_audio:
            m = _ACODEC_RE.search(line)
            if m:
                info.has_audio = True
                info.acodec = m.group(1).lower()
                hz = _HZ_RE.search(line)
                if hz:
                    info.sample_rate = int(hz.group(1))
                info.channels = _channels_from(line)
    return info


def encoder_codec_family(encoder: str) -> str | None:
    """Map an ffmpeg video encoder name to the codec it produces ('h264'/'hevc').

    Returns None for anything the intro pipeline can't concat (so the caller can
    report it rather than produce a broken file).
    """
    e = (encoder or "").lower()
    if "265" in e or "hevc" in e:
        return "hevc"
    if "264" in e or "avc" in e:
        return "h264"
    return None


# Preference order for the "web-safe" (universal-browser H.264) re-encode: GPU
# encoders first for speed on 4K, then the always-present software fallback.
_H264_ENCODER_CANDIDATES = (
    "h264_nvenc",        # NVIDIA
    "h264_qsv",          # Intel Quick Sync
    "h264_amf",          # AMD
    "h264_videotoolbox",  # Apple
    "libx264",           # software (always works)
)


def find_h264_encoder(ffmpeg: str, candidates: Sequence[str] = _H264_ENCODER_CANDIDATES) -> str:
    """Pick the fastest H.264 encoder that actually works on this machine.

    "Compiled in" (ffmpeg -encoders) is not enough -- a GPU encoder fails at
    runtime without the hardware. So each candidate is proven with a one-frame
    test encode; ``libx264`` is the guaranteed software fallback.
    """
    for enc in candidates:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240",
             "-frames:v", "1", "-c:v", enc, "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            return enc
    return "libx264"


def h264_quality_args(encoder: str, crf: int, preset: str) -> list[str]:
    """Quality/rate-control args for an H.264 encoder, keyed off its family.

    ``crf`` is interpreted as a constant-quality target on every encoder (CQ on
    NVENC, QP on AMF, global_quality on QSV) so one number drives them all.
    """
    e = (encoder or "").lower()
    if "nvenc" in e:
        return ["-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if "qsv" in e:
        return ["-global_quality", str(crf)]
    if "amf" in e:
        return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf)]
    if "videotoolbox" in e:
        return ["-q:v", str(max(1, min(100, 110 - crf * 2)))]
    # libx264 / anything else
    return ["-preset", preset, "-crf", str(crf)]


def _h264_safe_source(info: "StreamInfo") -> bool:
    """True if a source can be stream-copied and still be browser-playable: an
    8-bit 4:2:0 H.264. (10-bit / 4:2:2 H.264 and any H.265 need re-encoding.)"""
    pix = info.pix_fmt or "yuv420p"
    return info.vcodec == "h264" and "420" in pix and "10" not in pix


def _fps_arg(fps: float) -> str:
    """An ffmpeg rate string for ``fps``, snapping NTSC rates to exact fractions
    (so a 59.94 source intro is 60000/1001, not 59.94 -> 2997/50)."""
    if fps <= 0:
        return "30"
    for base in (24, 30, 48, 60, 120):
        if abs(fps - base * 1000 / 1001) < 0.05:
            return f"{base * 1000}/1001"
        if abs(fps - base) < 0.01:
            return str(base)
    if abs(fps - round(fps)) < 0.01:
        return str(int(round(fps)))
    return f"{fps:.5f}"


def _intro_pix_fmt(src_pix_fmt: str) -> str:
    """Match the source's bit depth (the one thing a single mp4 track can't vary
    mid-stream); chroma/range differences on a static card are imperceptible."""
    return "yuv420p10le" if "10" in (src_pix_fmt or "") else "yuv420p"


def _run_quiet(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return proc.returncode, (proc.stderr or "").strip()


def _last_line(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def card_target_key(
    vcodec: str, info: StreamInfo, card: CardSpec
) -> str:
    """Signature of an encoded card so identical ones are reused across a batch.

    Every clip in a batch comes from one source, so a given image+duration is
    encoded once; the image path is in the key so an intro and a (different)
    outro -- or a mix of stream-copy and `exact` body codecs -- get their own.
    """
    return "|".join(map(str, [
        vcodec, info.has_audio, info.sample_rate, info.channels,
        _intro_pix_fmt(info.pix_fmt), info.width, info.height,
        _fps_arg(info.fps), card.seconds, str(card.image),
    ]))


def build_card_ts(
    ffmpeg: str, card: CardSpec, info: StreamInfo, vcodec: str, dst_ts: str | Path
) -> tuple[int, str]:
    """Encode a card image into an MPEG-TS segment matched to ``vcodec`` and
    ``info`` (resolution, frame-rate, bit depth, and a silent audio track that
    matches the body's so the two concat cleanly). Returns (returncode, stderr)."""
    width = info.width or 1920
    height = info.height or 1080
    fps = _fps_arg(info.fps)
    pix = _intro_pix_fmt(info.pix_fmt)
    encoder = _INTRO_ENCODER[vcodec]
    bsf = _INTRO_BSF[vcodec]
    # Letterbox the image to the body's frame (no distortion if aspect differs).
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format={pix}"
    )
    seconds = f"{max(card.seconds, 0.1):g}"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", fps, "-t", seconds, "-i", str(card.image),
    ]
    if info.has_audio:
        sr = info.sample_rate or 48000
        layout = _CHANNEL_LAYOUTS.get(info.channels, "stereo")
        cmd += ["-f", "lavfi", "-t", seconds, "-i", f"anullsrc=channel_layout={layout}:sample_rate={sr}"]
    cmd += ["-map", "0:v:0"]
    if info.has_audio:
        cmd += ["-map", "1:a:0"]
    cmd += ["-vf", vf, "-r", fps, "-c:v", encoder, "-pix_fmt", pix, "-preset", "veryfast", "-crf", "18"]
    if vcodec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    else:
        cmd += ["-profile:v", "high"]
    if info.has_audio:
        sr = info.sample_rate or 48000
        ac = info.channels if info.channels in _CHANNEL_LAYOUTS else 2
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(sr), "-ac", str(ac)]
    cmd += ["-bsf:v", bsf, "-f", "mpegts", str(dst_ts)]
    return _run_quiet(cmd)


def _build_body_ts_cmd(
    ffmpeg: str, src: str | Path, start: float, end: float, dst_ts: str | Path, *,
    vcodec: str, reencode: bool, encoder: str, crf: int, preset: str, reencode_audio: bool,
) -> list[str]:
    """ffmpeg args to cut one body segment to MPEG-TS (annexb).

    ``reencode`` re-encodes the video with ``encoder`` (for an `exact` row or a
    web-safe H.265->H.264 conversion); otherwise the body is stream-copied.
    """
    duration = max(end - start, 0.0)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", timecode.format_timecode(start),
        "-i", str(src),
        "-t", timecode.format_timecode(duration),
        "-map", "0:v:0", "-map", "0:a?", "-dn",
    ]
    if reencode:
        cmd += ["-c:v", encoder]
        if vcodec == "h264":
            cmd += h264_quality_args(encoder, crf, preset) + ["-profile:v", "high", "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-crf", str(crf), "-preset", preset]
        cmd += ["-c:a", "aac"]
    else:
        cmd += ["-c:v", "copy", "-c:a", "aac" if reencode_audio else "copy"]
    cmd += [
        "-bsf:v", _INTRO_BSF[vcodec],
        "-avoid_negative_ts", "make_zero",
        "-f", "mpegts", str(dst_ts),
    ]
    return cmd


def _concat_ts_to_mp4(
    ffmpeg: str, segments: Sequence[str | Path], dst: str | Path, *,
    vcodec: str, has_audio: bool,
) -> tuple[int, str]:
    """Concat MPEG-TS ``segments`` into one mp4 by stream copy (concat demuxer)."""
    fd, list_path = tempfile.mkstemp(prefix="clipper_introlist_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(build_concat_listfile(segments))
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-map", "0:v:0", "-map", "0:a?", "-dn", "-c", "copy",
        ]
        if vcodec == "hevc":
            cmd += ["-tag:v", "hvc1"]  # hvc1 (not hev1) for QuickTime/NLE compatibility
        cmd += ["-movflags", "+faststart", str(dst)]
        return _run_quiet(cmd)
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


def _ensure_card_ts(
    ffmpeg: str, card: CardSpec, info: StreamInfo, vcodec: str,
    card_cache: dict[str, str] | None,
) -> tuple[str | None, bool, str | None]:
    """Return (ts_path, is_temp, error). Reuses a cached card if present, else
    encodes one. ``is_temp`` is True only when there is no cache (so the caller
    deletes it); cached cards are the cache owner's to clean up."""
    key = card_target_key(vcodec, info, card)
    if card_cache is not None:
        cached = card_cache.get(key)
        if cached and Path(cached).exists():
            return cached, False, None
    fd, ts = tempfile.mkstemp(prefix="clipper_card_", suffix=".ts")
    os.close(fd)
    rc, err = build_card_ts(ffmpeg, card, info, vcodec, ts)
    if rc != 0:
        try:
            os.unlink(ts)
        except OSError:
            pass
        return None, False, _last_line(err) or f"ffmpeg exit {rc}"
    if card_cache is not None:
        card_cache[key] = ts
        return ts, False, None
    return ts, True, None


def run_cut_with_cards(
    ffmpeg: str,
    src: str | Path,
    start: float,
    end: float,
    out_path: str | Path,
    *,
    intro: CardSpec | None = None,
    outro: CardSpec | None = None,
    exact: bool = False,
    encoder: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    web_safe: bool = False,
    src_info: StreamInfo | None = None,
    card_cache: dict[str, str] | None = None,
    dry_run: bool = False,
) -> CutResult:
    """Cut one clip with an ``intro`` card prepended and/or an ``outro`` card
    appended (see the module section above).

    ``web_safe`` makes the whole clip browser-playable H.264: the body is
    re-encoded to H.264 (with ``encoder``) unless the source already is, and the
    cards are generated to match. ``src_info`` lets the caller probe the shared
    source once for a whole batch; ``card_cache`` (a caller-owned dict) reuses
    encoded cards across rows -- the caller is responsible for deleting the
    cached .ts files when done. With neither card this defers to :func:`run_cut`.
    Never raises on ffmpeg failure; returns a :class:`CutResult`.
    """
    out_path = Path(out_path)
    if intro is None and outro is None:
        return run_cut(
            ffmpeg, src, start, end, out_path,
            exact=exact, encoder=encoder, crf=crf, preset=preset,
            web_safe=web_safe, src_info=src_info, dry_run=dry_run,
        )

    info = src_info or probe_streams(ffmpeg, src)
    # Decide the body's codec and whether it must be re-encoded:
    #   web-safe -> always H.264 (re-encode unless the source already qualifies);
    #   exact    -> re-encode to the encoder's codec for a frame-accurate start;
    #   else     -> stream-copy the source's own codec.
    if web_safe:
        body_codec = "h264"
        body_encoder = encoder if encoder_codec_family(encoder) == "h264" else "libx264"
        body_reencode = exact or not _h264_safe_source(info)
    else:
        body_codec = encoder_codec_family(encoder) if exact else info.vcodec
        body_encoder = encoder
        body_reencode = exact
    if body_codec not in _INTRO_BSF:
        what = f"encoder {encoder!r}" if exact else f"source video ({info.vcodec or 'unknown'})"
        return CutResult(
            False, out_path, [], 1,
            f"intro/outro card supports H.264/H.265 only; {what} is not",
        )

    if dry_run:
        return CutResult(True, out_path, [str(ffmpeg), "(cards)", str(src), str(out_path)])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temps: list[str] = []  # files this call must delete (body + any un-cached card)
    try:
        # 1. Encode (or reuse) the intro / outro cards matched to this body.
        intro_ts = outro_ts = None
        for label, card, setter in (("intro", intro, "intro"), ("outro", outro, "outro")):
            if card is None:
                continue
            ts, is_temp, err = _ensure_card_ts(ffmpeg, card, info, body_codec, card_cache)
            if err is not None:
                return CutResult(False, out_path, [], 1, f"{label} encode failed: {err}")
            if is_temp:
                temps.append(ts)
            if setter == "intro":
                intro_ts = ts
            else:
                outro_ts = ts

        # 2. Cut the body straight to TS (temp lives on the output volume).
        bfd, body_ts = tempfile.mkstemp(prefix="clipper_body_", suffix=".ts", dir=str(out_path.parent))
        os.close(bfd)
        temps.append(body_ts)
        reencode_audio = info.has_audio and info.acodec != "aac"
        bcmd = _build_body_ts_cmd(
            ffmpeg, src, start, end, body_ts,
            vcodec=body_codec, reencode=body_reencode, encoder=body_encoder, crf=crf,
            preset=preset, reencode_audio=reencode_audio,
        )
        rc, err = _run_quiet(bcmd)
        if rc != 0:
            return CutResult(False, out_path, bcmd, rc, f"body cut failed: {_last_line(err)}")

        # 3. Concat intro + body + outro (in order) into the final clip.
        segments = [s for s in (intro_ts, body_ts, outro_ts) if s]
        rc, err = _concat_ts_to_mp4(
            ffmpeg, segments, out_path,
            vcodec=body_codec, has_audio=info.has_audio,
        )
        ok = rc == 0 and out_path.exists() and out_path.stat().st_size > 0
        return CutResult(
            ok, out_path, bcmd, rc,
            "" if ok else f"card concat failed: {_last_line(err)}",
        )
    finally:
        for tmp in temps:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# Back-compat: the original intro-only entry point.
def run_cut_with_intro(ffmpeg, src, start, end, out_path, intro, *, intro_cache=None, **kwargs):
    return run_cut_with_cards(
        ffmpeg, src, start, end, out_path,
        intro=intro, card_cache=intro_cache, **kwargs,
    )


# --------------------------------------------------------------- joining inputs

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_BLACK_START_RE = re.compile(r"black_start:([0-9.]+)")
_BLACK_END_RE = re.compile(r"black_end:([0-9.]+)")


def detect_trailing_black(
    ffmpeg: str,
    path: str | Path,
    *,
    duration: float | None = None,
    window: float = 6.0,
    min_black: float = 0.04,
    pix_th: float = 0.10,
) -> float | None:
    """Timestamp where black at the *end* of ``path`` begins, or None if it
    doesn't end in black.

    Some cameras (notably DJI) write a few black frames at the close of every
    auto-split file; after a lossless join those become a brief black flash at
    each splice. The returned value is meant to be used directly as an ffmpeg
    concat ``outpoint`` (stop reading the file there), dropping the black tail
    without re-encoding and without losing real footage.

    Only the last ``window`` seconds are decoded (fast even for 4K), and
    ``blackdetect`` is run with ``-copyts`` so its timestamps are absolute in the
    file's own timeline -- exactly what ``outpoint`` expects. A black run counts
    as *trailing* only if it reaches the end of the file (within ~0.5s), so a
    dark scene earlier in the recording is left untouched.
    """
    if duration is None:
        duration = probe_duration(ffmpeg, path)
    if not duration or duration <= 0:
        return None
    start = max(duration - window, 0.0)
    cmd = [
        ffmpeg, "-hide_banner", "-ss", f"{start:.3f}", "-copyts", "-i", str(path),
        "-an", "-vf", f"blackdetect=d={min_black}:pix_th={pix_th}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    text = proc.stderr or ""
    starts = [float(x) for x in _BLACK_START_RE.findall(text)]
    ends = [float(x) for x in _BLACK_END_RE.findall(text)]
    if not starts or not ends:
        return None
    # The last detected interval is the trailing-black candidate.
    black_start, black_end = starts[-1], ends[-1]
    if black_end < duration - 0.5:
        return None  # black is mid-recording, not at the very end -> leave it
    if black_start <= start or black_start >= duration:
        # Whole window black (cap the trim at the window) or zero-length: clamp.
        black_start = max(start, min(black_start, duration))
    return black_start


def probe_duration(ffmpeg: str, path: str | Path) -> float | None:
    """Best-effort media duration in seconds, read from the file header.

    Uses ffmpeg itself (it prints ``Duration:`` to stderr) so no separate
    ffprobe is required -- ``imageio-ffmpeg`` only ships ffmpeg. Returns None if
    the duration can't be parsed.
    """
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return None
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def _concat_quote(path: str | Path) -> str:
    """One concat-demuxer list line: ``file '...'`` with single quotes escaped.

    Paths are made absolute with forward slashes so they resolve regardless of
    where the (temporary) list file lives.
    """
    p = Path(path).resolve().as_posix().replace("'", "'\\''")
    return f"file '{p}'"


def build_concat_listfile(
    inputs: Sequence[str | Path],
    outpoints: Sequence[float | None] | None = None,
) -> str:
    """The text body of an ffmpeg concat-demuxer list for ``inputs`` (in order).

    ``outpoints[i]``, when not None, adds an ``outpoint`` directive after that
    file so the demuxer stops reading it at that timestamp -- used to drop a
    trailing black tail (see :func:`detect_trailing_black`)."""
    lines: list[str] = []
    for i, p in enumerate(inputs):
        lines.append(_concat_quote(p))
        if outpoints is not None and i < len(outpoints) and outpoints[i] is not None:
            lines.append(f"outpoint {outpoints[i]:.3f}")
    return "\n".join(lines) + "\n"


@dataclass
class ConcatResult:
    ok: bool
    output: Path
    command: list[str]
    returncode: int = 0
    stderr: str = ""
    cancelled: bool = False


def _parse_progress_seconds(value: str) -> float | None:
    try:
        h, m, s = value.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None  # e.g. "N/A" before the first frame is muxed


def concat_videos(
    ffmpeg: str,
    inputs: Sequence[str | Path],
    out_path: str | Path,
    *,
    total_duration: float | None = None,
    outpoints: Sequence[float | None] | None = None,
    progress: Callable[[float, float | None], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    dry_run: bool = False,
) -> ConcatResult:
    """Join ``inputs`` into one file by stream copy (no re-encode).

    Uses ffmpeg's concat *demuxer*, which is lossless and fast and correct when
    every input shares the same codec/resolution/framerate -- exactly the case
    for the ~hour-long chapters a GoPro auto-splits one long recording into.

    ``outpoints`` (one per input, any may be None) trims each file's tail at the
    given timestamp via the concat demuxer -- used to drop trailing black frames
    while still stream-copying (see :func:`detect_trailing_black`).

    ``progress(seconds_done, total_duration)`` is called as ffmpeg reports it
    (``total_duration`` may be None if unknown). ``cancel`` is polled between
    progress updates; when it returns True the join stops and the partial output
    is removed. Never raises on ffmpeg failure -- returns a :class:`ConcatResult`.
    """
    out_path = Path(out_path)
    inputs = [Path(p) for p in inputs]

    list_fd, list_path = tempfile.mkstemp(prefix="clipper_concat_", suffix=".txt")
    err_fd, err_path = tempfile.mkstemp(prefix="clipper_concat_", suffix=".log")
    os.close(err_fd)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-map", "0:v:0", "-map", "0:a?",   # video + audio; drop GoPro data tracks
        "-c", "copy",
        "-progress", "pipe:1", "-nostats",
        str(out_path),
    ]
    try:
        with os.fdopen(list_fd, "w", encoding="utf-8") as fh:
            fh.write(build_concat_listfile(inputs, outpoints))
        if dry_run:
            return ConcatResult(True, out_path, cmd)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cancelled = False
        # stderr -> a file (not a pipe) so it can never fill and deadlock while
        # we stream the progress pipe.
        with open(err_path, "w", encoding="utf-8") as errfh:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfh, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                if progress and line.startswith("out_time="):
                    secs = _parse_progress_seconds(line.split("=", 1)[1])
                    if secs is not None:
                        progress(secs, total_duration)
                if cancel and cancel():
                    cancelled = True
                    proc.terminate()
                    break
            proc.wait()

        stderr = ""
        try:
            stderr = Path(err_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass

        if cancelled:
            try:
                out_path.unlink()
            except OSError:
                pass
            return ConcatResult(False, out_path, cmd, proc.returncode, "cancelled", cancelled=True)

        ok = proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
        return ConcatResult(ok, out_path, cmd, proc.returncode, stderr)
    finally:
        for p in (list_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass
