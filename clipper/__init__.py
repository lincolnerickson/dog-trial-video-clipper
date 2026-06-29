"""Shared core for the Dog Trial Video Clipper.

Both the marking tool (``marker.py``) and the batch cutter (``cutter.py``)
import from this package so that timecode handling, filename sanitizing,
sequence numbering and the clip-list/CSV format stay identical on both sides.
"""

from . import clips, ffmpeg_tools, naming, timecode

__all__ = ["clips", "ffmpeg_tools", "naming", "timecode"]
