"""
LogPanel — A Qt widget that displays Python logging output in real time.

Thread-safe: the handler emits a Qt signal, which is queued across threads
to update the text widget on the main thread.
"""
import logging
from datetime import datetime

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

_LEVEL_COLORS = {
    logging.DEBUG:    QColor("#888888"),
    logging.INFO:     QColor("#1a1a1a"),
    logging.WARNING:  QColor("#b36200"),
    logging.ERROR:    QColor("#c62828"),
    logging.CRITICAL: QColor("#6a0000"),
}
_MAX_LINES = 2000


class _SignalBridge(QObject):
    """Emits log records from any thread; Qt queues delivery to the main thread."""
    record_emitted = pyqtSignal(int, str)   # (level, message)


class QtLogHandler(logging.Handler):
    """A logging.Handler that routes records to a LogPanel via Qt signals."""

    def __init__(self):
        super().__init__()
        self._bridge = _SignalBridge()
        self.record_emitted = self._bridge.record_emitted

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._bridge.record_emitted.emit(record.levelno, msg)
        except Exception:
            self.handleError(record)


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_count = 0
        self._min_level = logging.INFO   # default: suppress DEBUG noise
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Top row
        top = QHBoxLayout()
        top.addWidget(QLabel("Application Log"))
        top.addStretch()
        top.addWidget(QLabel("Level:"))
        self._level_combo = QComboBox()
        self._level_combo.addItems(["INFO", "WARNING", "DEBUG"])
        self._level_combo.setFixedWidth(80)
        self._level_combo.currentIndexChanged.connect(self._on_level_changed)
        top.addWidget(self._level_combo)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._clear)
        top.addWidget(clear_btn)
        layout.addLayout(top)

        # Log text area
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.NoWrap)
        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.Monospace)
        self._text.setFont(font)
        layout.addWidget(self._text, stretch=1)

    def _on_level_changed(self, idx: int) -> None:
        levels = [logging.INFO, logging.WARNING, logging.DEBUG]
        self._min_level = levels[idx]

    def append_record(self, level: int, message: str) -> None:
        """Append a formatted log line. Must be called on the main thread."""
        if level < self._min_level:
            return
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS[logging.INFO])

        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.End)

        fmt = QTextCharFormat()
        fmt.setForeground(color)
        cursor.setCharFormat(fmt)
        cursor.insertText(message + "\n")

        self._line_count += 1
        if self._line_count > _MAX_LINES:
            self._trim_old_lines(200)

        # Auto-scroll to bottom
        sb = self._text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _trim_old_lines(self, n: int) -> None:
        """Remove the first `n` lines to keep the buffer bounded."""
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for _ in range(n):
            cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        self._line_count -= n

    def _clear(self) -> None:
        self._text.clear()
        self._line_count = 0


def setup_log_handler(panel: LogPanel) -> QtLogHandler:
    """Create a handler, connect it to `panel`, and attach it to the root logger."""
    handler = QtLogHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
                          datefmt="%H:%M:%S")
    )
    handler.record_emitted.connect(panel.append_record)

    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return handler
