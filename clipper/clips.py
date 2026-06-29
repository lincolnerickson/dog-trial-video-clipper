"""The clip list: data model, CSV round-trip, and validation.

CSV schema (header required)::

    start,end,label[,exact]
    00:14:32.000,00:17:05.000,Smith_Rex
    00:19:48.000,00:22:30.000,Jones / Bella's run,
    00:25:10.000,00:25:40.000,Photo finish,1

``start`` / ``end`` are ``HH:MM:SS.mmm`` timecodes (see :mod:`clipper.timecode`).
``label`` is the raw, human-typed label -- it is sanitized into a filename
only at output time, never stored mangled.  The optional ``exact`` column is
truthy ("1", "true", "yes", "x") when that one row should be re-encoded for a
frame-accurate cut instead of a stream copy.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from . import timecode

CSV_FIELDS = ("start", "end", "label", "exact")
_TRUTHY = {"1", "true", "yes", "y", "x", "exact", "on"}


@dataclass
class Clip:
    """One marked segment: an in-point, an out-point and a raw label."""

    start: float            # seconds
    end: float              # seconds
    label: str              # raw, human-typed
    exact: bool = False     # re-encode this row for a frame-accurate cut
    # GUI-only: the roster participant this clip came from, so deleting the clip
    # can return that participant to the roster. Never written to / read from CSV.
    source_participant: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def start_tc(self, decimals: int = 3) -> str:
        return timecode.format_timecode(self.start, decimals)

    def end_tc(self, decimals: int = 3) -> str:
        return timecode.format_timecode(self.end, decimals)


@dataclass
class Issue:
    """A validation finding tied to a row (0-based ``index``)."""

    index: int
    severity: str           # "error" or "warning"
    message: str

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


def _parse_exact(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def read_csv(path: str | Path) -> list[Clip]:
    """Read a clip list from CSV. Raises ValueError with a row number on bad data."""
    clips: list[Clip] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV is empty (no header row)")
        normalized = {(name or "").strip().lower() for name in reader.fieldnames}
        for required in ("start", "end", "label"):
            if required not in normalized:
                raise ValueError(
                    f"CSV header is missing required column {required!r}; "
                    f"found {reader.fieldnames}"
                )
        # rownum: +2 == header is line 1, first data row is line 2.
        for rownum, row in enumerate(reader, start=2):
            lower = {(k or "").strip().lower(): v for k, v in row.items()}
            label = (lower.get("label") or "").strip()
            try:
                start = timecode.parse_timecode(lower.get("start", ""))
                end = timecode.parse_timecode(lower.get("end", ""))
            except timecode.TimecodeError as exc:
                raise ValueError(f"row {rownum}: {exc}") from exc
            clips.append(
                Clip(
                    start=start,
                    end=end,
                    label=label,
                    exact=_parse_exact(lower.get("exact")),
                )
            )
    return clips


def write_csv(path: str | Path, clips: list[Clip], *, decimals: int = 3) -> None:
    """Write the clip list to CSV in marking order."""
    any_exact = any(c.exact for c in clips)
    fields = list(CSV_FIELDS) if any_exact else list(CSV_FIELDS[:3])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for clip in clips:
            base = [
                clip.start_tc(decimals),
                clip.end_tc(decimals),
                clip.label,
            ]
            if any_exact:
                base.append("1" if clip.exact else "")
            writer.writerow(base)


def validate(clips: list[Clip], *, overlap_warn_fraction: float = 0.25) -> list[Issue]:
    """Check the clip list and return a list of issues (errors + warnings).

    Errors (block export):
      * end is not after start
      * blank label

    Warnings (surface for review, don't block):
      * negative / zero-length already covered as error, but ranges that
        overlap a neighbour are flagged; an overlap covering more than
        ``overlap_warn_fraction`` of the shorter clip is called out as
        "heavily" overlapping ("wildly overlapping ranges" in the spec).
    """
    issues: list[Issue] = []

    for i, clip in enumerate(clips):
        if not clip.label.strip():
            issues.append(Issue(i, "error", "label is blank"))
        if clip.end <= clip.start:
            issues.append(
                Issue(
                    i,
                    "error",
                    f"end ({clip.end_tc()}) is not after start ({clip.start_tc()})",
                )
            )

    # Overlap detection across the (start-sorted) timeline.
    order = sorted(range(len(clips)), key=lambda i: clips[i].start)
    for a, b in zip(order, order[1:]):
        first, second = clips[a], clips[b]
        if first.end <= first.start or second.end <= second.start:
            continue  # already an error; skip overlap noise
        overlap = first.end - second.start
        if overlap > 0:
            shorter = min(first.duration, second.duration)
            frac = overlap / shorter if shorter > 0 else 1.0
            degree = "heavily " if frac >= overlap_warn_fraction else ""
            issues.append(
                Issue(
                    b,
                    "warning",
                    f"{degree}overlaps row with label "
                    f"{first.label.strip() or '(blank)'!r} "
                    f"by {timecode.format_timecode(overlap)}",
                )
            )

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.is_error for issue in issues)
