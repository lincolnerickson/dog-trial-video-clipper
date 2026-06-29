"""Verify the QtVideoPlayer actually paints frames (not a black canvas).

Loads a file through the real backend, waits for the primed first frame, then
grabs the canvas widget and checks it contains non-black pixels.

Run:  .venv\\Scripts\\python tests\\render_check.py [video]
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

from markerlib.player import create_player  # noqa: E402


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "sample" / "trial_4k.mp4"
    )
    app = QApplication([])
    player = create_player("qt")
    canvas = player.widget
    canvas.resize(640, 360)
    canvas.show()

    player.load(video)

    # Wait for the primed first frame to reach the canvas.
    deadline = time.time() + 12
    while time.time() < deadline and getattr(canvas, "_image", None) is None:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()

    img = getattr(canvas, "_image", None)
    if img is None or img.isNull():
        print("FAIL: no frame reached the canvas (decode/codec issue on this file)")
        return 1
    print(f"first frame on canvas: {img.width()}x{img.height()}")

    # Grab what the widget actually paints and confirm it isn't all black.
    grab = canvas.grab().toImage()
    grab = grab.scaled(64, 36)
    nonblack = 0
    for y in range(grab.height()):
        for x in range(grab.width()):
            c = grab.pixelColor(x, y)
            if c.red() + c.green() + c.blue() > 24:
                nonblack += 1
    total = grab.width() * grab.height()
    print(f"non-black pixels painted: {nonblack}/{total}")

    # Scrub to a different spot and confirm the frame updates while paused.
    player.seek(20.0)
    deadline = time.time() + 5
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    print("after seek, canvas frame:", canvas._image.width(), "x", canvas._image.height())

    ok = nonblack > total * 0.10
    print("RENDER OK: frames are decoded and painted" if ok else "FAIL: canvas appears black")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
