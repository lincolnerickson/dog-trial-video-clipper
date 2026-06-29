"""Headless test of the marker's roster + click-to-assign + export-suffix workflow.

Runs under the Qt 'offscreen' platform (no display needed). It proves the
window builds, media loads, the roster loads, clicking a participant + setting
In/Out auto-adds a clip and consumes the participant, deleting a clip restores
the participant, and export filenames carry the Participant-Search suffix --
including a real ffmpeg cut to disk.

Run: QT_QPA_PLATFORM=offscreen python tests/smoke_marker.py <video>
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

import cutter  # noqa: E402
import marker  # noqa: E402
from clipper import clips as clips_mod  # noqa: E402
from clipper import naming  # noqa: E402
from clipper.ffmpeg_tools import find_ffmpeg  # noqa: E402
from markerlib import roster  # noqa: E402


def main():
    root = Path(__file__).resolve().parent.parent
    video = sys.argv[1] if len(sys.argv) > 1 else str(root / "sample" / "trial_4k.mp4")
    app = QApplication([])
    win = marker.MarkerWindow(video)
    win.show()

    # Let the media backend report duration/fps.
    deadline = time.time() + 10
    while time.time() < deadline and win.player.duration() <= 0:
        app.processEvents()
        time.sleep(0.02)
    print(f"loaded duration = {win.player.duration():.3f}s, fps = {win.player.fps():g}")

    # Load the roster (handler+dog) and set the search/event label.
    names = roster.load_participants(root / "sample" / "roster.csv")
    win._roster_all = list(names)
    win._available = list(names)
    win._refresh_roster()
    win.search_edit.setText("NW3_Interior")
    assert names[0] == "Smith Rex", names
    print("roster:", names)

    def mark(start, end, participant):
        win.in_point = start
        win._update_marks_ui()
        win._pick_participant(participant)          # click -> sets field, no commit yet
        assert win.out_point is None
        win.out_point = end
        win._maybe_autocommit()                    # "O" -> auto-add

    mark(5.0, 15.0, "Smith Rex")
    mark(18.5, 30.0, "Jones Bella")
    assert len(win.clips) == 2, win.clips
    assert win.clips[0].source_participant == "Smith Rex"
    assert "Smith Rex" not in win._available and "Jones Bella" not in win._available
    assert win._available == ["O'Brien Max", "Nguyen Scout"], win._available
    print("after 2 auto-adds, roster left:", win._available)

    # Delete clip 0 -> its participant returns to the roster, in original order.
    win.table.selectRow(0)
    win.delete_selected()
    assert len(win.clips) == 1
    assert win._available[0] == "Smith Rex", win._available
    print("after delete, roster left:", win._available)

    # Export label/filename reads as "Participant - Search" (no number prefix).
    eff = win._effective_clips()
    assert eff[0].label == "Jones Bella - NW3_Interior", eff[0].label
    fname = naming.build_filename(eff[0].label)
    assert fname == "Jones Bella - NW3_Interior.mp4", fname
    print("export filename:", fname)

    # CSV export round-trips the combined label.
    tmp_csv = root / "tests" / "_tmp_marker.csv"
    clips_mod.write_csv(tmp_csv, eff)
    assert clips_mod.read_csv(tmp_csv)[0].label == "Jones Bella - NW3_Interior"
    tmp_csv.unlink()

    # Real cut to disk -> confirm the actual file lands with the readable name.
    out_dir = root / "tests" / "_tmp_out"
    result = cutter.run_batch(find_ffmpeg(), video, eff, out_dir)
    produced = sorted(p.name for p in out_dir.glob("*.mp4"))
    assert produced == ["Jones Bella - NW3_Interior.mp4"], produced
    for p in out_dir.glob("*.mp4"):
        p.unlink()
    out_dir.rmdir()
    print("cut to disk:", produced, f"({result.elapsed:.2f}s)")

    # Folder-per-participant: the clip lands in a "Jones Bella/" subfolder.
    grouped_dir = root / "tests" / "_tmp_grouped"
    cutter.run_batch(find_ffmpeg(), video, eff, grouped_dir, folder_per_participant=True)
    grouped = sorted(p.relative_to(grouped_dir).as_posix() for p in grouped_dir.rglob("*.mp4"))
    assert grouped == ["Jones Bella/Jones Bella - NW3_Interior.mp4"], grouped
    for p in grouped_dir.rglob("*.mp4"):
        p.unlink()
    (grouped_dir / "Jones Bella").rmdir()
    grouped_dir.rmdir()
    print("folder per participant:", grouped)

    print("SMOKE OK: roster + click-to-assign + auto-add + restore + grouped export all worked")
    win.close()
    # Offscreen Qt teardown can segfault on interpreter shutdown (a C++ object
    # destruction-order issue) *after* the test has fully passed. Exit cleanly
    # so the exit code reflects the actual result.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
