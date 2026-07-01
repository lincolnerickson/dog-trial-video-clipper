"""Scrub coalescer: a burst of seek() calls during a slider drag must collapse to
a SINGLE in-flight seek that always chases the latest target, so the decoder never
backs up (the fix for choppy scrubbing vs. an NLE).

Run: QT_QPA_PLATFORM=offscreen python tests/test_scrub.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402
from markerlib.player import QtVideoPlayer  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    p = QtVideoPlayer()
    calls: list[int] = []
    p._player.setPosition = lambda ms: calls.append(ms)  # spy on the real seeks

    # A fast drag: many seek() calls before any frame comes back.
    p.seek(1.0)      # nothing in flight -> issued immediately
    p.seek(2.0)      # in flight -> just recorded as pending
    p.seek(3.5)      # newest pending
    assert calls == [1000], calls
    assert p._seek_inflight and p._seek_pending_ms == 3500

    # Frame lands: the intermediate 2000 is dropped, we jump to the latest 3500.
    p._settle_seek_after_frame()
    assert calls == [1000, 3500], calls
    assert p._seek_inflight and p._seek_pending_ms is None

    # Next frame, nothing pending -> go idle.
    p._settle_seek_after_frame()
    assert not p._seek_inflight

    # A lone later seek issues right away (single seeks aren't delayed).
    p.seek(5.0)
    assert calls == [1000, 3500, 5000], calls

    # Watchdog: if a seek yields no frame, it still chases the newest target.
    p._settle_seek_after_frame()          # clear the 5000 in-flight
    p.seek(6.0)                            # issued
    p.seek(7.0)                            # pending
    assert calls[-1] == 6000
    p._on_seek_watchdog()                  # simulate the timeout firing
    assert calls[-1] == 7000, calls

    print("SCRUB OK: burst of seeks coalesced to the latest target (no backlog)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
