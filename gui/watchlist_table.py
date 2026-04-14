from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView

from core.models import StockEntry

_COL_SYMBOL = 0
_COL_PRICE = 1
_COL_LOW = 2
_COL_HIGH = 3
_COL_STATUS = 4
_COL_NOTES = 5

_HEADERS = ["Symbol", "Price", "Low Target", "High Target", "Status", "Notes"]

_HEADER_TOOLTIPS = [
    "Stock ticker symbol (e.g. AAPL, TSLA)",
    "Current market price — refreshed automatically on the poll interval",
    "Your buy-zone floor.\nYou'll be alerted when the price drops to or below this target.",
    "Your sell-zone ceiling.\nYou'll be alerted when the price rises to or above this target.",
    "OK = price is within your range\nBELOW LOW = price hit buy target (green)\nABOVE HIGH = price hit sell target (red)",
    "Your personal notes for this stock",
]

_COLOR_OK = QColor("#ffffff")
_COLOR_ABOVE = QColor("#ffcccc")   # light red — sell signal
_COLOR_BELOW = QColor("#ccffcc")   # light green — buy opportunity
_COLOR_TEXT_DARK = QColor("#000000")


class WatchlistTable(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(0, len(_HEADERS), parent)
        self.setHorizontalHeaderLabels(_HEADERS)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(_COL_SYMBOL, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False)

        # Column header tooltips
        for col, tip in enumerate(_HEADER_TOOLTIPS):
            self.model().setHeaderData(col, Qt.Horizontal, tip, Qt.ToolTipRole)

    def refresh(self, entries: List[StockEntry]) -> None:
        self.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            price_str = f"${entry.current_price:.2f}" if entry.current_price is not None else "—"
            values = [
                entry.symbol,
                price_str,
                f"${entry.low_target:.2f}",
                f"${entry.high_target:.2f}",
                entry.alert_status,
                entry.notes,
            ]
            if entry.alert_status == "ABOVE HIGH":
                bg = _COLOR_ABOVE
            elif entry.alert_status == "BELOW LOW":
                bg = _COLOR_BELOW
            else:
                bg = _COLOR_OK

            for col, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                item.setBackground(bg)
                item.setForeground(_COLOR_TEXT_DARK)
                item.setTextAlignment(Qt.AlignCenter)
                self.setItem(row, col, item)

    def selected_row(self) -> int:
        rows = self.selectionModel().selectedRows()
        return rows[0].row() if rows else -1
