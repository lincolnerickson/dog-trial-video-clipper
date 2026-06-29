"""Diagnose why a video might show blank in the marker.

Loads the file, captures decoded frames straight from a QVideoSink (bypassing
the on-screen widget), and reports codec / pixel format / frame count / errors.

  * frames decoded > 0  -> decoding works; blank picture is a WIDGET render
    issue -> fix is to switch the render path (QGraphicsVideoItem).
  * frames decoded == 0 (+ an error / odd pixel format) -> a DECODE issue
    (codec / 10-bit / HW path) -> fix is on the decode side.

Run:  .venv\\Scripts\\python tests\\diagnose_video.py "C:\\path\\to\\your.mp4"
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtMultimedia import (  # noqa: E402
    QAudioOutput,
    QMediaMetaData,
    QMediaPlayer,
    QVideoSink,
)
from PySide6.QtWidgets import QApplication  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("usage: python tests/diagnose_video.py <video-file>")
        return 2
    path = sys.argv[1]
    if not Path(path).exists():
        print(f"file not found: {path}")
        return 2

    app = QApplication([])
    player = QMediaPlayer()
    audio = QAudioOutput()
    sink = QVideoSink()
    player.setAudioOutput(audio)
    player.setVideoOutput(sink)

    stats = {"frames": 0, "size": None, "pixfmt": None}

    def on_frame(frame):
        stats["frames"] += 1
        if stats["size"] is None and frame.isValid():
            stats["size"] = f"{frame.width()}x{frame.height()}"
            try:
                stats["pixfmt"] = str(frame.surfaceFormat().pixelFormat())
            except Exception:
                stats["pixfmt"] = "?"

    sink.videoFrameChanged.connect(on_frame)
    errors = []
    player.errorOccurred.connect(lambda e, m: errors.append(f"{e}: {m}"))

    player.setSource(QUrl.fromLocalFile(path))
    player.play()

    end = time.time() + 3.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)

    meta = player.metaData()

    def safe(key):
        try:
            return meta.stringValue(key) or meta.value(key)
        except Exception as exc:  # noqa: BLE001
            return f"<unreadable: {exc}>"

    print("file        :", path)
    print("error       :", player.error(), "|", player.errorString() or "(none)")
    print("hasVideo    :", player.hasVideo(), " hasAudio:", player.hasAudio())
    print("duration(s) :", round(player.duration() / 1000.0, 2))
    print("video codec :", safe(QMediaMetaData.Key.VideoCodec))
    print("resolution  :", safe(QMediaMetaData.Key.Resolution))
    print("framerate   :", safe(QMediaMetaData.Key.VideoFrameRate))
    print("frames in 3s:", stats["frames"], " first frame:", stats["size"], " pixfmt:", stats["pixfmt"])
    if errors:
        print("errorOccurred:", errors)
    print()
    if stats["frames"] > 0:
        print(">>> Decoding WORKS. The blank picture is a widget-render issue; fix = switch render path.")
    else:
        print(">>> NO frames decoded. This is a decode/codec issue; fix = on the decode side.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
