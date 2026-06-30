"""Marking hotkeys: ↑/↓ (primary) and I/O (fallback) set In/Out.

↑/↓ are the primary In/Out keys (tested with the video focused). I/O still fire
regardless of which widget has focus -- except a text field, where they must type
normally -- so marking keeps working right after a roster click moves focus to
the list (whose type-ahead would otherwise swallow them). Runs offscreen.

Run:  QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python tests\\test_hotkeys.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import marker  # noqa: E402


def main():
    root = Path(__file__).resolve().parent.parent
    video = str(root / "sample" / "trial_4k.mp4")
    app = QApplication([])
    win = marker.MarkerWindow(video)
    win.show()
    win.activateWindow()  # so setFocus() actually updates QApplication.focusWidget()
    win.raise_()

    deadline = time.time() + 10
    while time.time() < deadline and win.player.duration() <= 0:
        app.processEvents()
        time.sleep(0.02)

    win._roster_all = ["Sara Tracer", "Lincoln Otter"]
    win._available = list(win._roster_all)
    win._refresh_roster()

    def press(widget, key, text=""):
        widget.setFocus()
        app.processEvents()
        assert QApplication.focusWidget() is widget, "focus did not move to the target"
        # Sent through the app (QCoreApplication.notify) so the window's
        # app-wide event filter sees it exactly like a real keypress.
        app.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text))
        app.processEvents()

    # 1) Roster list focused (as right after a click): I/O must still mark.
    win.in_point = win.out_point = None
    win.player.seek(5.0); app.processEvents()
    press(win.participants, Qt.Key.Key_I)
    assert win.in_point is not None, "I did not set In when the roster list had focus"
    win.player.seek(9.0); app.processEvents()
    press(win.participants, Qt.Key.Key_O)
    assert win.out_point is not None, "O did not set Out when the roster list had focus"
    print(f"OK: I/O work with roster focused (in={win.in_point:.1f}, out={win.out_point:.1f})")

    # 2) Clip table focused: I/O must still mark (not be eaten by type-ahead).
    win.in_point = win.out_point = None
    win.player.seek(3.0); app.processEvents()
    press(win.table, Qt.Key.Key_I)
    assert win.in_point is not None, "I did not set In when the clip table had focus"
    print("OK: I works with the clip table focused")

    # 3) Participant text field focused: I/O must type, NOT mark.
    win.in_point = win.out_point = None
    win.label_edit.clear()
    press(win.label_edit, Qt.Key.Key_I, "i")
    press(win.label_edit, Qt.Key.Key_O, "o")
    assert win.in_point is None and win.out_point is None, "I/O wrongly marked while typing a name"
    assert win.label_edit.text().lower() == "io", f"name field didn't receive the typed text: {win.label_edit.text()!r}"
    print(f"OK: I/O type in the name field instead of marking (text={win.label_edit.text()!r})")

    # 4) ↑/↓ arrows are the primary marking keys (window/video focused). They are
    # sent to the window itself, the way an unhandled arrow propagates up to the
    # main window's keyPressEvent when the video area has focus. Clear the name
    # field first so setting Out doesn't auto-commit a clip (which resets marks).
    win.label_edit.clear()
    win.in_point = win.out_point = None
    win.player.seek(2.0); app.processEvents()
    app.sendEvent(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    assert win.in_point is not None, "Up arrow did not set In"
    win.player.seek(7.0); app.processEvents()
    app.sendEvent(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    assert win.out_point is not None, "Down arrow did not set Out"
    print(f"OK: ↑/↓ mark In/Out (in={win.in_point:.1f}, out={win.out_point:.1f})")

    # 5) Editing a clip focuses the VIDEO (so ↑/↓ + scrubbing re-mark right away),
    # not the name field. Make a clip, edit it, and check focus + a re-mark.
    win._run_order = []
    win._roster_all = ["Sara Tracer"]; win._available = ["Sara Tracer"]; win._refresh_roster()
    win.clips = []
    win.in_point, win.out_point = 2.0, 5.0
    win.label_edit.setText("Sara Tracer")
    win.add_or_update_clip()
    win.table.selectRow(0)
    win.edit_selected()
    app.processEvents()
    assert QApplication.focusWidget() is win.video_area, \
        f"edit did not focus the video (focus={type(QApplication.focusWidget()).__name__})"
    win.player.seek(8.0); app.processEvents()
    app.sendEvent(win.video_area, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    assert win.out_point is not None and abs(win.out_point - 8.0) < 0.5, \
        f"↓ did not re-mark Out during edit (out={win.out_point})"
    print(f"OK: editing a clip focuses the video and ↓ re-marks Out (out={win.out_point:.1f})")

    # 6) Holding → accelerates: a fresh tap is the small step, each auto-repeat
    # while held moves a bit further.
    win.editing_row = None
    win.player.seek(0.0); app.processEvents()

    def arrow_right(autorep):
        app.sendEvent(win, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                                     Qt.KeyboardModifier.NoModifier, "", autorep))
        app.processEvents()

    arrow_right(False)                      # fresh tap (resets the ramp)
    prev = win.player.position()
    deltas = []
    for _ in range(5):
        arrow_right(True)                   # held -> auto-repeats accelerate
        cur = win.player.position()
        deltas.append(round(cur - prev, 3)); prev = cur
    assert win._arrow_held == 5, win._arrow_held
    assert all(b >= a for a, b in zip(deltas, deltas[1:])), f"steps didn't grow: {deltas}"
    assert deltas[-1] > deltas[0], f"no acceleration: {deltas}"
    print(f"OK: holding → accelerates (step deltas = {deltas})")

    print("HOTKEYS OK: ↑/↓ mark; edit focuses video; hold ←/→ accelerates; I/O work over lists")
    win.close()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
