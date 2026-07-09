"""Qt window listing recent clipboard copies (a stack); click one to re-copy it.

Lives in a real window rather than a tray submenu because GNOME exports the tray
menu via DBusMenu as a static snapshot — a dynamically-populated submenu stays
empty there. A window has no such limitation and updates live via `historyChanged`.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

log = logging.getLogger("bage.ui.clipboard")

_current: dict = {"win": None}


def open_clipboard_window(clipboard_manager) -> None:
    """Show (or raise) the clipboard-history window. Call on the Qt main thread."""
    win = _current.get("win")
    if win is not None:
        try:
            win.refresh()
            win.show()
            win.raise_()
            win.activateWindow()
            return
        except RuntimeError:
            pass  # previous window was destroyed
    win = ClipboardWindow(clipboard_manager)
    _current["win"] = win
    win.show()


class ClipboardWindow(QWidget):
    def __init__(self, cm):
        super().__init__()
        self.cm = cm
        self.setWindowTitle("Ba-Ge — Clipboard history")
        self.resize(480, 380)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hint = QLabel("Recent copies (newest first). Double-click — or Copy — to put "
                      "one back on the clipboard. Your dictations are not listed here.")
        hint.setStyleSheet("color:#8a8a8a")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.list = QListWidget()
        self.list.itemActivated.connect(self._copy_item)  # double-click / Enter
        root.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        copy = QPushButton("Copy selected")
        copy.clicked.connect(self._copy_selected)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(copy)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self.refresh()
        try:
            cm.historyChanged.connect(self.refresh)  # stay current while open
        except Exception:
            log.debug("could not connect historyChanged", exc_info=True)

    def refresh(self) -> None:
        self.list.clear()
        items = self.cm.history()
        if not items:
            placeholder = QListWidgetItem("(nothing copied yet)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list.addItem(placeholder)
            return
        for text in items:
            label = " ".join(text.split())
            if len(label) > 90:
                label = label[:90] + "…"
            item = QListWidgetItem(label or "(blank)")
            item.setData(Qt.UserRole, text)
            item.setToolTip(text[:400])
            self.list.addItem(item)

    def _copy_item(self, item) -> None:
        text = item.data(Qt.UserRole)
        if text:
            self.cm.set_clipboard(text)

    def _copy_selected(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self._copy_item(item)

    def _clear(self) -> None:
        self.cm.clear_history()
        self.refresh()
