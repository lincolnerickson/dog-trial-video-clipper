"""Regression tests for the 1.0.17 shutdown/cancel fixes.

Covers: the ffmpeg process registry (kill an in-flight child, honour a cancel
that lands between spawns, SIGKILL escalation plumbing), partial-output cleanup
on failed/killed cuts and joins, UTF-8 decoding of ffmpeg output regardless of
locale, the _closing guard that keeps _pump() from running during quit,
editing_row survival across delete/reorder, and Cancel persisting the cleared
queue immediately.

Run:  QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python tests\\test_shutdown_fixes.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clipper.clips import Clip  # noqa: E402
from clipper.ffmpeg_tools import (  # noqa: E402
    _run_tracked,
    clear_thread_cancellation,
    concat_videos,
    find_ffmpeg,
    probe_streams,
    run_cut,
    terminate_thread_ffmpeg,
)


def main():
    root = Path(__file__).resolve().parent.parent
    sample = root / "sample" / "trial_4k.mp4"
    ff = find_ffmpeg()
    tmp = Path(tempfile.mkdtemp(prefix="clipper_shutdown_test_"))

    try:
        # 1) A cancel that lands when no child is registered must still kill the
        #    NEXT child the thread spawns (the stop()-between-spawns race).
        tid = threading.get_ident()
        terminate_thread_ffmpeg(tid)          # nothing running yet -> marks tid
        t0 = time.time()
        proc = _run_tracked(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240", "-t", "30",
             "-c:v", "libx264", "-f", "null", "-"],
        )
        elapsed = time.time() - t0
        assert proc.returncode != 0, "cancelled thread's new child ran to completion"
        assert elapsed < 10, f"child not killed at spawn (took {elapsed:.1f}s)"
        clear_thread_cancellation(tid)
        proc = _run_tracked(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240",
             "-frames:v", "1", "-f", "null", "-"],
        )
        assert proc.returncode == 0, "clear_thread_cancellation didn't lift the mark"
        print("OK: cancel between spawns kills the next child; clearing restores normal runs")

        # 2) A cut killed mid-encode reports failure and leaves NO partial mp4.
        out = tmp / "killed_cut.mp4"
        box: dict = {}

        def work():
            box["tid"] = threading.get_ident()
            box["res"] = run_cut(ff, sample, 0.0, 13.0, out, exact=True,
                                 encoder="libx264", preset="veryslow")

        th = threading.Thread(target=work)
        th.start()
        time.sleep(2.0)                       # let ffmpeg spawn and start encoding
        terminate_thread_ffmpeg(box["tid"])
        th.join(timeout=15)
        assert not th.is_alive(), "worker still blocked after kill"
        clear_thread_cancellation(box["tid"])
        assert not box["res"].ok, "killed cut reported ok"
        assert not out.exists(), "killed cut left a truncated mp4 behind"
        print("OK: killed cut unblocks fast, reports failure, partial file removed")

        # 3) ffmpeg stderr containing non-cp1252 UTF-8 (the filename) must not
        #    raise UnicodeDecodeError on any locale.
        weird = tmp / "trial_ō_Œ.mp4"
        shutil.copy(sample, weird)
        info = probe_streams(ff, weird)       # banner echoes the filename
        assert info.width > 0, "probe failed on a unicode filename"
        print("OK: unicode filenames survive stderr decoding")

        # 4) A failed join leaves no partial output.
        bad_out = tmp / "failed_join.mp4"
        res = concat_videos(ff, [tmp / "does_not_exist.mp4"], bad_out)
        assert not res.ok and not bad_out.exists(), "failed join left a partial file"
        print("OK: failed join cleans up its output")

        # ------------------------------------------------ GUI-level guards
        from PySide6.QtWidgets import QApplication  # noqa: E402
        import marker  # noqa: E402

        app = QApplication([])
        win = marker.MarkerWindow()

        # 5) _pump() must be inert once quit is confirmed (_closing set): no new
        #    worker, queue untouched, resume file not rewritten.
        win._closing = True
        win._export_queue = [{"label": "fake"}]
        saved = []
        real_save = marker.session.save_jobs
        marker.session.save_jobs = lambda jobs: saved.append(jobs)
        try:
            win._pump()
        finally:
            marker.session.save_jobs = real_save
        assert win._export_worker is None and win._join_worker is None
        assert win._export_queue == [{"label": "fake"}], "closing _pump consumed the queue"
        assert saved == [], "closing _pump re-persisted (would delete the resume file)"
        win._closing = False
        win._export_queue = []
        print("OK: _pump is inert during quit (no new worker, resume file untouched)")

        # 6) editing_row follows its clip across delete and reorder.
        win.clips = [Clip(1, 2, "A"), Clip(3, 4, "B"), Clip(5, 6, "C")]
        win._refresh_table()
        win.editing_row = 2                   # editing "C"
        win._select_row(0)
        win.delete_selected()                 # delete "A" above it
        assert win.editing_row == 1, f"editing_row not shifted: {win.editing_row}"
        assert win.clips[win.editing_row].label == "C"

        win.clips = [Clip(1, 2, "A"), Clip(3, 4, "B"), Clip(5, 6, "C")]
        win._refresh_table()
        win.editing_row = 1                   # editing "B"
        win._select_row(1)
        win.move_up()                         # B swaps with A
        assert win.editing_row == 0 and win.clips[0].label == "B", win.editing_row
        win._select_row(1)                    # "A" now at row 1; editing_row=0 ("B")
        win.move_up()                         # A swaps up past B
        assert win.editing_row == 1 and win.clips[1].label == "B", win.editing_row

        win.clips = [Clip(1, 2, "A"), Clip(3, 4, "B")]
        win._refresh_table()
        win.editing_row = 0
        win._select_row(0)
        win.move_down()
        assert win.editing_row == 1 and win.clips[1].label == "A", win.editing_row

        win.clips = [Clip(1, 2, "A"), Clip(3, 4, "B")]
        win._refresh_table()
        win.editing_row = 1
        win._select_row(1)
        win.delete_selected()                 # deleting the edited clip exits edit mode
        assert win.editing_row is None
        print("OK: editing_row tracks its clip through delete / move up / move down")

        # 7) Cancel persists the cleared queue immediately.
        win._export_queue = [{"label": "queued"}]
        win._join_queue = []
        saved = []
        marker.session.save_jobs = lambda jobs: saved.append(jobs)
        try:
            win._cancel_background()
        finally:
            marker.session.save_jobs = real_save
        assert saved == [[]], f"cancel didn't persist an empty queue: {saved}"
        print("OK: Cancel writes the cleared queue to disk immediately")

        print("\nSHUTDOWN FIXES OK: registry race, partial cleanup, unicode, "
              "closing guard, editing_row, cancel persistence all verified")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
