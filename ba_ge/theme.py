"""Qt appearance — Fusion style + a clean dark palette, consistent on all OSes.

Call `apply(app)` once, right after creating the QApplication. Qt handles HiDPI
scaling automatically, so there is no manual scale handling here.
"""

from __future__ import annotations

import logging

log = logging.getLogger("bage.theme")


def apply(app) -> None:
    try:
        from PySide6.QtGui import QColor, QPalette
        app.setStyle("Fusion")

        text = QColor("#e6e6e6")
        muted = QColor("#8a8a8a")
        window = QColor("#1e1e1e")
        base = QColor("#2b2b2b")
        button = QColor("#333333")
        accent = QColor("#4f46e5")

        pal = QPalette()
        pal.setColor(QPalette.Window, window)
        pal.setColor(QPalette.WindowText, text)
        pal.setColor(QPalette.Base, base)
        pal.setColor(QPalette.AlternateBase, window)
        pal.setColor(QPalette.Text, text)
        pal.setColor(QPalette.Button, button)
        pal.setColor(QPalette.ButtonText, text)
        pal.setColor(QPalette.BrightText, QColor("#ffffff"))
        pal.setColor(QPalette.Highlight, accent)
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ToolTipBase, base)
        pal.setColor(QPalette.ToolTipText, text)
        pal.setColor(QPalette.PlaceholderText, muted)
        pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#777777"))
        pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#777777"))
        pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#777777"))
        app.setPalette(pal)
    except Exception:
        log.info("qt theme setup failed", exc_info=True)
