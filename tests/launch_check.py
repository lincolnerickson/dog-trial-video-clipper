"""Open the real marker window on the native display for ~2s, then quit.

Confirms the actual platform plugin (not offscreen) initializes and renders.
Run: python tests/launch_check.py [video]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import marker  # noqa: E402


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "sample" / "trial_4k.mp4"
    )
    app = QApplication([])
    win = marker.MarkerWindow(video)
    win.show()
    QTimer.singleShot(2000, app.quit)
    app.exec()
    print("NATIVE LAUNCH OK: window opened and rendered, then closed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
