#!/usr/bin/env python3
"""Marking tool for the Dog Trial Video Clipper.

A lightweight native desktop app to scrub one long 4K trial recording and mark
one clip per participant run.

Workflow (roster + click-to-assign):
  1. Load the source video and a participant roster CSV (handler + dog columns).
     If the camera split the recording into chapters (GoPro auto-splits ~hourly),
     use "Join videos…" to stream-copy them into one continuous file first.
  2. Set In (Up arrow), click a participant in the roster to assign it, set Out
     (Down arrow) ->
     the clip is added automatically and that participant leaves the roster.
  3. Type a "search / event" label once; it is appended to every exported
     filename as ``Participant-Search``.

Export a clip-list CSV (for cutter.py) or the finished clips directly (the
"Export clips" button calls the same cutter code in-process).

Player playback is isolated behind markerlib.player.VideoPlayer so the 4K
scrubbing engine can be swapped without touching this UI.
"""

from __future__ import annotations

import copy
import csv
import functools
import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from clipper import clips as clips_mod
from clipper import naming, timecode
from clipper.ffmpeg_tools import (
    CardSpec,
    ConcatResult,
    FFmpegNotFound,
    concat_videos,
    detect_trailing_black,
    find_ffmpeg,
    find_hevc_encoder,
    probe_duration,
)
from markerlib import applog, roster, session
from markerlib.player import create_player
from markerlib.widgets import ParticipantList
import cutter
import logging

APP_VERSION = "1.0.16"
log = logging.getLogger("clipper")
_SIG_UNSET = object()   # distinct from None (an empty board) so the first autosave always runs

VIDEO_FILTER = (
    "Video files (*.mp4 *.mov *.mkv *.avi *.m4v *.mts *.m2ts *.mxf *.wmv);;All files (*)"
)
IMAGE_FILTER = (
    "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff);;All files (*)"
)
SPEEDS = [0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 8.0]
SHUTTLE = [1.0, 2.0, 4.0, 8.0]
UNDO_LIMIT = 100         # most recent undoable actions kept on the stack
ARROW_STEP = 0.75        # seconds a single ← / → tap moves the playhead
ARROW_STEP_SHIFT = 10.0  # seconds per press when Shift is held (fixed jump, no ramp)
ARROW_STEP_ACCEL = 0.3   # extra seconds added per held-key repeat — hold to scrub faster
ARROW_STEP_MAX = 12.0    # cap on the per-press step while a ← / → is held down
PARTICIPANT_PLACEHOLDER = "click a participant (or type a name) — clip auto-adds on Out"
# Header-ish first lines tolerated (and skipped) when loading a running-order file.
RUN_ORDER_HEADERS = {"participant", "participants", "name", "running order", "handler dog"}


def _mono_font() -> QFont:
    """A monospace font that exists on BOTH Windows and macOS.

    The timecode readouts must be monospace so their width stays constant as the
    digits change — otherwise, while scrubbing, the labels reflow every frame and
    the whole layout visibly shifts back and forth. ``Consolas`` is Windows-only,
    so we ask Qt for the platform's guaranteed fixed-pitch font (Consolas/Courier
    New on Windows, Menlo on macOS) and render it at the default UI size."""
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font = QFont(fixed.family())
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


def _undoable(method):
    """Mark a window method as one undo step.

    Snapshots the editable state *before* the action; if the action actually
    changed anything, the snapshot is pushed onto the undo stack afterward. A
    re-entrancy depth guard means a gesture that fans out into several decorated
    calls (e.g. setting Out auto-adds a clip) records exactly ONE undo step, and
    actions that change nothing (a no-op move, a cancelled dialog) record none."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        outermost = self._action_depth == 0
        before_snap = self._snapshot() if outermost else None
        before_sig = self._state_signature() if outermost else None
        self._action_depth += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._action_depth -= 1
            if outermost and self._state_signature() != before_sig:
                self._undo_stack.append(before_snap)
                del self._undo_stack[:-UNDO_LIMIT]
                self._update_undo_ui()
    return wrapper


def _is_clip_csv(path: str) -> bool:
    """True if the CSV's header has the clip-list columns (start & end)."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), [])
        cols = {(c or "").strip().lower() for c in header}
        return "start" in cols and "end" in cols
    except Exception:
        return False


def _natural_key(name: str):
    """Sort key that orders embedded numbers numerically (part2 < part10).

    This also puts GoPro chapters in record order for both naming schemes:
    GX010078 < GX020078, and the older GOPR0078 < GP010078 < GP020078.
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


class VideoArea(QWidget):
    """Focusable container for the video widget so unhandled key presses
    propagate up to the main window's keyPressEvent (our hotkey handler)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


class ExportWorker(QThread):
    """Runs the batch cut off the UI thread; reports per-row progress."""

    rowDone = Signal(int, int, object)   # rownum, total, RowOutcome
    finishedResult = Signal(object)      # BatchResult

    def __init__(self, ffmpeg, video, rows, out_dir, folder_per_participant,
                 intro=None, outro=None, video_mode="copy", crf=23, bitrate=0):
        super().__init__()
        self.ffmpeg = ffmpeg
        self.video = video
        self.rows = rows
        self.out_dir = out_dir
        self.folder_per_participant = folder_per_participant
        self.intro = intro
        self.outro = outro
        self.video_mode = video_mode
        self.crf = crf
        self.bitrate = bitrate

    def run(self):
        try:
            result = cutter.run_batch(
                self.ffmpeg, self.video, self.rows, self.out_dir,
                folder_per_participant=self.folder_per_participant,
                intro=self.intro,
                outro=self.outro,
                video_mode=self.video_mode,
                crf=self.crf,
                bitrate=self.bitrate,
                progress=lambda rn, tot, o: self.rowDone.emit(rn, tot, o),
                cancel=self.isInterruptionRequested,
            )
        except Exception:
            # Never let a worker exception vanish and stall the queue: log it and
            # report a failed batch so _pump() advances to the next job.
            logging.getLogger("clipper").exception("export worker crashed")
            result = cutter.BatchResult(
                outcomes=[cutter.RowOutcome(0, "batch", "failed", reason="internal error — see log")],
            )
        self.finishedResult.emit(result)


class JoinWorker(QThread):
    """Stream-copy joins several chapters into one file off the UI thread.

    When ``trim_black`` is set, each input's tail is scanned first and any
    trailing black frames (the DJI auto-split quirk) are trimmed via concat
    outpoints — still a lossless copy."""

    progress = Signal(float, object)   # seconds_done, total (float | None)
    analyzing = Signal(int, int, str)  # index, count, filename (black-frame scan)
    finishedResult = Signal(object)    # ConcatResult

    def __init__(self, ffmpeg, inputs, out_path, total_duration, trim_black=False,
                 encoder=None, bitrate=0, gop=0):
        super().__init__()
        self.ffmpeg = ffmpeg
        self.inputs = inputs
        self.out_path = out_path
        self.total_duration = total_duration
        self.trim_black = trim_black
        self.encoder = encoder
        self.bitrate = bitrate
        self.gop = gop

    def run(self):
        try:
            outpoints = None
            if self.trim_black:
                outpoints = []
                n = len(self.inputs)
                for i, p in enumerate(self.inputs):
                    if self.isInterruptionRequested():
                        self.finishedResult.emit(
                            ConcatResult(False, Path(self.out_path), [], cancelled=True)
                        )
                        return
                    self.analyzing.emit(i + 1, n, Path(p).name)
                    outpoints.append(detect_trailing_black(self.ffmpeg, p))
            result = concat_videos(
                self.ffmpeg, self.inputs, self.out_path,
                total_duration=self.total_duration,
                outpoints=outpoints,
                encoder=self.encoder, bitrate=self.bitrate, gop=self.gop,
                progress=lambda secs, tot: self.progress.emit(secs, tot),
                cancel=self.isInterruptionRequested,
            )
        except Exception:
            logging.getLogger("clipper").exception("join worker crashed")
            result = ConcatResult(False, Path(self.out_path), [], stderr="internal error — see log")
        self.finishedResult.emit(result)


