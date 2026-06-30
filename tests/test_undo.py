"""Undo: each user gesture is one reversible step.

Checks that one gesture = one undo step (even when setting Out auto-adds a clip),
that undo restores clips + marks + roster together, and that no-op actions create
no undo step. Runs offscreen.

Run:  QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python tests\\test_undo.py
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

from PySide6.QtWidgets import QApplication  # noqa: E402

import marker  # noqa: E402


def main():
    root = Path(__file__).resolve().parent.parent
    video = str(root / "sample" / "trial_4k.mp4")
    app = QApplication([])
    win = marker.MarkerWindow(video)
    win.show(); win.activateWindow(); win.raise_()
    deadline = time.time() + 10
    while time.time() < deadline and win.player.duration() <= 0:
        app.processEvents(); time.sleep(0.02)

    def reset_roster():
        win._run_order = []
        win._roster_all = ["Amy Ace", "Bob Bolt", "Cy Comet"]
        win._available = list(win._roster_all)
        win._refresh_roster()

    # 1) Set In is one undo step; undo clears it.
    reset_roster()
    win.clips = []
    win.in_point = win.out_point = None
    win._undo_stack.clear(); win._update_undo_ui()
    win.player.seek(5.0); app.processEvents()
    win.set_in()
    assert win.in_point is not None and len(win._undo_stack) == 1, (win.in_point, len(win._undo_stack))
    assert win.undo_btn.isEnabled()
    win.undo()
    assert win.in_point is None and not win._undo_stack and not win.undo_btn.isEnabled()
    print("OK: undo reverts Set In (and disables the button when empty)")

    # 2) Assigning a name with In/Out set auto-adds a clip — ONE gesture, ONE step.
    win.clips = []; reset_roster()
    win.in_point, win.out_point = 2.0, 4.0
    win.label_edit.clear()
    win._undo_stack.clear()
    win._pick_participant("Amy Ace")
    assert len(win.clips) == 1 and win.clips[0].label == "Amy Ace", win.clips
    assert "Amy Ace" not in win._available, "roster not consumed"
    assert len(win._undo_stack) == 1, f"expected 1 undo step, got {len(win._undo_stack)}"
    win.undo()
    assert len(win.clips) == 0, "clip not removed by undo"
    assert "Amy Ace" in win._available, "roster not restored by undo"
    assert win.in_point == 2.0 and win.out_point == 4.0, "marks not restored by undo"
    print("OK: undo reverts an auto-add (clip + roster + marks together) in one step")

    # 3) Delete is undoable: the clip comes back and the participant is re-consumed.
    win.clips = []; reset_roster()
    win.in_point, win.out_point = 1.0, 3.0
    win.label_edit.clear()
    win._pick_participant("Bob Bolt")          # adds a clip from the roster
    win._undo_stack.clear()
    win.table.selectRow(0)
    win.delete_selected()
    assert len(win.clips) == 0 and "Bob Bolt" in win._available
    win.undo()
    assert len(win.clips) == 1 and win.clips[0].label == "Bob Bolt"
    assert "Bob Bolt" not in win._available, "participant should be re-consumed after undoing a delete"
    print("OK: undo reverts a delete (clip back, participant re-consumed)")

    # 4) A no-op action records no undo step.
    win._undo_stack.clear()
    win.table.clearSelection()
    win.move_up()                               # nothing selected -> no change
    assert win._undo_stack == [], "a no-op action created an undo step"
    print("OK: a no-op action creates no undo step")

    # 5) Undo with an empty stack is harmless.
    win._undo_stack.clear()
    win.undo()
    print("OK: undo on an empty stack is a safe no-op")

    print("UNDO OK: one gesture = one reversible step; clips/marks/roster restored together")
    win.close(); sys.stdout.flush(); os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
