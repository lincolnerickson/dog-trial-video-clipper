"""Running order reused across camera views.

View 1 establishes the participant order (the order clips were marked in). Views
2+ reload that order so the roster is sorted to it and Enter adds the next run.
Runs offscreen.

Run:  QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python tests\\test_run_order.py
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
from clipper import clips as clips_mod  # noqa: E402


def main():
    root = Path(__file__).resolve().parent.parent
    video = str(root / "sample" / "trial_4k.mp4")
    app = QApplication([])
    win = marker.MarkerWindow(video)
    win.show()
    win.activateWindow()
    win.raise_()

    deadline = time.time() + 10
    while time.time() < deadline and win.player.duration() <= 0:
        app.processEvents()
        time.sleep(0.02)

    # --- View 1: the running order is the order clips were marked in. ---
    order_view1 = ["Sara Tracer", "Lincoln Otter", "Jess Bramble"]
    win.clips = [clips_mod.Clip(start=i * 2.0, end=i * 2.0 + 1.0, label=name)
                 for i, name in enumerate(order_view1)]
    for c in win.clips:
        c.source_participant = c.label
    got = win._running_order_from_clips()
    assert got == order_view1, f"running order extracted from clips is wrong: {got}"
    print(f"OK: running order extracted from view-1 clips = {got}")

    # --- View 2: load that order; a roster in a DIFFERENT order sorts to it. ---
    win.clips = []
    win._roster_all = ["Lincoln Otter", "Jess Bramble", "Sara Tracer"]  # e.g. CSV/alpha order
    win._available = list(win._roster_all)
    win._run_order = list(order_view1)
    win._refresh_roster()
    assert win._available == order_view1, f"roster not sorted to running order: {win._available}"
    assert win._next_participant() == "Sara Tracer", win._next_participant()
    print(f"OK: view-2 roster sorted to running order, next up = {win._next_participant()}")

    # --- ↑/↓ then Enter adds the next participant and advances the queue. ---
    added = []
    for _ in order_view1:
        win.in_point, win.out_point = 1.0, 2.0   # stand in for ↑ / ↓
        win.label_edit.clear()
        win._on_enter()                          # no name yet -> grab the next run
        app.processEvents()
        added.append(win.clips[-1].label)
    assert added == order_view1, f"Enter did not add participants in running order: {added}"
    assert win._available == [], f"roster should be fully consumed: {win._available}"
    print(f"OK: ↑/↓ + Enter added participants in running order = {added}")

    # --- A name not in the order is tolerated: it sorts after the known ones. ---
    win._available = ["Mystery Dog", "Jess Bramble", "Sara Tracer"]
    win._refresh_roster()
    assert win._available == ["Sara Tracer", "Jess Bramble", "Mystery Dog"], win._available
    print("OK: an unknown name sorts after the running-order names")

    # --- Enter also adds the next roster participant with NO running order set
    # (view 1, straight from the loaded CSV in its own order). ---
    win.clips = []
    win._run_order = []
    win._roster_all = ["Amy Ace", "Bob Bolt", "Cy Comet"]
    win._available = list(win._roster_all)
    win._refresh_roster()
    assert win._next_participant() == "Amy Ace", win._next_participant()
    plain = []
    for _ in range(3):
        win.in_point, win.out_point = 1.0, 2.0
        win.label_edit.clear()
        win._on_enter()
        app.processEvents()
        plain.append(win.clips[-1].label)
    assert plain == ["Amy Ace", "Bob Bolt", "Cy Comet"], f"Enter w/o running order: {plain}"
    print(f"OK: Enter adds next roster participant with no running order = {plain}")

    print("RUN ORDER OK: Enter adds next (with or without a running order); view 2 sorts to view 1")
    win.close()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
