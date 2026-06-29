"""Lightweight self-tests for the shared core. Run: python tests/test_core.py

No pytest dependency -- plain asserts so it works in the bare venv.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipper import clips, naming, timecode  # noqa: E402
from clipper.ffmpeg_tools import StreamInfo, build_cut_command, h264_quality_args  # noqa: E402
from markerlib import roster  # noqa: E402


def test_timecode_roundtrip():
    assert timecode.parse_timecode("00:14:32.5") == 14 * 60 + 32.5
    assert timecode.parse_timecode("14:32") == 14 * 60 + 32
    assert timecode.parse_timecode("872.5") == 872.5
    assert timecode.parse_timecode(872) == 872.0
    assert timecode.format_timecode(872.5) == "00:14:32.500"
    assert timecode.format_timecode(3661.0, decimals=1) == "01:01:01.0"
    for raw in ("00:14:32.5", "1:02:03.250", "5"):
        assert abs(
            timecode.parse_timecode(timecode.format_timecode(timecode.parse_timecode(raw)))
            - timecode.parse_timecode(raw)
        ) < 1e-6


def test_bad_timecode():
    for bad in ("", "abc", "1:2:3:4", "00:99:00"):
        try:
            timecode.parse_timecode(bad)
        except timecode.TimecodeError:
            continue
        raise AssertionError(f"expected failure on {bad!r}")


def test_sanitize():
    # spaces and case are preserved; only illegal chars are made safe
    assert naming.sanitize_label("Smith / Rex's run!") == "Smith Rexs run"
    assert naming.sanitize_label("Jones\\Bella") == "Jones Bella"
    assert naming.sanitize_label("   ") == "clip"
    assert naming.sanitize_label("CON") == "CON_clip"
    assert naming.sanitize_label("Max  Power") == "Max Power"
    # apostrophes drop, not split
    assert naming.sanitize_label("O'Brien") == "OBrien"


def test_build_filename():
    assert naming.build_filename("Max") == "Max.mp4"
    assert naming.build_filename("Sara", ext="mov") == "Sara.mov"
    # participant + search reads naturally: spaces and case preserved, no prefix
    assert naming.build_filename("Sara - Interior Search 1") == "Sara - Interior Search 1.mp4"
    # illegal characters are made safe but the name stays readable
    assert naming.build_filename("Smith/Rex") == "Smith Rex.mp4"


def test_unique_dest(tmp=Path(__file__).resolve().parent / "_tmp_dedup"):
    import cutter

    tmp.mkdir(exist_ok=True)
    target = tmp / "Sara Tracer - Interior Search 1.mp4"

    # In-batch: same name twice -> (2), (3) so two clips don't clobber each other.
    used: set[str] = set()
    d1 = cutter.unique_dest(target, used); used.add(str(d1).lower())
    d2 = cutter.unique_dest(target, used); used.add(str(d2).lower())
    d3 = cutter.unique_dest(target, used)
    assert d1.name == "Sara Tracer - Interior Search 1.mp4"
    assert d2.name == "Sara Tracer - Interior Search 1 (2).mp4"
    assert d3.name == "Sara Tracer - Interior Search 1 (3).mp4"

    # Across exports (option 2): a file already on disk is preserved, not overwritten.
    target.write_bytes(b"x")
    d = cutter.unique_dest(target, set())
    assert d.name == "Sara Tracer - Interior Search 1 (2).mp4"
    target.unlink()
    tmp.rmdir()


def test_folder_per_participant():
    import cutter

    out = Path("out")
    # GUI path: the marker captured the participant -> folder is the Handler Dog pair.
    c = clips.Clip(0, 1, "Sara Tracer - Interior Search 1")
    c.source_participant = "Sara Tracer"
    p = cutter.out_path_for(out, "Sara Tracer - Interior Search 1.mp4", c, True)
    assert p == out / "Sara Tracer" / "Sara Tracer - Interior Search 1.mp4"

    # Same handler, different dog -> a different folder.
    c2 = clips.Clip(0, 1, "Sara Otter - Interior Search 1")
    c2.source_participant = "Sara Otter"
    p2 = cutter.out_path_for(out, "Sara Otter - Interior Search 1.mp4", c2, True)
    assert p2.parent.name == "Sara Otter"

    # CLI path: no participant set -> derived from the label before " - ".
    c3 = clips.Clip(0, 1, "Lincoln Otter - NW2 Exterior")
    p3 = cutter.out_path_for(out, "Lincoln Otter - NW2 Exterior.mp4", c3, True)
    assert p3.parent.name == "Lincoln Otter"

    # Flat mode: straight into the output folder.
    assert cutter.out_path_for(out, "x.mp4", c, False) == out / "x.mp4"


def test_validation():
    rows = [
        clips.Clip(start=10, end=20, label="A"),
        clips.Clip(start=30, end=25, label="B"),     # end before start -> error
        clips.Clip(start=40, end=50, label="   "),   # blank label -> error
        clips.Clip(start=18, end=28, label="C"),      # overlaps A -> warning
    ]
    issues = clips.validate(rows)
    errors = [i for i in issues if i.is_error]
    warnings = [i for i in issues if not i.is_error]
    assert any("not after start" in i.message for i in errors)
    assert any("blank" in i.message for i in errors)
    assert any("overlap" in i.message for i in warnings)
    assert clips.has_errors(issues)


def test_csv_roundtrip(tmp=Path(__file__).resolve().parent / "_tmp_clips.csv"):
    rows = [
        clips.Clip(start=872.0, end=1025.0, label="Smith_Rex"),
        clips.Clip(start=1188.0, end=1350.0, label="Jones / Bella", exact=True),
    ]
    clips.write_csv(tmp, rows)
    back = clips.read_csv(tmp)
    assert len(back) == 2
    assert back[0].label == "Smith_Rex"
    assert abs(back[0].start - 872.0) < 1e-3
    assert back[1].exact is True
    tmp.unlink()


def test_roster_handler_dog(tmp=Path(__file__).resolve().parent / "_tmp_roster.csv"):
    tmp.write_text("handler,dog\nSmith,Rex\nJones,Bella\nO'Brien,Max\n", encoding="utf-8")
    names = roster.load_participants(tmp)
    tmp.unlink()
    assert names == ["Smith Rex", "Jones Bella", "O'Brien Max"], names
    # and they sanitize to the expected filename stems
    assert naming.sanitize_label(names[0]) == "Smith Rex"
    assert naming.sanitize_label(names[2]) == "OBrien Max"


def test_roster_variants(tmp=Path(__file__).resolve().parent / "_tmp_roster2.csv"):
    # no header, two columns -> joined in order
    tmp.write_text("Smith,Rex\nJones,Bella\n", encoding="utf-8")
    assert roster.load_participants(tmp) == ["Smith Rex", "Jones Bella"]
    # single name column with header, plus an id column that's ignored
    tmp.write_text("bib,participant\n12,Smith / Rex\n7,Jones Bella\n", encoding="utf-8")
    assert roster.load_participants(tmp) == ["Smith / Rex", "Jones Bella"]
    # 'participant' + 'dog' header -> joined (the user's real layout)
    tmp.write_text("participant,dog\nSmith,Rex\nJones,Bella\n", encoding="utf-8")
    assert roster.load_participants(tmp) == ["Smith Rex", "Jones Bella"]
    # 'participant' + 'dog' with an ignored id column
    tmp.write_text("bib,participant,dog\n3,Smith,Rex\n", encoding="utf-8")
    assert roster.load_participants(tmp) == ["Smith Rex"]
    tmp.unlink()


def test_cut_command_shape():
    copy_cmd = build_cut_command("ffmpeg", "in.mp4", 100.0, 160.0, "out.mp4")
    assert "-c" in copy_cmd and "copy" in copy_cmd
    assert copy_cmd[copy_cmd.index("-ss") + 1] == "00:01:40.000"
    assert copy_cmd[copy_cmd.index("-t") + 1] == "00:01:00.000"  # duration, not -to
    exact_cmd = build_cut_command("ffmpeg", "in.mp4", 100.0, 160.0, "out.mp4", exact=True)
    assert "libx264" in exact_cmd
    assert "copy" not in exact_cmd


def test_websafe_command_shape():
    # An H.265 source must be re-encoded to a browser-safe H.264 (faststart).
    hevc = StreamInfo(vcodec="hevc", width=1920, height=1080, pix_fmt="yuv420p",
                      has_audio=True, acodec="aac")
    cmd = build_cut_command("ffmpeg", "in.mp4", 0.0, 5.0, "out.mp4",
                            web_safe=True, encoder="libx264", src_info=hevc)
    assert "+faststart" in cmd
    assert "yuv420p" in cmd and "high" in cmd and "libx264" in cmd
    assert "copy" not in cmd

    # An already web-safe H.264 source is stream-copied (no re-encode), faststart.
    h264 = StreamInfo(vcodec="h264", width=1920, height=1080, pix_fmt="yuv420p",
                      has_audio=True, acodec="aac")
    cmd2 = build_cut_command("ffmpeg", "in.mp4", 0.0, 5.0, "out.mp4",
                             web_safe=True, encoder="libx264", src_info=h264)
    assert "+faststart" in cmd2
    assert cmd2[cmd2.index("-c:v") + 1] == "copy"
    assert "libx264" not in cmd2


def test_h264_quality_args():
    assert h264_quality_args("libx264", 20, "fast") == ["-preset", "fast", "-crf", "20"]
    assert "-cq" in h264_quality_args("h264_nvenc", 20, "fast")
    assert "-global_quality" in h264_quality_args("h264_qsv", 20, "fast")
    assert "-qp_i" in h264_quality_args("h264_amf", 20, "fast")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
