"""Swappable video-player backend for the marking tool.

The marking UI talks ONLY to the :class:`VideoPlayer` interface below -- it
never touches Qt Multimedia (or any other engine) directly. That keeps the
flagged technical risk (smooth 4K scrubbing) contained to one file: if Qt
Multimedia's playback isn't smooth enough on the real hardware, drop in an
mpv-backed ``MpvVideoPlayer`` implementing the same interface and the rest of
the app is unchanged.

Why we render frames ourselves
------------------------------
Qt's ``QVideoWidget`` renders through the RHI (Direct3D on Windows) and on a
number of Windows GPU/driver combos it shows a black/blank picture even though
decoding is fine. To be robust we instead pull decoded frames from a
``QVideoSink`` and paint them onto a plain widget -- if a frame decodes, it
shows. This also lets us "prime" the first frame on load so the picture isn't
black until the user hits Play.

Interface contract
------------------
A backend owns a render widget (``.widget``) to embed in the window, exposes
position/duration/fps/rate, and emits these signals **in seconds** so the UI is
unit- and engine-agnostic:

    positionChanged(float seconds)
    durationChanged(float seconds)
    playingChanged(bool)
    loaded(float fps)          # fired once media metadata is known
    errorOccurred(str)
"""

from __future__ import annotations

from abc import abstractmethod

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

DEFAULT_FPS = 30.0


