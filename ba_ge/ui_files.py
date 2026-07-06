"""Qt file-transcription window — progress, then transcript + Copy/Save."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from .filejob import AUDIO_EXTENSIONS, FileJobError, default_txt_path, transcribe_file
from .notify import notify

log = logging.getLogger("bage.ui.files")

_open_windows: list = []  # keep references so windows aren't garbage-collected


def choose_and_transcribe(root=None, config=None) -> None:
    """Pick an audio file and open a transcript window. Call on the Qt main thread."""
    patterns = " ".join("*" + e for e in AUDIO_EXTENSIONS)
    path, _ = QFileDialog.getOpenFileName(
        None, "Choose an audio file to transcribe", "",
        f"Audio files ({patterns});;All files (*)")
    if path:
        win = TranscribeWindow(config, path)
        _open_windows.append(win)
        win.show()


class TranscribeWindow(QWidget):
    _ok = Signal(str, object)
    _failed = Signal(str)
    _progress = Signal(str)

    def __init__(self, config, path):
        super().__init__()
        self.config = config
        self.path = path
        self.text = ""
        self.setWindowTitle(f"Transcript — {Path(path).name}")
        self.resize(700, 520)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 18, 18, 18)
        self._build_progress()

        self._ok.connect(self._build_result)
        self._failed.connect(self._build_error)
        self._progress.connect(self._set_status)
        threading.Thread(target=self._work, daemon=True).start()

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _build_progress(self) -> None:
        self._clear()
        self._layout.addWidget(QLabel(f"Transcribing {Path(self.path).name}…"))
        bar = QProgressBar()
        bar.setRange(0, 0)  # indeterminate
        self._layout.addWidget(bar)
        self._status = QLabel("")
        self._status.setStyleSheet("color:#8a8a8a")
        self._layout.addWidget(self._status)
        self._layout.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        crow = QHBoxLayout()
        crow.addStretch(1)
        crow.addWidget(cancel)
        self._layout.addLayout(crow)

    def _set_status(self, message: str) -> None:
        if getattr(self, "_status", None) is not None:
            try:
                self._status.setText(message)
            except RuntimeError:
                pass

    def _work(self) -> None:
        try:
            text, payload = transcribe_file(
                self.path, self.config,
                progress=lambda m: self._progress.emit(m))
        except FileJobError as exc:
            self._failed.emit(str(exc))
            return
        except Exception as exc:  # pragma: no cover
            log.exception("file transcribe failed")
            self._failed.emit(f"Unexpected error: {exc}")
            return
        self._ok.emit(text, payload)

    def _build_error(self, message: str) -> None:
        self._clear()
        title = QLabel("Transcription failed")
        title.setStyleSheet("font-weight:bold")
        self._layout.addWidget(title)
        body = QLabel(message)
        body.setWordWrap(True)
        body.setStyleSheet("color:#e05555")
        self._layout.addWidget(body)
        self._layout.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        self._layout.addLayout(row)
        notify("Ba-Ge", f"Transcription failed: {message}", urgency="critical")

    def _build_result(self, text: str, payload) -> None:
        self.text = text
        self._clear()

        lang = payload.get("language_code") or "?"
        speakers = {w.get("speaker_id") for w in (payload.get("words") or [])
                    if w.get("speaker_id")}
        info = f"{Path(self.path).name}  ·  {lang}"
        if len(speakers) > 1:
            info += f"  ·  {len(speakers)} speakers"
        label = QLabel(info)
        label.setStyleSheet("color:#8a8a8a")
        self._layout.addWidget(label)

        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        self._layout.addWidget(view, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy)
        save = QPushButton("Save…")
        save.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        for b in (copy, save, close):
            row.addWidget(b)
        self._layout.addLayout(row)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text)
        notify("Ba-Ge", "Transcript copied to clipboard.", urgency="low")

    def _save(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, "Save transcript", default_txt_path(self.path).name, "Text (*.txt)")
        if target:
            try:
                Path(target).write_text(self.text, encoding="utf-8")
                notify("Ba-Ge", f"Saved {Path(target).name}", urgency="low")
            except OSError as exc:
                notify("Ba-Ge", f"Save failed: {exc}", urgency="critical")
