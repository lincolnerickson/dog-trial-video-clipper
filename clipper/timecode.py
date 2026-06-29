"""Timecode parsing and formatting.

Canonical on-disk / CSV format is ``HH:MM:SS.mmm`` (e.g. ``00:14:32.000``).
This string is fed directly to ffmpeg as a ``-ss`` / ``-t`` value, so the
marker and the cutter must agree on it -- that is the whole point of keeping
this in one shared module.

Parsing is deliberately lenient so a human editing the CSV by hand can write
``14:32`` or ``872.5`` and have it understood; formatting is always strict.
"""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


class TimecodeError(ValueError):
    """Raised when a timecode string cannot be parsed."""


def parse_timecode(value: str | float | int) -> float:
    """Parse a timecode into a number of seconds (float).

    The number of colon-separated fields decides the meaning, so there is no
    ambiguity:
      * one field  -> bare seconds: ``872`` or ``872.5``
      * two fields -> ``MM:SS(.s)``: ``14:32`` / ``14:32.5``
      * three      -> ``HH:MM:SS(.s)``: ``00:14:32.0``

    Only the last (seconds) field may carry a fraction. Raises
    :class:`TimecodeError` on anything else.
    """
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise TimecodeError(f"negative timecode: {value!r}")
        return seconds

    text = str(value).strip()
    if not text:
        raise TimecodeError("empty timecode")

    parts = text.split(":")
    if len(parts) > 3:
        raise TimecodeError(f"too many ':' fields in {value!r}")

    # Every field must be a non-negative number; only the last may be fractional.
    for i, part in enumerate(parts):
        if not _NUMBER_RE.match(part.strip()):
            raise TimecodeError(f"not a valid timecode field {part!r} in {value!r}")
        if i < len(parts) - 1 and "." in part:
            raise TimecodeError(f"only the seconds field may be fractional in {value!r}")

    nums = [float(p) for p in parts]
    if len(nums) == 1:
        hours, minutes, seconds = 0.0, 0.0, nums[0]
    elif len(nums) == 2:
        hours, (minutes, seconds) = 0.0, nums
    else:
        hours, minutes, seconds = nums

    # When fields are sub-fields of a larger value they must be 0-59.
    if len(nums) >= 2 and seconds >= 60:
        raise TimecodeError(f"seconds field >= 60 in {value!r}")
    if len(nums) == 3 and minutes >= 60:
        raise TimecodeError(f"minutes field >= 60 in {value!r}")

    return hours * 3600 + minutes * 60 + seconds


def format_timecode(seconds: float, decimals: int = 3) -> str:
    """Format a number of seconds as ``HH:MM:SS.mmm``.

    ``decimals`` controls the fractional precision (default 3 = milliseconds).
    Always zero-padded so the values sort lexically in marking order too.
    """
    if seconds < 0:
        seconds = 0.0
    total = round(float(seconds), decimals)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total - hours * 3600 - minutes * 60
    width = 2 + (1 + decimals if decimals > 0 else 0)
    return f"{hours:02d}:{minutes:02d}:{secs:0{width}.{decimals}f}"
