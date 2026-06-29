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

from PySide6.QtCore import QPoint, QRect, Qt, QObject, QUrl, Signal
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
        self.setMinimumHeight(360)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor("black"))
        self.setPalette(pal)

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
            fitted = self._image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
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
        self._prime = False     # show the first frame on load without playing

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
        self._canvas.clear()
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
        # Seeking emits a fresh frame to the sink even while paused, so the
        # canvas updates as the user scrubs.
        self._player.setPosition(int(round(seconds * 1000)))

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
                # Paint immediately when not actively playing, so scrubbing/
                # stepping shows each frame live instead of a stale one.
                self._canvas.set_image(image, immediate=not self._user_playing)
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
            self.loaded.emit(self._fps)
            # Prime the first frame so the picture isn't black before Play.
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
