#!/usr/bin/env python3
"""Batch cutter for the Dog Trial Video Clipper.

Reads a clip-list CSV (produced by the marking tool, or hand-written) plus a
source video, and writes one named clip per row via an ffmpeg stream copy --
no re-encode, so a whole trial's worth of 4K clips finishes in seconds.

    python cutter.py --video trial.mp4 --csv clips.csv --out clips

Filename sanitizing and sequence numbering come from clipper.naming, the exact
same module the marker uses, so labels map to identical filenames on both sides.
The marking tool's "Export clips" button calls run_batch() here directly, so
the CLI and the GUI share one cutting path.

See the README for the stream-copy keyframe caveat and the per-row `exact` flag.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from clipper import clips as clips_mod
from clipper import naming
from clipper.ffmpeg_tools import (
    CardSpec,
    FFmpegNotFound,
    encoder_codec_family,
    find_ffmpeg,
    find_h264_encoder,
    probe_streams,
    run_cut,
    run_cut_with_cards,
)


@dataclass
class RowOutcome:
    rownum: int            # 1-based
    label: str             # display label (raw, blank shown as "(blank)")
    status: str            # "written" | "skipped" | "failed"
    filename: str = ""
    dest: Path | None = None
    exact: bool = False
    reason: str = ""       # why skipped/failed


@dataclass
class BatchResult:
    outcomes: list[RowOutcome] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def written(self) -> list[RowOutcome]:
        return [o for o in self.outcomes if o.status == "written"]

    @property
    def problems(self) -> list[RowOutcome]:
        return [o for o in self.outcomes if o.status != "written"]

    @property
    def total(self) -> int:
        return len(self.outcomes)


def group_folder(clip: clips_mod.Clip) -> str:
    """The participant folder a clip belongs in (e.g. ``Sara & Tracer``).

    Prefers the participant the marker captured when the clip was marked; if that
    isn't set (the CLI path, reading only a CSV) it falls back to the part of the
    label before the ``" - "`` search separator -- which the marker guarantees is
    exactly the ``First & Dog`` participant.
    """
    participant = (clip.source_participant or "").strip()
    if not participant:
        participant = clip.label.split(" - ", 1)[0].strip()
    return participant or clip.label


def folder_filename_label(clip: clips_mod.Clip) -> str:
    """The label a clip's file gets *inside its participant folder*: the
    search/event part only, since the participant is the folder itself
    (``Sara & Tracer - Interior Search 1`` -> ``Interior Search 1``). Falls back
    to the whole label if there's no search part to split off.
    """
    label = (clip.label or "").strip()
    if " - " in label:
        return label.split(" - ", 1)[1].strip() or label
    return label


def out_path_for(out_dir: Path, filename: str, clip: clips_mod.Clip, folder_per_participant: bool) -> Path:
    if folder_per_participant:
        return out_dir / naming.sanitize_label(group_folder(clip)) / filename
    return out_dir / filename


def unique_dest(dest: Path, used: set[str]) -> Path:
    """Return ``dest``, or a ``name (2).ext`` / ``name (3).ext`` variant if that
    name is already taken -- either earlier in this batch (``used``) or by a file
    already on disk from a previous export.

    Without a sequence-number prefix two clips could sanitize to the same name;
    this guarantees a new clip never silently overwrites an existing file. ``used``
    holds the lower-cased paths already claimed this batch; the caller adds the
    returned path to it.
    """
    def taken(p: Path) -> bool:
        return str(p).lower() in used or p.exists()

    if not taken(dest):
        return dest
    n = 2
    while True:
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not taken(candidate):
            return candidate
        n += 1


# Progress callback signature: (rownum, total, RowOutcome) -> None
ProgressFn = Callable[[int, int, RowOutcome], None]


def run_batch(
    ffmpeg: str,
    video: str | Path,
    rows: list[clips_mod.Clip],
    out_dir: str | Path,
    *,
    folder_per_participant: bool = False,
    ext: str = "mp4",
    encoder: str | None = None,
    crf: int = 23,
    preset: str = "medium",
    intro: CardSpec | None = None,
    outro: CardSpec | None = None,
    web_safe: bool = False,
    video_mode: str = "copy",
    dry_run: bool = False,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> BatchResult:
    """Cut every row in ``rows`` to a named file. Shared by the CLI and the GUI.

    Validation errors cause that row to be skipped (recorded with a reason);
    warnings do not block. Per-clip ffmpeg failures are recorded too. Never
    raises on a single bad row -- the whole batch always runs to completion.

    ``cancel`` is an optional callable polled before each row; when it returns
    True the batch stops early (the row in progress is left to finish). Used by
    the GUI's Cancel button.
    """
    video = Path(video)
    out_dir = Path(out_dir)
    total = len(rows)
    result = BatchResult()

    issues = clips_mod.validate(rows)
    errors_by_row: dict[int, list[str]] = {}
    for issue in issues:
        if issue.is_error:
            errors_by_row.setdefault(issue.index, []).append(issue.message)

    # Resolve the output video mode + its re-encode encoder. "hevc" picks the
    # fastest working HEVC encoder (GPU if available), "h264" an H.264 one; an
    # explicit matching --encoder is honoured. "copy" only re-encodes `exact`
    # rows (with libx264) for a frame-accurate start.
    mode = video_mode if video_mode in ("hevc", "h264") else ("h264" if web_safe else "copy")
    exact_encoder = encoder or "libx264"
    reencode_encoder = None
    if mode == "h264":
        reencode_encoder = (
            encoder if (encoder and encoder_codec_family(encoder) == "h264")
            else find_h264_encoder(ffmpeg)
        )
    elif mode == "hevc":
        # Software libx265 for a *consistent* CRF across machines. Hardware HEVC
        # encoders read the quality number very differently (e.g. NVENC CQ 22 ~ 3x
        # the bitrate of libx265 CRF 22), so an explicit hardware --encoder is
        # honoured but never auto-selected here.
        reencode_encoder = (
            encoder if (encoder and encoder_codec_family(encoder) == "hevc")
            else "libx265"
        )

    # The cards / re-encode all match the (single) source, so probe it once and
    # reuse each encoded card across every row (cached .ts files cleaned up after).
    has_cards = intro is not None or outro is not None
    src_info = probe_streams(ffmpeg, video) if (has_cards or mode != "copy") else None
    card_cache: dict[str, str] = {}

    used_dests: set[str] = set()
    start_time = time.perf_counter()
    for idx, clip in enumerate(rows):
        if cancel and cancel():
            break
        rownum = idx + 1
        label_display = clip.label.strip() or "(blank)"
        # In a participant folder the file is just the search part (the folder is
        # the participant); a flat export keeps the participant in the filename.
        file_label = folder_filename_label(clip) if folder_per_participant else clip.label
        filename = naming.build_filename(file_label, ext=ext)

        if idx in errors_by_row:
            outcome = RowOutcome(
                rownum, label_display, "skipped",
                filename=filename, exact=clip.exact,
                reason="; ".join(errors_by_row[idx]),
            )
            result.outcomes.append(outcome)
            if progress:
                progress(rownum, total, outcome)
            continue

        dest = unique_dest(out_path_for(out_dir, filename, clip, folder_per_participant), used_dests)
        used_dests.add(str(dest).lower())
        filename = dest.name
        cut_encoder = reencode_encoder if mode != "copy" else exact_encoder
        if has_cards:
            cut = run_cut_with_cards(
                ffmpeg, video, clip.start, clip.end, dest,
                intro=intro, outro=outro,
                exact=clip.exact, encoder=cut_encoder, crf=crf, preset=preset,
                video_mode=mode, src_info=src_info, card_cache=card_cache,
                dry_run=dry_run,
            )
        else:
            cut = run_cut(
                ffmpeg, video, clip.start, clip.end, dest,
                exact=clip.exact, encoder=cut_encoder, crf=crf, preset=preset,
                video_mode=mode, src_info=src_info, dry_run=dry_run,
            )
        if cut.ok:
            outcome = RowOutcome(
                rownum, label_display, "written",
                filename=filename, dest=dest, exact=clip.exact,
            )
        else:
            last = cut.stderr.splitlines()[-1] if cut.stderr else "unknown error"
            outcome = RowOutcome(
                rownum, label_display, "failed",
                filename=filename, dest=dest, exact=clip.exact,
                reason=f"ffmpeg exit {cut.returncode}: {last}",
            )
        result.outcomes.append(outcome)
        if progress:
            progress(rownum, total, outcome)

    for ts in card_cache.values():
        try:
            os.unlink(ts)
        except OSError:
            pass

    result.elapsed = time.perf_counter() - start_time
    return result


# --------------------------------------------------------------------------- CLI


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cutter.py",
        description="Stream-copy a source video into one named clip per CSV row.",
    )
    p.add_argument("--video", "-i", required=True, help="source video file")
    p.add_argument("--csv", "-c", default="clips.csv", help="clip list CSV (default: clips.csv)")
    p.add_argument("--out", "-o", default="clips", help="output folder (default: ./clips)")
    p.add_argument(
        "--folder-per-participant",
        "--folder-per-label",   # back-compat alias
        dest="folder_per_participant",
        action="store_true",
        help="group clips into a subfolder per participant ('First & Dog', "
        "e.g. 'Sara & Tracer/'), the file named for just the search part "
        "(default: one flat folder)",
    )
    p.add_argument("--ext", default="mp4", help="output container/extension (default: mp4)")
    p.add_argument("--ffmpeg", default=None, help="path to ffmpeg (default: auto-detect)")
    p.add_argument(
        "--encoder",
        default=None,
        help="video encoder for re-encoded rows -- exact=1 rows and (as an "
        "H.264 encoder) --web-safe (default: libx264; e.g. h264_nvenc for "
        "NVIDIA GPU). With --web-safe, the fastest working H.264 encoder is "
        "auto-detected when this is unset.",
    )
    p.add_argument("--crf", type=int, default=23, help="quality for re-encoded rows, lower=bigger/better (default: 23)")
    p.add_argument("--preset", default="medium", help="encoder preset for re-encoded rows (default: medium)")
    p.add_argument(
        "--video-mode",
        choices=("copy", "hevc", "h264"),
        default="copy",
        help="output video: 'copy' = stream copy, original codec (default); "
        "'hevc' = re-encode smaller HEVC at --crf (keeps full detail); "
        "'h264' = browser-playable H.264. Uses the fastest working GPU encoder.",
    )
    p.add_argument(
        "--web-safe",
        action="store_true",
        help="alias for --video-mode h264 (browser-playable H.264 mp4).",
    )
    p.add_argument(
        "--intro-image",
        default=None,
        help="image file (PNG/JPG/...) to prepend as an intro card to every clip "
        "(the run footage is still stream-copied; only the intro is encoded)",
    )
    p.add_argument(
        "--intro-seconds",
        type=float,
        default=3.0,
        help="how long the intro card is shown, in seconds (default: 3)",
    )
    p.add_argument(
        "--outro-image",
        default=None,
        help="image file (PNG/JPG/...) to append as an outro card to every clip "
        "(e.g. marking where the hides were; the run footage stays a stream copy)",
    )
    p.add_argument(
        "--outro-seconds",
        type=float,
        default=3.0,
        help="how long the outro card is shown, in seconds (default: 3)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be cut (and the ffmpeg commands) without writing files",
    )
    return p.parse_args(argv)


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def main(argv=None) -> int:
    args = parse_args(argv)

    video = Path(args.video)
    if not video.exists():
        print(f"ERROR: source video not found: {video}", file=sys.stderr)
        return 2

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    try:
        ffmpeg = args.ffmpeg or find_ffmpeg()
    except FFmpegNotFound as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        rows = clips_mod.read_csv(csv_path)
    except (ValueError, OSError) as exc:
        print(f"ERROR reading {csv_path}: {exc}", file=sys.stderr)
        return 2

    if not rows:
        print(f"No clips in {csv_path}; nothing to do.")
        return 0

    intro = None
    if args.intro_image:
        intro_path = Path(args.intro_image)
        if not intro_path.exists():
            print(f"ERROR: intro image not found: {intro_path}", file=sys.stderr)
            return 2
        intro = CardSpec(image=intro_path, seconds=args.intro_seconds)

    outro = None
    if args.outro_image:
        outro_path = Path(args.outro_image)
        if not outro_path.exists():
            print(f"ERROR: outro image not found: {outro_path}", file=sys.stderr)
            return 2
        outro = CardSpec(image=outro_path, seconds=args.outro_seconds)

    out_dir = Path(args.out)
    total = len(rows)

    # Surface warnings up front (they do not block).
    for issue in clips_mod.validate(rows):
        if not issue.is_error:
            lbl = rows[issue.index].label.strip() or "(blank)"
            print(f"  ! row {issue.index + 1} {lbl}: {issue.message}")

    print(f"ffmpeg : {ffmpeg}")
    print(f"source : {video}")
    print(f"csv    : {csv_path}  ({total} rows)")
    print(f"output : {out_dir}{' (folder per participant)' if args.folder_per_participant else ''}")
    if intro is not None:
        print(f"intro  : {Path(args.intro_image).name} for {args.intro_seconds:g}s on every clip")
    if outro is not None:
        print(f"outro  : {Path(args.outro_image).name} for {args.outro_seconds:g}s on every clip")
    video_mode = "h264" if args.web_safe else args.video_mode
    if video_mode == "hevc":
        enc = (args.encoder if (args.encoder and encoder_codec_family(args.encoder) == "hevc")
               else "libx265")
        print(f"video  : re-encode to HEVC at crf {args.crf} (via {enc})")
    elif video_mode == "h264":
        enc = (args.encoder if (args.encoder and encoder_codec_family(args.encoder) == "h264")
               else find_h264_encoder(ffmpeg))
        print(f"video  : web-safe H.264 (browser-playable; H.265 re-encoded via {enc})")
    if args.dry_run:
        print("mode   : DRY RUN (no files written)")
    print("-" * 64)

    def on_progress(rownum: int, total: int, o: RowOutcome) -> None:
        if o.status == "written":
            tag = "exact" if o.exact else "copy "
            if args.dry_run:
                print(f"  [{tag}] row {rownum} -> {o.dest}")
            else:
                print(f"  [{tag}] row {rownum} -> {o.filename}")
        elif o.status == "skipped":
            print(f"  SKIP row {rownum} {o.label}: {o.reason}")
        else:
            print(f"  FAIL row {rownum} {o.label}: {o.reason}")

    result = run_batch(
        ffmpeg, video, rows, out_dir,
        folder_per_participant=args.folder_per_participant, ext=args.ext,
        encoder=args.encoder, crf=args.crf, preset=args.preset,
        intro=intro, outro=outro,
        video_mode=video_mode, dry_run=args.dry_run, progress=on_progress,
    )

    print("-" * 64)
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {len(result.written)}/{result.total} clips in {result.elapsed:.1f}s")
    if result.problems:
        print(f"skipped/failed {len(result.problems)} row(s):")
        for o in result.problems:
            print(f"  - row {o.rownum} {o.label}: {o.reason}")
    return 0 if not result.problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
