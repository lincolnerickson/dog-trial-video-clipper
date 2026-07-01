"""Queue persistence: unfinished export + join jobs survive a round-trip to disk so
a crashed/quit batch can resume. Uses a temp app-data dir so the real one is
untouched.

Run: python tests/test_session.py
"""
import logging
import sys
import tempfile
from pathlib import Path

logging.disable(logging.CRITICAL)  # the corrupt-file case logs (expected) — keep output clean
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipper.clips import Clip
from clipper.ffmpeg_tools import CardSpec
from markerlib import applog, session


def main() -> int:
    # Redirect app-data to a temp dir for the test.
    tmp = Path(tempfile.mkdtemp())
    applog.app_data_dir = lambda: tmp  # type: ignore
    session.queue_path = lambda: tmp / "queue.json"  # type: ignore

    export_job = {
        "video": "/vids/view1.mp4",
        "rows": [Clip(1.0, 3.5, "Amy Ace - Interior", exact=True, source_participant="Amy Ace"),
                 Clip(9.0, 12.0, "Bob Bolt - Interior")],
        "out_dir": "/out/Interior",
        "folder_per_participant": True,
        "intro": CardSpec(image="/cards/in.png", seconds=2.5),
        "outro": None,
        "video_mode": "copy",
        "bitrate": 0,
        "label": "Interior",
        "csv": "/out/Interior/Interior clips.csv",
    }
    join_job = {
        "inputs": ["/vids/GX010001.mp4", "/vids/GX020001.mp4"],
        "out_path": "/out/rec1_delivery.mp4",
        "total": 3600.0,
        "trim_black": True,
        "encoder": "hevc_videotoolbox",
        "bitrate": 30000,
        "gop": 60,
        "label": "rec1_delivery",
    }

    session.save_jobs([join_job, export_job])           # running/queued mix
    assert (tmp / "queue.json").exists()

    restored = session.load_jobs()
    assert len(restored) == 2, restored
    (k0, j0), (k1, j1) = restored
    assert k0 == "join" and j0["inputs"] == join_job["inputs"] and j0["gop"] == 60
    assert k1 == "export"
    # Clips came back as real Clip objects with all fields intact.
    assert [type(c) for c in j1["rows"]] == [Clip, Clip]
    assert j1["rows"][0].start == 1.0 and j1["rows"][0].exact is True
    assert j1["rows"][0].source_participant == "Amy Ace"
    # Cards came back as CardSpec / None.
    assert isinstance(j1["intro"], CardSpec) and j1["intro"].seconds == 2.5
    assert j1["outro"] is None
    assert j1["folder_per_participant"] is True

    # Clearing removes the file (clean state -> no resume prompt next launch).
    session.clear()
    assert not (tmp / "queue.json").exists()
    assert session.load_jobs() == []

    # A corrupt file is ignored, not fatal.
    (tmp / "queue.json").write_text("{ not json", encoding="utf-8")
    assert session.load_jobs() == []

    for p in tmp.glob("*"):
        p.unlink()
    tmp.rmdir()
    print("SESSION OK: export+join jobs persist and restore; clean/corrupt handled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
