from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import QTextEdit, QVBoxLayout, QLabel, QWidget

from core.models import AlertRecord

_COLOR_ABOVE = "#cc0000"
_COLOR_BELOW = "#006600"


class AlertHistoryPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        label = QLabel("Alert History:")
        label.setStyleSheet("font-weight: bold;")
        layout.addWidget(label)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumHeight(140)
        self._text.setStyleSheet("font-family: monospace; font-size: 11px;")
        layout.addWidget(self._text)

    def add_record(self, record: AlertRecord) -> None:
        time_str = record.timestamp.strftime("%H:%M:%S")
        if record.direction == "ABOVE HIGH":
            color = _COLOR_ABOVE
            label = "ABOVE HIGH"
        else:
            color = _COLOR_BELOW
            label = "BELOW LOW"

        line = (
            f"[{time_str}] {record.symbol}: "
            f"${record.price:.2f} {label} ${record.target:.2f}"
        )
        if record.notified:
            line += " ✓"

        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.End)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        if not self._text.toPlainText():
            cursor.insertText(line)
        else:
            cursor.insertText("\n" + line)

        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

    def clear(self) -> None:
        self._text.clear()