class MarkerWindow(QMainWindow):
    def __init__(self, initial_video: str | None = None):
        super().__init__()
        self.setWindowTitle("Dog Trial Video Clipper — Marker")
        self.resize(1480, 860)

        self.player = create_player("qt", self)
        self.video_path: str | None = None
        self.clips: list[clips_mod.Clip] = []
        self.in_point: float | None = None
        self.out_point: float | None = None
        self.editing_row: int | None = None
        self._scrubbing = False
        self._arrow_held = 0     # consecutive ←/→ auto-repeats, for hold-to-accelerate
        self._undo_stack: list[dict] = []
        self._action_depth = 0   # re-entrancy guard so one gesture = one undo step
        self._ffmpeg: str | None = None
        self._export_worker: ExportWorker | None = None
        self._join_worker: JoinWorker | None = None
        # Background job queues: exports (each captures its own source video +
        # clip snapshot) and joins (encode a whole recording overnight). Only one
        # heavy job runs at a time -- see _pump(); joins and exports never fight.
        self._export_queue: list[dict] = []
        self._export_results: list = []          # completed jobs, for the batch summary
        self._current_job: dict | None = None
        self._join_queue: list[dict] = []
        self._join_results: list = []
        self._current_join: dict | None = None
        self._hevc_encoder: str | None = None    # cached fastest HEVC encoder
        # Optional cards added to every exported clip: intro (prepended), outro
        # (appended, e.g. a bullseye map of where the hides were).
        self.intro_image: str | None = None
        self.outro_image: str | None = None

        # Roster state: full order (for restoring) + currently-available names.
        self._roster_all: list[str] = []
        self._available: list[str] = []
        # Reference "running order" reused across camera views: once set (saved
        # from view 1's clips, or loaded from a file), the available roster is
        # kept sorted by it so the top name is always the next run, and Enter
        # (with no name yet) grabs that top name. Empty = feature off.
        self._run_order: list[str] = []

        self._build_ui()
        self._connect_player()
        self._refresh_table()
        self._refresh_roster()
        self._update_marks_ui()

        # App-wide filter so the letter hotkeys (I/O/J/K/L) still fire when a
        # list/table/button has focus -- those widgets would otherwise eat them
        # for type-ahead search. Text fields are exempted (so names type normally).
        QApplication.instance().installEventFilter(self)

        # Undo the last action with the platform-native shortcut (Ctrl+Z / ⌘Z).
        undo_sc = QShortcut(QKeySequence.StandardKey.Undo, self)
        undo_sc.activated.connect(self.undo)

        if initial_video:
            self.load_video(initial_video)
        self._focus_video()

        # After the window is up, offer to recover anything left over from a
        # session that ended before finishing (crash or quit): unfinished background
        # jobs AND the in-progress marking board. Then start the marking autosave so
        # a future crash mid-marking is recoverable too.
        self._pending_resume = session.load_jobs()
        self._pending_marking = session.load_marking()
        self._last_marking_sig = _SIG_UNSET
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(5000)
        self._autosave_timer.timeout.connect(self._autosave_marking)
        QTimer.singleShot(0, self._offer_startup_recovery)

    def _offer_startup_recovery(self):
        # Headless/offscreen (tests) must never block on a modal recovery prompt.
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            # Sequential (each modal completes before the next) to avoid stacked dialogs.
            self._offer_resume()             # unfinished background jobs (queue.json)
            self._offer_marking_restore()    # in-progress marking board (marking.json)
        self._autosave_timer.start()         # begin autosaving only after recovery is settled

    def _autosave_marking(self):
        """Persist the live marking board so a crash mid-marking can be restored.
        Skips redundant writes; an empty board clears the recovery file."""
        has_state = bool(self.clips) or self.in_point is not None or self.out_point is not None
        sig = self._marking_signature() if has_state else None
        if sig == self._last_marking_sig:
            return
        self._last_marking_sig = sig
        if not has_state:
            session.clear_marking()
            return
        session.save_marking({
            "video": self.video_path,
            "search": self.search_edit.text(),
            "position": self.player.position(),
            "in_point": self.in_point,
            "out_point": self.out_point,
            "clips": self.clips,                 # Clip objects; session serializes them
            "roster_all": list(self._roster_all),
            "available": list(self._available),
            "run_order": list(self._run_order),
            "intro": self.intro_image,
            "outro": self.outro_image,
        })

    def _marking_signature(self):
        return (self.video_path, self.search_edit.text(), self.in_point, self.out_point,
                tuple((c.start, c.end, c.label, c.exact, c.source_participant) for c in self.clips))

    def _offer_marking_restore(self):
        data = self._pending_marking
        self._pending_marking = None
        if not data or not data.get("clips"):
            return
        n = len(data["clips"])
        vid = data.get("video")
        vidname = Path(vid).name if vid else "your last video"
        search = data.get("search") or ""
        extra = f" — “{search}”" if search else ""
        keep = QMessageBox.question(
            self, "Restore last session",
            f"You had {n} clip(s) in progress on {vidname}{extra} that were never "
            "exported (the app may have closed unexpectedly).\n\nRestore that marking "
            "session? (Choosing No discards it — nothing else is affected.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes
        if not keep:
            session.clear_marking()
            log.info("user discarded a restorable marking session (%d clips)", n)
            return
        self._restore_marking(data)

    def _restore_marking(self, data):
        self._roster_all = list(data.get("roster_all", []))
        self._available = list(data.get("available", self._roster_all))
        self._run_order = list(data.get("run_order", []))
        self.clips = list(data.get("clips", []))
        self.in_point = data.get("in_point")
        self.out_point = data.get("out_point")
        self.search_edit.setText(data.get("search") or "")
        self.intro_image = data.get("intro")
        self.outro_image = data.get("outro")
        self._update_card_label("intro")
        self._update_card_label("outro")
        self._undo_stack.clear()
        self._update_undo_ui()
        vid = data.get("video")
        if vid and Path(vid).exists():
            self.load_video(vid)
            pos = data.get("position")
            if pos:
                QTimer.singleShot(500, lambda: self.player.seek(pos))  # after the video loads
        elif vid:
            self.statusBar().showMessage(
                f"Clips restored, but the video moved ({Path(vid).name}). Reopen it with Open video…", 12000)
        self._refresh_table()
        self._refresh_roster()
        self._update_marks_ui()
        log.info("restored marking session: %d clips, video=%s", len(self.clips), vid)
        self.statusBar().showMessage(f"Restored {len(self.clips)} clip(s) from your last session.", 8000)

    def _offer_resume(self):
        jobs = getattr(self, "_pending_resume", [])
        self._pending_resume = []
        if not jobs:
            return
        n = len(jobs)
        labels = "\n".join(f"   • {kind}: {job.get('label', '?')}" for kind, job in jobs[:8])
        more = f"\n   … and {n - 8} more" if n > 8 else ""
        keep = QMessageBox.question(
            self, "Resume unfinished jobs",
            f"{n} background job(s) from your last session didn't finish "
            "(the app may have closed unexpectedly):\n\n" + labels + more +
            "\n\nResume them now? (Choosing No discards the list — the source "
            "files are untouched.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes
        if not keep:
            session.clear()
            log.info("user discarded %d resumable job(s)", n)
            return
        try:
            self._ffmpeg = self._ffmpeg or find_ffmpeg()
        except FFmpegNotFound as exc:
            QMessageBox.critical(self, "Resume jobs", str(exc))
            return
        for kind, job in jobs:
            (self._join_queue if kind == "join" else self._export_queue).append(job)
        log.info("resuming %d job(s) from the previous session", n)
        self.statusBar().showMessage(f"Resuming {n} unfinished background job(s)…", 8000)
        self._pump()

    def _on_unhandled(self, exc_type, exc, tb):
        """Top-level exception already logged by the excepthook -- tell the user
        non-fatally and keep running. Guarded so the notice can't itself crash."""
        try:
            name = getattr(exc_type, "__name__", "Error")
            self.statusBar().showMessage(
                f"Something went wrong ({name}) — it's been logged; the app is still running. "
                "If it recurs, send the log from Help ▸ about the log location.", 15000)
        except Exception:
            pass

    def closeEvent(self, event):
        """On quit, if background jobs are still running/queued, confirm and save
        them for resume (and stop the worker so ffmpeg isn't left orphaned)."""
        self._autosave_marking()   # flush the latest marks so un-exported work can be restored
        running = (self._export_worker is not None) or (self._join_worker is not None)
        pending = len(self._export_queue) + len(self._join_queue)
        if running or pending:
            n = pending + (1 if running else 0)
            keep = QMessageBox.question(
                self, "Background jobs still running",
                f"{n} background job(s) are still encoding or queued. Quit anyway?\n\n"
                "They’ll resume automatically the next time you open the app.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes
            if not keep:
                event.ignore()
                return
            self._persist_queues()            # capture running + queued for resume
            for w in (self._export_worker, self._join_worker):
                if w:
                    w.requestInterruption()
                    w.wait(3000)
            log.info("quit with %d unfinished job(s); saved for resume", n)
        else:
            session.clear()                   # clean exit -> nothing to resume
        super().closeEvent(event)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([880, 600])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage(
            "Open a video + roster. Then: ↑ (in) · click a participant · ↓ (out) → clip auto-added."
        )

    def _build_left(self) -> QWidget:
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(6, 6, 6, 6)

        # Video
        self.video_area = VideoArea()
        va = QVBoxLayout(self.video_area)
        va.setContentsMargins(0, 0, 0, 0)
        self.player.widget.setMinimumHeight(360)
        va.addWidget(self.player.widget)
        col.addWidget(self.video_area, stretch=1)

        # Scrub slider + time
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.sliderMoved.connect(self._slider_moved)
        col.addWidget(self.slider)

        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        self.time_label.setFont(_mono_font())
        self.fps_label = QLabel("fps —")
        time_row = QHBoxLayout()
        time_row.addWidget(self.time_label)
        time_row.addStretch(1)
        time_row.addWidget(self.fps_label)
        col.addLayout(time_row)

        # Transport buttons
        trow = QHBoxLayout()
        self.open_btn = self._btn("Open video…", self.open_dialog, focusable=True)
        self.join_btn = self._btn("Join videos…", self.join_videos_dialog, focusable=True)
        self.join_btn.setToolTip(
            "Join several GoPro chapters (one recording auto-split into ~1-hour files)\n"
            "into a single video, then load it for marking. Lossless, no re-encode."
        )
        self.play_btn = self._btn("Play (Space)", self.player.toggle_play)
        trow.addWidget(self.open_btn)
        trow.addWidget(self.join_btn)
        trow.addWidget(self.play_btn)
        trow.addSpacing(12)
        for text, fn in [
            ("« 10s", lambda: self.player.step_seconds(-10)),
            ("‹ 1s", lambda: self.player.step_seconds(-1)),
            ("‹ frame", lambda: self.player.step_frames(-1)),
            ("frame ›", lambda: self.player.step_frames(1)),
            ("1s ›", lambda: self.player.step_seconds(1)),
            ("10s »", lambda: self.player.step_seconds(10)),
        ]:
            trow.addWidget(self._btn(text, fn))
        trow.addSpacing(12)
        trow.addWidget(QLabel("Speed"))
        self.speed_combo = QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f"{s:g}×", s)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.currentIndexChanged.connect(
            lambda i: self.player.set_rate(self.speed_combo.itemData(i))
        )
        trow.addWidget(self.speed_combo)
        trow.addStretch(1)
        col.addLayout(trow)

        col.addWidget(self._hline())

        # Marking row
        mark = QVBoxLayout()
        r1 = QHBoxLayout()
        self.in_btn = self._btn("Set In (↑)", self.set_in, focusable=True)
        self.out_btn = self._btn("Set Out (↓)", self.set_out, focusable=True)
        self.in_label = QLabel("In —")
        self.out_label = QLabel("Out —")
        self.dur_label = QLabel("len —")
        for w in (self.in_label, self.out_label, self.dur_label):
            w.setFont(_mono_font())
            w.setMinimumWidth(130)
        r1.addWidget(self.in_btn)
        r1.addWidget(self.in_label)
        r1.addWidget(self.out_btn)
        r1.addWidget(self.out_label)
        r1.addWidget(self.dur_label)
        r1.addStretch(1)
        mark.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Participant"))
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText(PARTICIPANT_PLACEHOLDER)
        self.label_edit.returnPressed.connect(self._on_enter)
        self.label_edit.textChanged.connect(self._update_preview)
        r2.addWidget(self.label_edit, stretch=1)
        self.exact_check = QCheckBox("exact cut")
        self.exact_check.setToolTip(
            "Re-encode this one clip for a frame-accurate start.\n"
            "Default (off) is a fast stream copy that may start up to ~1s early."
        )
        r2.addWidget(self.exact_check)
        mark.addLayout(r2)

        r3 = QHBoxLayout()
        self.preview_label = QLabel("→ —")
        self.preview_label.setStyleSheet("color: #2a6;")
        r3.addWidget(self.preview_label, stretch=1)
        self.add_btn = self._btn("Add clip", self.add_or_update_clip, focusable=True)
        self.clear_marks_btn = self._btn("Clear marks", self.clear_marks, focusable=True)
        r3.addWidget(self.clear_marks_btn)
        r3.addWidget(self.add_btn)
        mark.addLayout(r3)
        col.addLayout(mark)

        return panel

    def _build_right(self) -> QWidget:
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(6, 6, 6, 6)

        # Search / event label (appended to every exported filename).
        srow = QHBoxLayout()
        srow.addWidget(QLabel("<b>Search / event label</b>"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("e.g. NW3 Interior — appended to every clip: Participant - Search")
        self.search_edit.textChanged.connect(self._update_preview)
        self.search_edit.returnPressed.connect(self._focus_video)  # Enter arms the marking hotkeys
        srow.addWidget(self.search_edit, stretch=1)
        col.addLayout(srow)

        # Optional intro/outro cards: a still image shown for N seconds at the
        # head (intro) and/or tail (outro) of every exported clip. The run
        # footage is still stream-copied; only the cards are encoded (see
        # clipper.ffmpeg_tools.run_cut_with_cards).
        col.addLayout(self._build_card_row(
            "intro", "Intro card",
            "An image placed at the START of every exported clip — like a title card.\n"
            "The run footage stays a lossless stream copy; only the card is encoded.",
        ))
        col.addLayout(self._build_card_row(
            "outro", "Outro card",
            "An image placed at the END of every exported clip — e.g. a bullseye map\n"
            "of where the hides were. Same image/position/duration on every clip.",
        ))

        # Web-safe delivery: re-encode to universal H.264 so clips play in any
        # browser (HEVC footage otherwise won't play in Chrome/Firefox).
        # Export video format: re-encode to a smaller HEVC delivery file (default),
        # keep the original (lossless copy), or browser-playable H.264.
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Export video:"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("Smaller — HEVC (recommended)", "hevc")
        self.format_combo.addItem("Original — no re-encode (largest, fastest)", "copy")
        self.format_combo.addItem("Web-safe — H.264 (plays in any browser)", "h264")
        self.format_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.format_combo.setToolTip(
            "Smaller (HEVC): re-encode to H.265 at the chosen quality — about half\n"
            "the size of the original, full detail kept, plays on modern devices.\n"
            "Re-encoding takes longer than a copy.\n"
            "Original: a lossless stream copy — largest, but exports in seconds.\n"
            "Web-safe (H.264): for clips that must play directly in Chrome/Firefox."
        )
        self.format_combo.currentIndexChanged.connect(self._update_format_ui)
        fmt_row.addWidget(self.format_combo, stretch=1)
        self.bitrate_label = QLabel("Bitrate")
        fmt_row.addWidget(self.bitrate_label)
        self.bitrate_spin = QDoubleSpinBox()
        self.bitrate_spin.setRange(2.0, 60.0)
        self.bitrate_spin.setValue(30.0)
        self.bitrate_spin.setSingleStep(1.0)
        self.bitrate_spin.setDecimals(1)
        self.bitrate_spin.setSuffix(" Mbps")
        self.bitrate_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.bitrate_spin.setToolTip(
            "Target bitrate for the HEVC re-encode, using your Mac's hardware\n"
            "encoder (fast, like CapCut). For high-bitrate GoPro 1080p60 footage,\n"
            "~12–15 Mbps looks clean and is much smaller than the source; dial down\n"
            "to find the smallest that still looks good (too low gets grainy)."
        )
        fmt_row.addWidget(self.bitrate_spin)
        col.addLayout(fmt_row)
        self._update_format_ui()

        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(self._build_roster_group())
        vsplit.addWidget(self._build_clips_group())
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 2)
        vsplit.setSizes([260, 460])
        col.addWidget(vsplit, stretch=1)

        # Export bar
        eb = QHBoxLayout()
        eb.addWidget(self._btn("Validate", self.validate_dialog, focusable=True))
        eb.addWidget(self._btn("Load clip CSV…", self.load_csv_dialog, focusable=True))
        eb.addStretch(1)
        eb.addWidget(self._btn("Export CSV…", self.export_csv, focusable=True))
        self.export_clips_btn = self._btn("Export clips…", self.export_clips, focusable=True)
        self.export_clips_btn.setStyleSheet("font-weight: bold;")
        eb.addWidget(self.export_clips_btn)
        col.addLayout(eb)

        # Inline, non-modal export progress: the cut runs in the background (on a
        # thread + the Mac's hardware encoder) so marking can continue while it
        # exports. Hidden until an export starts.
        self.export_progress_row = QWidget()
        pr = QHBoxLayout(self.export_progress_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.export_bar = QProgressBar()
        self.export_bar.setTextVisible(False)
        self.export_status = QLabel("")
        self.export_status.setStyleSheet("color: #888;")
        self.export_cancel_btn = self._btn("Cancel", self._cancel_background, focusable=True)
        pr.addWidget(self.export_bar, stretch=1)
        pr.addWidget(self.export_status)
        pr.addWidget(self.export_cancel_btn)
        self.export_progress_row.setVisible(False)
        col.addWidget(self.export_progress_row)
        return panel

    def _build_roster_group(self) -> QWidget:
        group = QGroupBox("Participants")
        v = QVBoxLayout(group)
        head = QHBoxLayout()
        self.roster_count = QLabel("0 left")
        head.addWidget(self._btn("Load roster…", self.load_roster, focusable=True))
        head.addWidget(self._btn("Restore all", self.restore_all_participants, focusable=True))
        head.addStretch(1)
        head.addWidget(self.roster_count)
        v.addLayout(head)

        # Running-order reuse across camera views (see _run_order).
        orow = QHBoxLayout()
        save_order = self._btn("Save running order…", self.save_running_order, focusable=True)
        save_order.setToolTip(
            "Save the order of participants in your clip list to a file.\n"
            "Do this after finishing the first view, then load it for the other views."
        )
        use_order = self._btn("Use saved order…", self.load_running_order, focusable=True)
        use_order.setToolTip(
            "Load a running order saved from an earlier view. The participant list is\n"
            "reordered to it, so the top name is always the next run — press Enter to add it."
        )
        orow.addWidget(save_order)
        orow.addWidget(use_order)
        orow.addStretch(1)
        v.addLayout(orow)

        self.participants = ParticipantList()
        self.participants.itemClicked.connect(
            lambda item: self._pick_participant(item.text())
        )
        v.addWidget(self.participants, stretch=1)
        self.roster_hint = QLabel("Click a name to add it to the current clip.")
        self.roster_hint.setStyleSheet("color: #888;")
        self.roster_hint.setWordWrap(True)
        v.addWidget(self.roster_hint)
        return group

    def _build_clips_group(self) -> QWidget:
        group = QGroupBox("Marked clips")
        v = QVBoxLayout(group)
        head = QHBoxLayout()
        self.count_label = QLabel("0 clips")
        head.addStretch(1)
        head.addWidget(self.count_label)
        v.addLayout(head)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Start", "End", "Len", "Participant", "Exact"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for c in (0, 1, 2, 3, 5):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(lambda *_: self.edit_selected())
        v.addWidget(self.table, stretch=1)

        rb = QHBoxLayout()
        rb.addWidget(self._btn("Edit", self.edit_selected, focusable=True))
        rb.addWidget(self._btn("Delete", self.delete_selected, focusable=True))
        rb.addWidget(self._btn("↑", self.move_up, focusable=True))
        rb.addWidget(self._btn("↓", self.move_down, focusable=True))
        rb.addStretch(1)
        self.undo_btn = self._btn("Undo", self.undo, focusable=True)
        self.undo_btn.setToolTip(
            "Undo the last action — set In/Out, assign a name, add/delete a clip,\n"
            "reorder, clear, load… (Ctrl+Z / ⌘Z)"
        )
        self.undo_btn.setEnabled(False)
        rb.addWidget(self.undo_btn)
        rb.addWidget(self._btn("Clear all", self.clear_all, focusable=True))
        v.addLayout(rb)

        delete_sc = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table)
        delete_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_sc.activated.connect(self.delete_selected)
        return group

    def _btn(self, text, fn, focusable=False) -> QPushButton:
        b = QPushButton(text)
        b.clicked.connect(fn)
        if not focusable:
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return b

    def _hline(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _build_card_row(self, kind: str, title: str, tooltip: str) -> QHBoxLayout:
        """One row of controls for an intro/outro card: a chosen-file label, a
        Choose/Clear pair, and a duration spinbox. Widgets are stored as
        ``self.<kind>_name_label`` etc. so one set of handlers drives both."""
        row = QHBoxLayout()
        row.addWidget(QLabel(title))
        name = QLabel("(none)")
        name.setStyleSheet("color: #888;")
        name.setToolTip(tooltip)
        row.addWidget(name, stretch=1)
        choose = self._btn("Choose image…", lambda: self._choose_card(kind), focusable=True)
        clear = self._btn("Clear", lambda: self._clear_card(kind), focusable=True)
        clear.setEnabled(False)
        row.addWidget(choose)
        row.addWidget(clear)
        row.addSpacing(12)
        row.addWidget(QLabel("Show for"))
        spin = QDoubleSpinBox()
        spin.setRange(0.5, 60.0)
        spin.setSingleStep(0.5)
        spin.setDecimals(1)
        spin.setValue(3.0)
        spin.setSuffix(" s")
        spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        row.addWidget(spin)
        setattr(self, f"{kind}_name_label", name)
        setattr(self, f"{kind}_clear_btn", clear)
        setattr(self, f"{kind}_seconds_spin", spin)
        return row

    def _update_format_ui(self):
        """The bitrate control applies to the HEVC ('Smaller') re-encode only."""
        is_hevc = self.format_combo.currentData() == "hevc"
        self.bitrate_label.setEnabled(is_hevc)
        self.bitrate_spin.setEnabled(is_hevc)

    # -------------------------------------------------------------- player

    def _connect_player(self):
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playingChanged.connect(self._on_playing)
        self.player.loaded.connect(self._on_loaded)
        self.player.errorOccurred.connect(self._on_player_error)

    def _on_position(self, seconds: float):
        if not self._scrubbing:
            self.slider.setValue(int(seconds * 1000))
        self._update_time_label()

    def _on_duration(self, seconds: float):
        self.slider.setRange(0, max(int(seconds * 1000), 0))
        self._update_time_label()

    def _on_playing(self, playing: bool):
        self.play_btn.setText("Pause (Space)" if playing else "Play (Space)")

    def _on_loaded(self, fps: float):
        self.fps_label.setText(f"fps {fps:g}")
        self.statusBar().showMessage(
            f"Loaded {Path(self.video_path).name}  ·  {timecode.format_timecode(self.player.duration(), 1)}  ·  {fps:g} fps",
            6000,
        )

    def _on_player_error(self, message: str):
        self.statusBar().showMessage(f"Player error: {message}", 8000)

    def _update_time_label(self):
        pos = self.player.position()
        dur = self.player.duration()
        self.time_label.setText(
            f"{timecode.format_timecode(pos)} / {timecode.format_timecode(dur)}"
        )

    # ------------------------------------------------------------- loading

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open video", "", VIDEO_FILTER)
        if path:
            self.load_video(path)

    def load_video(self, path: str):
        self.video_path = path
        self.player.load(path)
        self.setWindowTitle(f"Dog Trial Video Clipper — {Path(path).name}")
        self._focus_video()

    # ----------------------------------------------------- intro / outro cards

    def _choose_card(self, kind: str):
        path, _ = QFileDialog.getOpenFileName(self, f"Choose {kind} image", "", IMAGE_FILTER)
        if path:
            setattr(self, f"{kind}_image", path)
            self._update_card_label(kind)
            where = "start" if kind == "intro" else "end"
            self.statusBar().showMessage(
                f"{kind.capitalize()} card set: {Path(path).name} — added to the {where} of every clip.",
                5000,
            )
        self._focus_video()

    def _clear_card(self, kind: str):
        setattr(self, f"{kind}_image", None)
        self._update_card_label(kind)
        self._focus_video()

    def _resolve_card(self, kind: str) -> tuple[CardSpec | None, bool]:
        """Build the CardSpec for ``kind`` from the UI, or (None, True) if unset.
        Returns ok=False (after warning) when a chosen image has gone missing."""
        image = getattr(self, f"{kind}_image")
        if not image:
            return None, True
        if not Path(image).exists():
            QMessageBox.warning(
                self, "Export clips",
                f"The {kind} image is no longer there:\n{image}\n\n"
                "Choose it again or clear it, then export.",
            )
            return None, False
        seconds = getattr(self, f"{kind}_seconds_spin").value()
        return CardSpec(image=image, seconds=seconds), True

    def _update_card_label(self, kind: str):
        image = getattr(self, f"{kind}_image")
        name_label = getattr(self, f"{kind}_name_label")
        clear_btn = getattr(self, f"{kind}_clear_btn")
        if image:
            name_label.setText(Path(image).name)
            name_label.setStyleSheet("color: #2a6;")
            name_label.setToolTip(image)
            clear_btn.setEnabled(True)
        else:
            name_label.setText("(none)")
            name_label.setStyleSheet("color: #888;")
            name_label.setToolTip("")
            clear_btn.setEnabled(False)

    # --------------------------------------------------- join GoPro chapters

    def join_videos_dialog(self):
        """Join a recording's chapters into one file, optionally re-encoding it to
        delivery quality (HEVC at the export bitrate) so later clips need no
        encoding. Jobs run in a background queue — add several and let them run
        overnight; results do NOT auto-load (open one to mark when ready)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select the chapters of one recording", "", VIDEO_FILTER
        )
        if not paths:
            return
        paths = sorted(paths, key=lambda p: _natural_key(Path(p).name))
        try:
            self._ffmpeg = self._ffmpeg or find_ffmpeg()
        except FFmpegNotFound as exc:
            QMessageBox.critical(self, "Join videos", str(exc))
            return

        n = len(paths)
        mbps = self.bitrate_spin.value()
        box = QMessageBox(self)
        box.setWindowTitle("Join / prepare recording")
        box.setIcon(QMessageBox.Icon.Question)
        if n > 1:
            order = "\n".join(f"   {i + 1}.  {Path(p).name}" for i, p in enumerate(paths))
            box.setText(f"Join these {n} chapters into one recording, in order?\n\n{order}\n\n"
                        "Pick a save location next.")
        else:
            box.setText(f"Prepare “{Path(paths[0]).name}” for marking?\n\nPick a save location next.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        encode_cb = QCheckBox(
            f"Encode to delivery quality now (HEVC ~{mbps:g} Mbps) so clips export instantly later")
        encode_cb.setChecked(True)
        encode_cb.setToolTip(
            "Re-encode the whole recording to your delivery bitrate now — this is\n"
            "queued and can run overnight. Then clips cut from it are an instant\n"
            "stream copy (no per-clip encoding). Uncheck for a fast lossless join.\n"
            "Trailing black frames (DJI) are trimmed either way."
        )
        box.setCheckBox(encode_cb)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        encode = encode_cb.isChecked()

        if n == 1 and not encode:
            self.load_video(paths[0])   # nothing to join or encode — just open it
            return

        first = Path(paths[0])
        default_out = str(first.with_name(f"{first.stem}{'_delivery' if encode else '_joined'}.mp4"))
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save prepared video as", default_out, "MP4 video (*.mp4)"
        )
        if not out_path:
            return
        if any(Path(out_path).resolve() == Path(p).resolve() for p in paths):
            QMessageBox.warning(self, "Join videos",
                                "The output can’t be one of the input videos. Pick a different name.")
            return

        total: float | None = 0.0
        for p in paths:
            d = probe_duration(self._ffmpeg, p)
            if d is None:
                total = None
                break
            total += d

        if encode:
            self._hevc_encoder = self._hevc_encoder or find_hevc_encoder(self._ffmpeg)
            encoder, bitrate, gop = self._hevc_encoder, int(mbps * 1000), 60  # ~1s keyframes
        else:
            encoder, bitrate, gop = None, 0, 0

        self._join_queue.append({
            "inputs": paths, "out_path": out_path, "total": total,
            "trim_black": True, "encoder": encoder, "bitrate": bitrate, "gop": gop,
            "label": Path(out_path).stem,
        })
        pending = len(self._join_queue) + len(self._export_queue) + (
            1 if (self._join_worker or self._export_worker) else 0)
        self.statusBar().showMessage(
            f"Queued “{Path(out_path).stem}” ({'encode' if encode else 'join'}) — "
            f"{pending} in the background queue. Add more and let them run.", 9000,
        )
        self._pump()

    # ------------------------------------------------------------- roster

    def load_roster(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load participant roster", "", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        # Guard against accidentally feeding a clip list into the roster loader.
        if _is_clip_csv(path):
            QMessageBox.warning(
                self, "Load roster",
                "That file looks like a clip list (start/end/label), not a participant "
                "roster. Use 'Load clip CSV…' for that instead.",
            )
            return
        self._load_roster_path(path)

    @_undoable
    def _load_roster_path(self, path: str) -> bool:
        try:
            names = roster.load_participants(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Load roster", f"Could not read roster:\n{exc}")
            return False
        if not names:
            QMessageBox.information(self, "Load roster", "No participant names found in that file.")
            return False
        if self.clips and QMessageBox.question(
            self, "Load roster",
            "Load a new roster? Participants already used by existing clips won't be re-consumed.",
        ) != QMessageBox.StandardButton.Yes:
            return False
        used = {c.source_participant for c in self.clips if c.source_participant}
        self._roster_all = list(names)
        self._available = [n for n in names if n not in used]
        self._refresh_roster()
        self.statusBar().showMessage(
            f"Loaded {len(names)} participants from {Path(path).name}", 6000
        )
        return True

    def _refresh_roster(self):
        self._sort_available_by_run_order()
        self.participants.clear()
        self.participants.addItems(self._available)
        self.roster_count.setText(f"{len(self._available)} left")
        self._update_next_up()

    def _use_participant(self, name: str):
        if name in self._available:
            self._available.remove(name)
            self._refresh_roster()

    def _restore_participant(self, name: str):
        if name not in self._roster_all:
            return  # ad-hoc typed label, not from the roster
        order = self._roster_all
        idx_all = order.index(name)
        pos = len(self._available)
        for i, n in enumerate(self._available):
            ni = order.index(n) if n in order else 10**9
            if ni > idx_all:
                pos = i
                break
        self._available.insert(pos, name)
        self._refresh_roster()

    @_undoable
    def restore_all_participants(self):
        if not self._roster_all:
            return
        used = {c.source_participant for c in self.clips if c.source_participant}
        self._available = [n for n in self._roster_all if n not in used]
        self._refresh_roster()
        self.statusBar().showMessage("Roster restored (participants used by clips stay consumed).", 4000)

    @_undoable
    def _pick_participant(self, text: str):
        self.label_edit.setText(text)
        self._update_preview()
        self._maybe_autocommit()
        # Return focus to the video so the marking/transport hotkeys (O, Space,
        # arrows) keep working after a click instead of going to the roster list.
        self._focus_video()

    # ------------------------------------------------- running order (views)

    def _sort_available_by_run_order(self):
        """Keep the available roster sorted by the saved running order so the
        top name is always the next run. Names not in the order keep their
        relative position after the known ones (stable sort)."""
        if not self._run_order:
            return
        rank = {name: i for i, name in enumerate(self._run_order)}
        self._available.sort(key=lambda n: rank.get(n, len(rank)))

    def _next_participant(self) -> str | None:
        """The next participant Enter will add: the top of the available roster
        (in running order when one is loaded, otherwise CSV/load order)."""
        return self._available[0] if self._available else None

    def _update_next_up(self):
        """Surface who's next: in the field placeholder and the roster hint."""
        nxt = self._next_participant()
        if nxt:
            where = "next in running order" if self._run_order else "next participant"
            self.label_edit.setPlaceholderText(f"press Enter → {nxt}   ({where})")
            prefix = "Running order on · " if self._run_order else ""
            self.roster_hint.setText(
                f"{prefix}Next up: {nxt} · press Enter to add it (or click any name)."
            )
        else:
            self.label_edit.setPlaceholderText(PARTICIPANT_PLACEHOLDER)
            self.roster_hint.setText("Click a name to add it to the current clip.")

    def _pick_next_participant(self):
        """Assign the next participant — the top of the roster — same as clicking it."""
        nxt = self._next_participant()
        if nxt is None:
            self.statusBar().showMessage("No participants left in the roster.", 3000)
            return
        self._pick_participant(nxt)

    def _on_enter(self):
        """Enter: with a roster loaded and no name typed yet, grab the next
        participant (top of the list) so it auto-adds once In/Out are set. Works
        whether or not a running order is loaded. Otherwise add/update the clip."""
        if (self.editing_row is None and self._available
                and not self.label_edit.text().strip()):
            self._pick_next_participant()
            return
        self.add_or_update_clip()

    def _running_order_from_clips(self) -> list[str]:
        """The participant sequence implied by the current clip list (in table
        order, de-duplicated) — this is the running order to reuse next view."""
        seen: set[str] = set()
        order: list[str] = []
        for c in self.clips:
            name = (c.source_participant or c.label or "").strip()
            if name and name not in seen:
                seen.add(name)
                order.append(name)
        return order

    @_undoable
    def save_running_order(self):
        order = self._running_order_from_clips()
        if not order:
            QMessageBox.information(
                self, "Save running order",
                "Mark some clips first — the running order is taken from the order "
                "of the participants in your clip list.",
            )
            return
        default = "running_order.txt"
        if self.video_path:
            default = str(Path(self.video_path).with_name("running_order.txt"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save running order", default,
            "Text files (*.txt);;CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                for name in order:
                    fh.write(name + "\n")
        except OSError as exc:
            QMessageBox.critical(self, "Save running order", f"Could not write the file:\n{exc}")
            return
        # Activate it now too, so the next view reuses it even before reloading.
        self._run_order = order
        self._refresh_roster()
        self.statusBar().showMessage(
            f"Saved running order ({len(order)} participants) → {Path(path).name}. "
            "Load it (or just the same roster) for the next view.", 8000,
        )

    @_undoable
    def load_running_order(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Use saved running order", "", "Text/CSV (*.txt *.csv);;All files (*)"
        )
        if not path:
            return
        try:
            names: list[str] = []
            with open(path, "r", encoding="utf-8-sig") as fh:
                for line in fh:
                    s = line.strip().rstrip(",").strip()
                    if s:
                        names.append(s)
        except OSError as exc:
            QMessageBox.critical(self, "Use running order", f"Could not read the file:\n{exc}")
            return
        if names and names[0].lower() in RUN_ORDER_HEADERS:
            names = names[1:]
        if not names:
            QMessageBox.information(self, "Use running order", "No participant names found in that file.")
            return
        self._run_order = names
        self._refresh_roster()
        nxt = self._next_participant() or "—"
        self.statusBar().showMessage(
            f"Running order on ({len(names)} participants). Next up: {nxt}. "
            "Mark ↑/↓, then press Enter to add the next participant.", 9000,
        )

    # -------------------------------------------------------------- marking

    @_undoable
    def set_in(self):
        if not self._require_video():
            return
        self.in_point = self.player.position()
        if self.out_point is not None and self.out_point <= self.in_point:
            self.out_point = None
        self._update_marks_ui()
        self._maybe_autocommit()

    @_undoable
    def set_out(self):
        if not self._require_video():
            return
        self.out_point = self.player.position()
        self._update_marks_ui()
        self._maybe_autocommit()

    def _maybe_autocommit(self):
        """Auto-add the clip once In + Out + participant are all set (new clips only)."""
        if self.editing_row is not None:
            return
        if self.in_point is None or self.out_point is None:
            return
        if self.out_point <= self.in_point:
            return
        if not self.label_edit.text().strip():
            return
        self.add_or_update_clip()

    @_undoable
    def clear_marks(self):
        self.in_point = None
        self.out_point = None
        self.editing_row = None
        self.label_edit.clear()
        self.exact_check.setChecked(False)
        self.add_btn.setText("Add clip")
        self._update_marks_ui()
        self._focus_video()

    def _update_marks_ui(self):
        self.in_label.setText(f"In {timecode.format_timecode(self.in_point)}" if self.in_point is not None else "In —")
        self.out_label.setText(f"Out {timecode.format_timecode(self.out_point)}" if self.out_point is not None else "Out —")
        if self.in_point is not None and self.out_point is not None:
            length = self.out_point - self.in_point
            self.dur_label.setText(f"len {timecode.format_timecode(max(length, 0))}")
            self.dur_label.setStyleSheet("color: #c33;" if length <= 0 else "")
        else:
            self.dur_label.setText("len —")
            self.dur_label.setStyleSheet("")
        self._update_preview()

    def _combined_label(self, participant: str) -> str:
        """Participant with the search/event label appended: ``Participant - Search``.

        Kept as readable text (case + spaces); naming.build_filename does the
        filesystem-safety pass when the actual file is named.
        """
        part = (participant or "").strip()
        search = self.search_edit.text().strip()
        if search:
            return f"{part} - {search}"
        return part

    def _output_preview(self, participant: str) -> str:
        """Where a clip lands, as ``Folder/File.mp4``: the participant is the
        folder, the search/event label is the file inside it."""
        part = (participant or "").strip()
        search = self.search_edit.text().strip()
        folder = naming.sanitize_label(part) if part else ""
        fname = naming.build_filename(search or part or "clip")
        return f"{folder}/{fname}" if folder else fname

    def _update_preview(self):
        text = self.label_edit.text().strip()
        self.preview_label.setText("→ " + self._output_preview(text) if text else "→ —")
        self._update_next_up()

    @_undoable
    def add_or_update_clip(self):
        if not self._require_video():
            return
        if self.in_point is None or self.out_point is None:
            self.statusBar().showMessage("Set both In (↑) and Out (↓) before adding.", 4000)
            return
        name = self.label_edit.text().strip()
        if not name:
            self.statusBar().showMessage("Click a participant (or type a name) first.", 4000)
            self.label_edit.setFocus()
            return

        clip = clips_mod.Clip(
            start=self.in_point,
            end=self.out_point,
            label=self.label_edit.text(),
            exact=self.exact_check.isChecked(),
        )
        if self.out_point <= self.in_point:
            self.statusBar().showMessage(
                "Warning: Out is not after In — added anyway, fix it before export.", 5000
            )

        if self.editing_row is not None:
            clip.source_participant = self.clips[self.editing_row].source_participant
            self.clips[self.editing_row] = clip
            row_to_select = self.editing_row
            self.statusBar().showMessage(f"Updated clip {row_to_select + 1}.", 3000)
        else:
            if name in self._available:
                clip.source_participant = name
                self._use_participant(name)
            self.clips.append(clip)
            row_to_select = len(self.clips) - 1
            self.statusBar().showMessage(f"Added clip {row_to_select + 1}: {name}", 3000)

        self.editing_row = None
        self.add_btn.setText("Add clip")
        self.in_point = None
        self.out_point = None
        self.label_edit.clear()
        self.exact_check.setChecked(False)
        self._refresh_table()
        self._select_row(row_to_select)
        self._update_marks_ui()
        self._autosave_marking()   # a committed clip is saved at once, not on the timer
        self._focus_video()

    # ---------------------------------------------------------- clip table

    def _refresh_table(self):
        self.table.setRowCount(len(self.clips))
        for i, clip in enumerate(self.clips):
            length = clip.end - clip.start
            values = [
                str(i + 1),
                clip.start_tc(1),
                clip.end_tc(1),
                timecode.format_timecode(max(length, 0), 1),
                clip.label,
                "exact" if clip.exact else "",
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c != 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if length <= 0 or not clip.label.strip():
                    item.setForeground(Qt.GlobalColor.red)
                item.setToolTip(self._output_preview(clip.label))
                self.table.setItem(i, c, item)
        self.count_label.setText(f"{len(self.clips)} clip{'s' if len(self.clips) != 1 else ''}")
        self._update_preview()

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _select_row(self, row: int):
        if 0 <= row < len(self.clips):
            self.table.selectRow(row)

    @_undoable
    def edit_selected(self):
        row = self._selected_row()
        if row is None:
            self.statusBar().showMessage("Select a clip row to edit.", 3000)
            return
        clip = self.clips[row]
        self.in_point = clip.start
        self.out_point = clip.end
        self.label_edit.setText(clip.label)
        self.exact_check.setChecked(clip.exact)
        self.editing_row = row
        self.add_btn.setText("Update clip")
        self.player.seek(clip.start)
        self._update_marks_ui()
        self.statusBar().showMessage(
            f"Editing clip {row + 1} — scrub + ↑/↓ to re-mark, click a name to rename, "
            "then Enter (or Update) to save.", 6000,
        )
        # Focus the video (not the name field) so the marking/transport hotkeys
        # work immediately for a re-mark — clips almost always already have the
        # right name, so the common edit is nudging In/Out. To rename, click the
        # name field or a roster name.
        self._focus_video()

    @_undoable
    def delete_selected(self):
        row = self._selected_row()
        if row is None:
            return
        clip = self.clips[row]
        name = clip.label.strip() or "(blank)"
        del self.clips[row]
        if clip.source_participant:
            self._restore_participant(clip.source_participant)
        if self.editing_row == row:
            self.clear_marks()
        self._refresh_table()
        self._select_row(min(row, len(self.clips) - 1))
        self.statusBar().showMessage(f"Deleted clip: {name}", 3000)

    @_undoable
    def move_up(self):
        row = self._selected_row()
        if row is None or row == 0:
            return
        self.clips[row - 1], self.clips[row] = self.clips[row], self.clips[row - 1]
        self._refresh_table()
        self._select_row(row - 1)

    @_undoable
    def move_down(self):
        row = self._selected_row()
        if row is None or row >= len(self.clips) - 1:
            return
        self.clips[row + 1], self.clips[row] = self.clips[row], self.clips[row + 1]
        self._refresh_table()
        self._select_row(row + 1)

    @_undoable
    def clear_all(self):
        if not self.clips:
            return
        if QMessageBox.question(self, "Clear all", f"Remove all {len(self.clips)} clips?") == QMessageBox.StandardButton.Yes:
            for clip in self.clips:
                if clip.source_participant:
                    self._restore_participant(clip.source_participant)
            self.clips.clear()
            self.clear_marks()
            self._refresh_table()

    # ------------------------------------------------------------- undo

    def _state_signature(self):
        """A comparable summary of all undoable state — used to tell whether an
        action actually changed anything (so no-ops don't create undo steps)."""
        clips = tuple(
            (c.start, c.end, c.label, c.exact, c.source_participant) for c in self.clips
        )
        return (
            clips, self.in_point, self.out_point, self.editing_row,
            self.label_edit.text(), self.search_edit.text(), self.exact_check.isChecked(),
            tuple(self._available), tuple(self._roster_all), tuple(self._run_order),
        )

    def _snapshot(self) -> dict:
        """A deep-enough copy of the editable state to restore on undo."""
        return {
            "clips": [copy.copy(c) for c in self.clips],
            "in_point": self.in_point,
            "out_point": self.out_point,
            "editing_row": self.editing_row,
            "label": self.label_edit.text(),
            "search": self.search_edit.text(),
            "exact": self.exact_check.isChecked(),
            "available": list(self._available),
            "roster_all": list(self._roster_all),
            "run_order": list(self._run_order),
            "selected": self._selected_row(),
        }

    def _restore(self, snap: dict):
        self.clips = [copy.copy(c) for c in snap["clips"]]
        self.in_point = snap["in_point"]
        self.out_point = snap["out_point"]
        self.editing_row = snap["editing_row"]
        self._roster_all = list(snap["roster_all"])
        self._available = list(snap["available"])
        self._run_order = list(snap["run_order"])
        self.exact_check.setChecked(snap["exact"])
        self.search_edit.setText(snap["search"])
        self.label_edit.setText(snap["label"])
        self.add_btn.setText("Update clip" if self.editing_row is not None else "Add clip")
        self._refresh_table()
        self._refresh_roster()
        self._update_marks_ui()
        if snap["selected"] is not None:
            self._select_row(snap["selected"])
        self._focus_video()

    def undo(self):
        if not self._undo_stack:
            self.statusBar().showMessage("Nothing to undo.", 2000)
            return
        self._restore(self._undo_stack.pop())
        self._update_undo_ui()
        self.statusBar().showMessage("Undo.", 1500)

    def _update_undo_ui(self):
        self.undo_btn.setEnabled(bool(self._undo_stack))

    # -------------------------------------------------------- validation

    def _validation_summary(self) -> tuple[bool, str]:
        issues = clips_mod.validate(self.clips)
        if not issues:
            return True, "All clips look good."
        lines = []
        for issue in issues:
            tag = "ERROR" if issue.is_error else "warn "
            lbl = self.clips[issue.index].label.strip() or "(blank)"
            lines.append(f"[{tag}] row {issue.index + 1} {lbl}: {issue.message}")
        return not clips_mod.has_errors(issues), "\n".join(lines)

    def validate_dialog(self):
        if not self.clips:
            QMessageBox.information(self, "Validate", "No clips marked yet.")
            return
        ok, summary = self._validation_summary()
        box = QMessageBox(self)
        box.setWindowTitle("Validation")
        box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning)
        box.setText("No problems found." if ok and summary.startswith("All") else
                    ("Warnings only — safe to export." if ok else "Errors found — fix before exporting."))
        box.setDetailedText(summary)
        box.exec()

    # ------------------------------------------------------------- export

    def _effective_clips(self) -> list[clips_mod.Clip]:
        """Copies of the clip list with the search/event label folded into each
        label (``Participant - Search``) -- what actually gets written/cut. Each
        carries its participant (``Handler Dog``) so the cutter can group the
        clips into a folder per participant."""
        out: list[clips_mod.Clip] = []
        for c in self.clips:
            eff = clips_mod.Clip(c.start, c.end, self._combined_label(c.label), c.exact)
            eff.source_participant = c.label.strip() or None
            out.append(eff)
        return out

    def export_csv(self):
        if not self._require_clips():
            return
        ok, summary = self._validation_summary()
        if not ok and not self._confirm_problems(summary, "export the CSV anyway"):
            return
        default = str(Path(self.video_path).with_name("clips.csv")) if self.video_path else "clips.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export clip list", default, "CSV files (*.csv)")
        if not path:
            return
        try:
            clips_mod.write_csv(path, self._effective_clips())
        except OSError as exc:
            QMessageBox.critical(self, "Export CSV", f"Could not write CSV:\n{exc}")
            return
        self.statusBar().showMessage(f"Wrote {path}", 6000)

    @_undoable
    def load_csv_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load clip list", "", "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            rows = clips_mod.read_csv(path)
        except (ValueError, OSError) as exc:
            # A common mix-up: this is actually a participant roster. Offer to
            # load it the right way instead of just erroring out.
            if not _is_clip_csv(path):
                names = []
                try:
                    names = roster.load_participants(path)
                except Exception:
                    names = []
                if names and QMessageBox.question(
                    self, "Load CSV",
                    f"That file looks like a participant roster ({len(names)} names), "
                    "not a clip list.\n\nLoad it into the Participants panel instead?",
                ) == QMessageBox.StandardButton.Yes:
                    self._load_roster_path(path)
                    return
            QMessageBox.critical(self, "Load CSV", f"Could not read CSV:\n{exc}")
            return
        if self.clips and QMessageBox.question(
            self, "Load CSV", f"Replace the current {len(self.clips)} clips with {len(rows)} from the file?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._absorb_loaded_search(rows)
        self.clips = rows
        self.clear_marks()
        self._refresh_table()
        self.statusBar().showMessage(f"Loaded {len(rows)} clips from {Path(path).name}", 5000)

    def _absorb_loaded_search(self, rows) -> str:
        """Split each ``Participant - Search`` label back into the participant
        (the clip's label) and the search/event label (the Search box), so a
        reloaded clip list is editable and re-exports to the right participant
        folder. Uses the most common search across the rows. Returns it."""
        searches: list[str] = []
        for c in rows:
            if " - " in c.label:
                participant, search = c.label.split(" - ", 1)
                c.label = participant.strip()
                c.source_participant = c.label or None
                searches.append(search.strip())
        if searches:
            common = max(set(searches), key=searches.count)
            self.search_edit.setText(common)
            return common
        return ""

    def _autosave_clip_list(self, out_dir: str, rows) -> "Path | None":
        """Save the clip list as a CSV next to the exported videos, so it can be
        reloaded later (Load clip CSV…) to tweak a clip without re-marking.
        Named for the search label; best-effort — never fails the export."""
        search = self.search_edit.text().strip()
        stem = naming.sanitize_label(f"{search} clips" if search else "clips")
        path = Path(out_dir) / f"{stem}.csv"
        try:
            clips_mod.write_csv(path, rows)
            return path
        except OSError:
            return None

    def export_clips(self):
        if not self._require_clips():
            return
        if not self.video_path:
            QMessageBox.warning(self, "Export clips", "Load the source video first.")
            return
        try:
            self._ffmpeg = self._ffmpeg or find_ffmpeg()
        except FFmpegNotFound as exc:
            QMessageBox.critical(self, "Export clips", str(exc))
            return
        ok, summary = self._validation_summary()
        if not ok and not self._confirm_problems(summary, "cut the valid rows (invalid rows are skipped)"):
            return

        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose output folder for clips", str(Path(self.video_path).parent),
        )
        if not out_dir:
            return
        folder_per_participant = QMessageBox.question(
            self, "Output layout",
            "Group the clips into a folder per participant?\n\n"
            "Yes = one folder each (“Sara & Tracer/”), the file named for the search "
            "label (“Interior Search 1.mp4”) — recommended\n"
            "No = all clips in one flat folder (“Sara & Tracer - Interior Search 1.mp4”)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes

        intro, ok = self._resolve_card("intro")
        if not ok:
            return
        outro, ok = self._resolve_card("outro")
        if not ok:
            return

        video_mode = self.format_combo.currentData()
        bitrate = int(self.bitrate_spin.value() * 1000) if video_mode == "hevc" else 0

        rows = self._effective_clips()
        # Save the clip list beside the videos so it can be reloaded to fix a clip.
        csv_path = self._autosave_clip_list(out_dir, rows)

        # Queue a job that captures its OWN source video + clip snapshot + settings,
        # so she can immediately load and mark the next view while this one cuts.
        job = {
            "video": self.video_path, "rows": rows, "out_dir": out_dir,
            "folder_per_participant": folder_per_participant,
            "intro": intro, "outro": outro, "video_mode": video_mode, "bitrate": bitrate,
            "label": self.search_edit.text().strip() or Path(out_dir).name or "clips",
            "csv": csv_path,
        }
        self._export_queue.append(job)
        pending = len(self._export_queue) + (1 if self._export_worker else 0)
        self.statusBar().showMessage(
            f"Queued “{job['label']}” for export ({pending} in the queue) — "
            "window cleared for your next view.", 8000,
        )
        self._pump()                # start it now if nothing is running
        self._reset_for_next()      # blank the window for the next camera view

    def _pump(self):
        """Run one background job at a time. Exports (interactive) go first, then
        queued joins; when everything drains, show the summaries. Called whenever a
        job is queued or one finishes -- so joins and exports never fight."""
        if self._export_worker is not None or self._join_worker is not None:
            return
        if self._export_queue:
            self._start_next_export()
        elif self._join_queue:
            self._start_next_join()
        else:
            self._flush_summaries()
        self._persist_queues()

    def _persist_queues(self):
        """Snapshot unfinished jobs (the running one + everything queued) to disk so
        a crash or accidental quit can resume the batch on next launch."""
        jobs = []
        if self._export_worker is not None and self._current_job is not None:
            jobs.append(self._current_job)
        if self._join_worker is not None and self._current_join is not None:
            jobs.append(self._current_join)
        jobs.extend(self._export_queue)
        jobs.extend(self._join_queue)
        session.save_jobs(jobs)

    def _start_next_export(self):
        job = self._current_job = self._export_queue.pop(0)
        total = len(job["rows"])
        log.info("export start: '%s' (%d clips, %s) -> %s",
                 job["label"], total, job["video_mode"], job["out_dir"])
        self.export_bar.setRange(0, total)
        self.export_bar.setValue(0)
        self.export_cancel_btn.setEnabled(True)
        self.export_progress_row.setVisible(True)
        self._update_export_status(0, total)
        self._export_worker = ExportWorker(
            self._ffmpeg, job["video"], job["rows"], job["out_dir"], job["folder_per_participant"],
            intro=job["intro"], outro=job["outro"], video_mode=job["video_mode"], bitrate=job["bitrate"],
        )
        self._export_worker.rowDone.connect(self._on_export_row)
        self._export_worker.finishedResult.connect(lambda res, j=job: self._on_export_done(res, j))
        self._export_worker.start()

    def _update_export_status(self, done: int, total: int):
        label = self._current_job["label"] if self._current_job else ""
        waiting = len(self._export_queue) + len(self._join_queue)
        tail = f"   ·   {waiting} more queued" if waiting else ""
        self.export_status.setText(f"Exporting “{label}”  {done}/{total}{tail}")

    def _cancel_background(self):
        """Cancel the running job (export or join) and drop everything queued."""
        dropped = len(self._export_queue) + len(self._join_queue)
        self._export_queue.clear()
        self._join_queue.clear()
        for worker in (self._export_worker, self._join_worker):
            if worker:
                worker.requestInterruption()
        self.export_cancel_btn.setEnabled(False)
        self.export_status.setText("Cancelling…")
        if dropped:
            self.statusBar().showMessage(f"Cancelled — dropped {dropped} queued job(s).", 5000)

    def _on_export_row(self, rownum: int, total: int, outcome):
        self.export_bar.setRange(0, total)
        self.export_bar.setValue(rownum)
        self._update_export_status(rownum, total)

    def _on_export_done(self, result, job):
        self._export_worker = None
        self._export_results.append((job, result))
        written = len(result.written)
        log.info("export done: '%s' %d/%d clips", job["label"], written, result.total)
        self.statusBar().showMessage(f"✓ “{job['label']}”: {written}/{result.total} clips exported", 8000)
        self._pump()                # next job, or the batch summary if the queue is empty

    def _show_export_summary(self, results):
        total_clips = sum(len(r.written) for _, r in results)
        any_problems = any(r.problems for _, r in results)
        lines = []
        for job, r in results:
            mark = "" if not r.problems else f"   ({len(r.problems)} skipped/failed)"
            lines.append(f"• {job['label']}: {len(r.written)}/{r.total} → {Path(job['out_dir']).name}{mark}")
        last_dir = results[-1][0]["out_dir"] if results else None
        box = QMessageBox(self)
        box.setWindowTitle("Exports complete")
        box.setIcon(QMessageBox.Icon.Warning if any_problems else QMessageBox.Icon.Information)
        plural = "export" if len(results) == 1 else "exports"
        box.setText(f"Finished {len(results)} {plural}, {total_clips} clips total:\n\n" + "\n".join(lines))
        if any_problems:
            details = [f"[{job['label']}] row {o.rownum} {o.label}: {o.reason}"
                       for job, r in results for o in r.problems]
            box.setDetailedText("\n".join(details))
        open_btn = box.addButton("Open last folder", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.setModal(False)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        box.buttonClicked.connect(
            lambda b: QDesktopServices.openUrl(QUrl.fromLocalFile(last_dir)) if (b is open_btn and last_dir) else None
        )
        self._export_done_box = box
        box.show()
        self.activateWindow()
        self._focus_video()

    # ----------------------------------------------------- join queue

    def _start_next_join(self):
        job = self._current_join = self._join_queue.pop(0)
        log.info("join start: '%s' (%s) -> %s", job["label"],
                 "encode" if job.get("bitrate") else "copy", job["out_path"])
        self.export_bar.setRange(0, 0)      # busy until progress arrives
        self.export_cancel_btn.setEnabled(True)
        self.export_progress_row.setVisible(True)
        self._update_join_status(0.0, job.get("total"))
        self._join_worker = JoinWorker(
            self._ffmpeg, job["inputs"], job["out_path"], job.get("total"),
            trim_black=job["trim_black"], encoder=job["encoder"],
            bitrate=job["bitrate"], gop=job["gop"],
        )
        self._join_worker.progress.connect(self._on_join_progress)
        self._join_worker.analyzing.connect(self._on_join_analyzing)
        self._join_worker.finishedResult.connect(lambda res, j=job: self._on_join_done(res, j))
        self._join_worker.start()

    def _update_join_status(self, secs, total):
        label = self._current_join["label"] if self._current_join else ""
        waiting = len(self._join_queue) + len(self._export_queue)
        tail = f"   ·   {waiting} more queued" if waiting else ""
        verb = "Encoding" if (self._current_join and self._current_join.get("bitrate")) else "Joining"
        if total:
            self.export_status.setText(
                f"{verb} “{label}”  {timecode.format_timecode(secs)} / "
                f"{timecode.format_timecode(total)}{tail}")
        else:
            self.export_status.setText(f"{verb} “{label}”  {timecode.format_timecode(secs)}{tail}")

    def _on_join_analyzing(self, index: int, count: int, name: str):
        self.export_bar.setRange(0, 0)
        self.export_status.setText(f"Checking for black frames ({index}/{count}): {name}")

    def _on_join_progress(self, seconds, total):
        if total:
            self.export_bar.setRange(0, 1000)
            self.export_bar.setValue(int(min(seconds / total, 1.0) * 1000))
        else:
            self.export_bar.setRange(0, 0)
        self._update_join_status(seconds, total)

    def _on_join_done(self, result, job):
        self._join_worker = None
        self._join_results.append((job, result))
        if result.cancelled:
            log.info("join cancelled: '%s'", job["label"])
            self.statusBar().showMessage(f"Join “{job['label']}” cancelled.", 5000)
        elif result.ok:
            log.info("join done: '%s' -> %s", job["label"], Path(result.output).name)
            self.statusBar().showMessage(f"✓ Joined “{job['label']}” → {Path(result.output).name}", 8000)
        else:
            tail = result.stderr.splitlines()[-1] if result.stderr else f"ffmpeg exit {result.returncode}"
            log.error("join FAILED: '%s': %s", job["label"], tail)
            self.statusBar().showMessage(f"Join “{job['label']}” failed: {tail}", 9000)
        self._pump()

    def _show_join_summary(self, results):
        lines, last_dir = [], None
        for job, r in results:
            if r.cancelled:
                lines.append(f"• {job['label']}: cancelled")
            elif r.ok:
                last_dir = str(Path(r.output).parent)
                lines.append(f"• {job['label']} → {Path(r.output).name}")
            else:
                tail = r.stderr.splitlines()[-1] if r.stderr else f"exit {r.returncode}"
                lines.append(f"• {job['label']}: FAILED — {tail}")
        any_fail = any((not r.ok and not r.cancelled) for _, r in results)
        box = QMessageBox(self)
        box.setWindowTitle("Joins complete")
        box.setIcon(QMessageBox.Icon.Warning if any_fail else QMessageBox.Icon.Information)
        plural = "recording" if len(results) == 1 else "recordings"
        box.setText(f"Finished {len(results)} {plural}:\n\n" + "\n".join(lines) +
                    "\n\nOpen one with Open video… to start marking — clips will export instantly.")
        open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole) if last_dir else None
        box.addButton(QMessageBox.StandardButton.Close)
        box.setModal(False)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        if open_btn is not None:
            box.buttonClicked.connect(
                lambda b: QDesktopServices.openUrl(QUrl.fromLocalFile(last_dir)) if b is open_btn else None)
        self._join_done_box = box
        box.show()
        self.activateWindow()
        self._focus_video()

    def _flush_summaries(self):
        self.export_progress_row.setVisible(False)
        self._current_job = None
        self._current_join = None
        if self._export_results:
            self._show_export_summary(self._export_results)
            self._export_results = []
        if self._join_results:
            self._show_join_summary(self._join_results)
            self._join_results = []

    def _reset_for_next(self):
        """Blank the marking state after an export is queued, so she can load and
        mark the next camera view. The job kept its own snapshot (and the clip list
        was auto-saved to CSV), so nothing is lost. The source video stays loaded;
        the running order (if any) is kept for reuse."""
        self.clips = []
        self.in_point = self.out_point = None
        self.editing_row = None
        self.exact_check.setChecked(False)
        self.label_edit.clear()
        self.search_edit.clear()
        self.add_btn.setText("Add clip")
        self._available = list(self._roster_all)   # same participants run again next view
        self._undo_stack.clear()
        self._update_undo_ui()
        self._refresh_table()
        self._refresh_roster()
        self._update_marks_ui()
        self._autosave_marking()   # board cleared after export -> drop the recovery file
        self._focus_video()

    def _confirm_problems(self, summary: str, action: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Validation problems")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"There are validation problems. Do you want to {action}?")
        box.setDetailedText(summary)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    # --------------------------------------------------------------- misc

    def _require_video(self) -> bool:
        if not self.video_path:
            self.statusBar().showMessage("Open a video first.", 3000)
            return False
        return True

    def _require_clips(self) -> bool:
        if not self.clips:
            QMessageBox.information(self, "Nothing to export", "Mark at least one clip first.")
            return False
        return True

    def _focus_video(self):
        self.video_area.setFocus()

    # ------------------------------------------------------------- scrub

    def _slider_moved(self, value: int):
        self._scrubbing = True
        self.player.seek(value / 1000.0)
        self._update_time_label()

    def _slider_released(self):
        self._scrubbing = False
        self.player.seek(self.slider.value() / 1000.0)

    def _set_speed(self, rate: float):
        if rate in SPEEDS:
            self.speed_combo.setCurrentIndex(SPEEDS.index(rate))
        self.player.set_rate(rate)

    # --------------------------------------------------------- hotkeys

    # Letter hotkeys that should work even when a list/table/button holds focus
    # (they have no navigation meaning there, and those widgets would otherwise
    # swallow them for type-ahead). When a text field is focused we leave them
    # alone so names like "Lincoln Otter" type normally.
    #
    # In/Out are primarily ↑/↓, but those are deliberately NOT in this set: over a
    # list/table the arrows must still navigate rows, so forwarding them would
    # hijack that. I/O stay here as the always-available marking fallback (incl.
    # right after a roster click, when the list briefly holds focus).
    _LETTER_HOTKEYS = frozenset(
        {Qt.Key.Key_I, Qt.Key.Key_O, Qt.Key.Key_J, Qt.Key.Key_K, Qt.Key.Key_L}
    )

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in self._LETTER_HOTKEYS:
            if not isinstance(QApplication.focusWidget(), QLineEdit):
                self.keyPressEvent(event)
                if event.isAccepted():
                    return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if key == Qt.Key.Key_Space:
            self.player.toggle_play()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = -1.0 if key == Qt.Key.Key_Left else 1.0
            # Hold to scrub faster: each auto-repeat while the key is held grows
            # the step, so a tap nudges and a hold accelerates. A fresh press
            # (not an auto-repeat) resets to the small tap step. Shift = fixed 10s.
            if event.isAutoRepeat():
                self._arrow_held += 1
            else:
                self._arrow_held = 0
            if shift:
                step = ARROW_STEP_SHIFT
            else:
                step = min(ARROW_STEP + self._arrow_held * ARROW_STEP_ACCEL, ARROW_STEP_MAX)
            self.player.step_seconds(direction * step)
        elif key == Qt.Key.Key_Comma:
            self.player.step_frames(-1)
        elif key == Qt.Key.Key_Period:
            self.player.step_frames(1)
        elif key == Qt.Key.Key_Home:
            self.player.seek(0)
        elif key == Qt.Key.Key_End:
            self.player.seek(self.player.duration())
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_I):
            self.set_in()
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_O):
            self.set_out()
        elif key == Qt.Key.Key_J:
            self._shuttle(-1)
        elif key == Qt.Key.Key_K:
            self.player.pause()
        elif key == Qt.Key.Key_L:
            self._shuttle(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_enter()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _shuttle(self, direction: int):
        current = self.speed_combo.itemData(self.speed_combo.currentIndex())
        try:
            idx = SHUTTLE.index(current)
        except ValueError:
            idx = 0
        idx = max(0, min(idx + direction, len(SHUTTLE) - 1))
        if direction < 0 and idx == 0:
            self.player.pause()
            self._set_speed(1.0)
            return
        self._set_speed(SHUTTLE[idx])
        self.player.play()


def _prepare_bundled_ffmpeg():
    """When running as a frozen (PyInstaller) app, make the bundled
    imageio-ffmpeg binary discoverable and executable, so cutting/joining works
    on a Mac with nothing installed. We set IMAGEIO_FFMPEG_EXE (not
    CLIPPER_FFMPEG) so a real system ffmpeg on PATH is still preferred, matching
    find_ffmpeg's documented resolution order."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return
    if exe and os.path.exists(exe):
        try:
            os.chmod(exe, 0o755)  # data files lose the exec bit in the bundle
        except OSError:
            pass
        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", exe)


def _selftest(argv) -> int:
    """Headless bundle check used by the macOS packaging workflow: build the
    window (proves all Qt/PySide6 modules and plugins are bundled), optionally
    load a video, and exit 0. Decode is reported but not required, since CI runs
    headless. Any missing-dependency error raises and fails the step."""
    import time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    video = None
    seen_flag = False
    for a in argv[1:]:
        if a == "--selftest":
            seen_flag = True
        elif seen_flag and not a.startswith("-"):
            video = a
            break
    app = QApplication([argv[0]])
    win = MarkerWindow(video)
    win.show()
    duration = 0.0
    if video:
        deadline = time.time() + 20
        while time.time() < deadline and win.player.duration() <= 0:
            app.processEvents()
            time.sleep(0.05)
        duration = win.player.duration()
    print(f"selftest OK: window built; video={video!r}; duration={duration:.3f}s")
    win.close()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _prepare_bundled_ffmpeg()
    if "--selftest" in argv:
        return _selftest(argv)
    logfile = applog.setup_logging(APP_VERSION)
    applog.install_qt_message_handler()
    initial = None
    for a in argv[1:]:
        if not a.startswith("-") and Path(a).exists():
            initial = a
            break
    app = QApplication(argv)
    win = MarkerWindow(initial)
    applog.install_excepthook(win._on_unhandled)
    log.info("UI ready (log: %s)", logfile)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