class VideoPlayer(QObject):
    """Abstract player. The UI depends on this, not on any concrete engine."""

    positionChanged = Signal(float)   # seconds
    durationChanged = Signal(float)   # seconds
    playingChanged = Signal(bool)
    loaded = Signal(float)            # fps
    errorOccurred = Signal(str)

    @property
    @abstractmethod
    def widget(self) -> QWidget: ...

    @abstractmethod
    def load(self, path: str) -> None: ...

    @abstractmethod
    def play(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def is_playing(self) -> bool: ...

    @abstractmethod
    def position(self) -> float:
        """Current playhead in seconds."""

    @abstractmethod
    def duration(self) -> float:
        """Total length in seconds (0 until known)."""

    @abstractmethod
    def seek(self, seconds: float) -> None: ...

    @abstractmethod
    def set_rate(self, rate: float) -> None: ...

    @abstractmethod
    def fps(self) -> float: ...

    # -- convenience built on the primitives above --------------------------

    def toggle_play(self) -> None:
        self.pause() if self.is_playing() else self.play()

    def step_frames(self, n: int) -> None:
        """Pause and nudge the playhead by ``n`` frames (negative = back)."""
        self.pause()
        frame = 1.0 / max(self.fps(), 1.0)
        target = self.position() + n * frame
        self.seek(max(0.0, min(target, self.duration() or target)))

    def step_seconds(self, seconds: float) -> None:
        self.pause()
        target = self.position() + seconds
        self.seek(max(0.0, min(target, self.duration() or target)))


class VideoCanvas(QWidget):
    """Plain widget that paints the latest decoded video frame, letterboxed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        # Locked display aspect ratio (the video's native size). The letterbox is
        # computed from this, NOT from each frame's pixel size -- so a backend that
        # delivers frames at a jittering coded size during seeking (e.g. Qt's
        # FFmpeg HEVC decoder emitting 3840x2176 vs the real 3840x2160) can't make
        # the picture wobble/flicker. None until the media's resolution is known.
        self._aspect: QSize | None = None
        self.setMinimumHeight(360)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor("black"))
        self.setPalette(pal)

    def aspect(self) -> QSize | None:
        return self._aspect

    def set_aspect(self, size: QSize | None) -> None:
        """Lock the letterbox to the video's native aspect ratio. Pass None to
        clear it (e.g. when loading a new file)."""
        if size is not None and size.isValid() and size.width() > 0 and size.height() > 0:
            self._aspect = QSize(size.width(), size.height())
        else:
            self._aspect = None
        self.update()

    def set_image(self, image: QImage, immediate: bool = False) -> None:
        self._image = image
        # While scrubbing/paused we repaint synchronously so the frame is shown
        # *now* (a deferred update() can be coalesced/starved during a burst of
        # arrow-key seeks, leaving a stale picture). During playback the cheap
        # coalesced update() keeps things smooth.
        if immediate:
            self.repaint()
        else:
            self.update()

    def clear(self) -> None:
        self._image = None
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._image is not None and not self._image.isNull():
            # Fit using the locked native aspect when known, so the letterbox
            # stays put even if a stray frame arrives at a different pixel size;
            # the frame is then drawn into that stable rect (slight scale at most,
            # never a flicker). Fall back to the frame's own size before the
            # resolution is known.
            source = self._aspect if (self._aspect is not None and self._aspect.isValid()) else self._image.size()
            fitted = source.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x = (self.width() - fitted.width()) // 2
            y = (self.height() - fitted.height()) // 2
            painter.drawImage(QRect(QPoint(x, y), fitted), self._image)


class QtVideoPlayer(VideoPlayer):
    """Default backend: Qt Multimedia decode + our own frame painting.

    Qt works in integer milliseconds; this wrapper converts to/from seconds so
    the rest of the app stays in floating-point seconds like the cutter.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Imported lazily so importing this module doesn't hard-require the
        # multimedia plugin until a player is actually constructed.
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        self._QMediaPlayer = QMediaPlayer
        self._canvas = VideoCanvas()
        self._sink = QVideoSink()
        self._audio = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setVideoOutput(self._sink)
        self._player.setAudioOutput(self._audio)
        self._fps = DEFAULT_FPS
        self._rate = 1.0
        self._user_playing = False
        self._prime = False       # a prime cycle is currently in progress
        self._has_primed = False  # priming happens once per file, not per buffer event

        # Scrub coalescer: while dragging, a burst of seek() calls collapses into a
        # SINGLE in-flight seek that always chases the LATEST target, so heavy
        # HEVC/4K decodes never back up (what made scrubbing choppy vs. an NLE).
        self._seek_pending_ms: int | None = None
        self._seek_inflight = False
        self._seek_watchdog = QTimer(self)
        self._seek_watchdog.setSingleShot(True)
        self._seek_watchdog.setInterval(150)
        self._seek_watchdog.timeout.connect(self._on_seek_watchdog)

        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)

    # -- VideoPlayer interface ---------------------------------------------

    @property
    def widget(self) -> QWidget:
        return self._canvas

    def load(self, path: str) -> None:
        self._fps = DEFAULT_FPS
        self._user_playing = False
        self._prime = False
        self._has_primed = False
        self._canvas.clear()
        self._canvas.set_aspect(None)  # re-locked from the new file's resolution
        self._player.setSource(QUrl.fromLocalFile(path))

    def play(self) -> None:
        self._user_playing = True
        self._prime = False
        self._player.setPlaybackRate(self._rate)
        self._player.play()

    def pause(self) -> None:
        self._user_playing = False
        self._player.pause()

    def is_playing(self) -> bool:
        return self._player.playbackState() == self._QMediaPlayer.PlaybackState.PlayingState

    def position(self) -> float:
        return self._player.position() / 1000.0

    def duration(self) -> float:
        return max(self._player.duration(), 0) / 1000.0

    def seek(self, seconds: float) -> None:
        # Coalesce: record the newest target and, if no seek is in flight, issue it
        # now. A flurry of drag positions collapses to the latest one -- seeking
        # emits a fresh frame even while paused, so the canvas follows the cursor
        # smoothly instead of the decoder queueing (and lagging behind) every move.
        self._seek_pending_ms = max(0, int(round(seconds * 1000)))
        if not self._seek_inflight:
            self._issue_seek()

    def _issue_seek(self) -> None:
        if self._seek_pending_ms is None:
            self._seek_inflight = False
            self._seek_watchdog.stop()
            return
        target = self._seek_pending_ms
        self._seek_pending_ms = None
        self._seek_inflight = True
        self._player.setPosition(target)
        self._seek_watchdog.start()   # release if this seek yields no new frame

    def _settle_seek_after_frame(self) -> None:
        """A frame landed, so the in-flight seek is done: chase the newest target
        if the user kept dragging, otherwise go idle."""
        if not self._seek_inflight:
            return
        self._seek_watchdog.stop()
        if self._seek_pending_ms is not None:
            self._issue_seek()
        else:
            self._seek_inflight = False

    def _on_seek_watchdog(self) -> None:
        # The seek produced no visible frame within the timeout (e.g. a sub-frame
        # move); don't stall -- continue to the newest target or go idle.
        if self._seek_pending_ms is not None:
            self._issue_seek()
        else:
            self._seek_inflight = False

    def set_rate(self, rate: float) -> None:
        self._rate = rate
        if self.is_playing():
            self._player.setPlaybackRate(rate)

    def fps(self) -> float:
        return self._fps

    # -- Qt signal plumbing -------------------------------------------------

    def _on_frame(self, frame) -> None:
        if frame is not None and frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                # Lock the display aspect from the first good frame if metadata
                # hasn't provided the resolution yet (keeps the picture stable
                # even when Resolution metadata is missing).
                if self._canvas.aspect() is None:
                    self._canvas.set_aspect(image.size())
                # Paint immediately when not actively playing, so scrubbing/
                # stepping shows each frame live instead of a stale one.
                self._canvas.set_image(image, immediate=not self._user_playing)
        # A new frame means any in-flight scrub seek has landed -- immediately
        # chase the latest drag position so the picture keeps up with the cursor.
        self._settle_seek_after_frame()
        # Priming: we briefly played to decode frame 0; now that it's shown,
        # pause again so we sit on the first frame.
        if self._prime and not self._user_playing:
            self._prime = False
            self._player.pause()

    def _on_position(self, ms: int) -> None:
        self.positionChanged.emit(ms / 1000.0)

    def _on_duration(self, ms: int) -> None:
        self.durationChanged.emit(max(ms, 0) / 1000.0)

    def _on_state(self, _state) -> None:
        self.playingChanged.emit(self.is_playing())

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaMetaData, QMediaPlayer

        loaded_states = (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        )
        if status in loaded_states:
            meta = self._player.metaData()
            rate = meta.value(QMediaMetaData.Key.VideoFrameRate)
            try:
                rate = float(rate)
            except (TypeError, ValueError):
                rate = 0.0
            self._fps = rate if rate and rate > 0 else DEFAULT_FPS
            # Lock the letterbox to the native display resolution so per-frame
            # coded-size jitter during seeking can't change the picture's shape.
            # (Read every time so it's picked up whenever it becomes available.)
            resolution = meta.value(QMediaMetaData.Key.Resolution)
            if isinstance(resolution, QSize) and resolution.isValid():
                self._canvas.set_aspect(resolution)
            # Announce load + prime the first frame ONCE per file. This status can
            # re-fire (e.g. BufferedMedia after a seek); priming again would
            # briefly replay/advance the picture and toggle the play state, which
            # on macOS shows up as the picture and UI jittering while scrubbing.
            if not self._has_primed:
                self._has_primed = True
                self.loaded.emit(self._fps)
                if not self._user_playing:
                    self._prime = True
                    self._player.play()

    def _on_error(self, _error, message: str = "") -> None:
        self.errorOccurred.emit(message or "playback error")


def create_player(backend: str = "qt", parent: QObject | None = None) -> VideoPlayer:
    """Factory so the app picks a backend by name (future: ``"mpv"``)."""
    if backend == "qt":
        return QtVideoPlayer(parent)
    raise ValueError(f"unknown player backend: {backend!r}")
