"""
SmartScannerPanel — Tab widget for the undervalued stock scanner.
"""
import csv
import os
from datetime import datetime
from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.scan_result import ScanResult

_COLS = [
    "#", "Symbol", "Total", "Value", "Growth", "Tech",
    "P/E", "PEG", "D/E", "RevGrow%", "ROE%", "RSI", "AI Rank",
]
_COL_IDX = {name: i for i, name in enumerate(_COLS)}

_GREEN_BG       = QColor("#d4edda")  # >= 65  strongest
_YELLOW_BG      = QColor("#fff3cd")  # 50–64  stronger
_ORANGE_BG      = QColor("#ffe0b2")  # 35–49  weak
_DARK_ORANGE_BG = QColor("#ffb74d")  # 20–34  weaker
_RED_BG         = QColor("#ffcdd2")  # < 20   weakest


class SmartScannerPanel(QWidget):
    # Emitted when user clicks "Add Selected to Watchlist"; carries list of ScanResult
    add_to_watchlist = pyqtSignal(list)
    # Emitted when user requests a quick scan
    request_quick_scan = pyqtSignal()
    # Emitted when user requests a deep scan
    request_deep_scan = pyqtSignal()
    # Emitted when user requests a complete scan
    request_complete_scan = pyqtSignal()
    # Emitted when user cancels an active scan
    request_cancel_scan = pyqtSignal()
    # Emitted when user clicks "AI Research" with exactly 1 row selected
    research_requested = pyqtSignal(str)  # symbol

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: List[ScanResult] = []
        self._last_scan_time: Optional[datetime] = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # --- Top control row ---
        top_row = QHBoxLayout()

        self._quick_btn = QPushButton("Quick Scan")
        self._quick_btn.clicked.connect(self.request_quick_scan.emit)
        top_row.addWidget(self._quick_btn)

        self._deep_btn = QPushButton("Deep Scan")
        self._deep_btn.clicked.connect(self.request_deep_scan.emit)
        top_row.addWidget(self._deep_btn)

        self._complete_btn = QPushButton("Complete Scan")
        self._complete_btn.clicked.connect(self.request_complete_scan.emit)
        top_row.addWidget(self._complete_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.request_cancel_scan.emit)
        top_row.addWidget(self._cancel_btn)

        top_row.addStretch()

        self._last_scan_label = QLabel("Last scan: —")
        top_row.addWidget(self._last_scan_label)

        layout.addLayout(top_row)

        # --- Status / progress row ---
        status_row = QHBoxLayout()

        self._mode_label = QLabel("Mode: —")
        status_row.addWidget(self._mode_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        status_row.addWidget(self._progress_bar, stretch=1)

        layout.addLayout(status_row)

        # --- Results table ---
        self._table = QTableWidget(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._table, stretch=1)

        # --- Bottom action row ---
        bottom_row = QHBoxLayout()

        self._add_btn = QPushButton("Add Selected to Watchlist")
        self._add_btn.clicked.connect(self._on_add_selected)
        bottom_row.addWidget(self._add_btn)

        self._export_btn = QPushButton("Export CSV")
        self._export_btn.clicked.connect(self._on_export_csv)
        bottom_row.addWidget(self._export_btn)

        self._research_btn = QPushButton("AI Research")
        self._research_btn.setEnabled(False)
        self._research_btn.clicked.connect(self._on_research_clicked)
        bottom_row.addWidget(self._research_btn)

        bottom_row.addStretch()
        layout.addLayout(bottom_row)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_scan_running(self, mode: str) -> None:
        """Call when a scan starts."""
        self._mode_label.setText(f"Mode: {mode}")
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)
        self._quick_btn.setEnabled(False)
        self._deep_btn.setEnabled(False)
        self._complete_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

    def set_scan_idle(self, mode_label: str = "") -> None:
        """Call when a scan finishes or is cancelled."""
        if mode_label:
            self._mode_label.setText(f"Mode: {mode_label}")
        self._progress_bar.setVisible(False)
        self._quick_btn.setEnabled(True)
        self._deep_btn.setEnabled(True)
        self._complete_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)

    def update_progress(self, pct: int) -> None:
        self._progress_bar.setValue(pct)

    def update_status(self, text: str) -> None:
        self._mode_label.setText(text)

    def display_results(self, results: List[ScanResult]) -> None:
        self._results = results
        self._last_scan_time = datetime.now()
        self._last_scan_label.setText(
            f"Last: {self._last_scan_time.strftime('%Y-%m-%d %H:%M')}"
        )
        self._populate_table(results)

    def update_ai_rank(self, symbol: str, score: Optional[float]) -> None:
        """Update the AI Rank cell for a given symbol.

        Args:
            symbol: Ticker symbol to update.
            score: Composite rank (1.0-10.0), or None to show 'ERR'.
        """
        col = _COL_IDX["AI Rank"]
        for row in range(self._table.rowCount()):
            sym_item = self._table.item(row, _COL_IDX["Symbol"])
            if sym_item and sym_item.text() == symbol:
                if score is None:
                    item = QTableWidgetItem("ERR")
                    item.setData(Qt.UserRole, 0.0)
                else:
                    item = QTableWidgetItem(f"{score:.1f}")
                    item.setData(Qt.UserRole, float(score))
                item.setTextAlignment(Qt.AlignCenter)
                # Preserve existing row background color
                existing = self._table.item(row, 0)
                if existing:
                    item.setBackground(existing.background())
                self._table.setItem(row, col, item)
                break

    # ── Table ─────────────────────────────────────────────────────────────────

    def _populate_table(self, results: List[ScanResult]) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)

        for rank, r in enumerate(results, start=1):
            row = self._table.rowCount()
            self._table.insertRow(row)

            def _num_item(val, fmt="{:.1f}") -> QTableWidgetItem:
                if val is None:
                    item = QTableWidgetItem("—")
                    item.setData(Qt.UserRole, -1.0)
                else:
                    item = QTableWidgetItem(fmt.format(val))
                    item.setData(Qt.UserRole, float(val))
                item.setTextAlignment(Qt.AlignCenter)
                return item

            def _pct_item(val) -> QTableWidgetItem:
                if val is None:
                    item = QTableWidgetItem("—")
                    item.setData(Qt.UserRole, -999.0)
                else:
                    item = QTableWidgetItem(f"{val * 100:.1f}%")
                    item.setData(Qt.UserRole, float(val))
                item.setTextAlignment(Qt.AlignCenter)
                return item

            ai_rank_item = QTableWidgetItem("--")
            ai_rank_item.setData(Qt.UserRole, 0.0)
            ai_rank_item.setTextAlignment(Qt.AlignCenter)

            items = [
                QTableWidgetItem(str(rank)),
                QTableWidgetItem(r.symbol),
                _num_item(r.total_score),
                _num_item(r.score_value),
                _num_item(r.score_growth),
                _num_item(r.score_technical),
                _num_item(r.pe_ratio),
                _num_item(r.peg_ratio),
                _num_item(r.debt_equity),
                _pct_item(r.revenue_growth),
                _pct_item(r.roe),
                _num_item(r.rsi, fmt="{:.0f}"),
                ai_rank_item,
            ]
            items[0].setTextAlignment(Qt.AlignCenter)
            items[1].setTextAlignment(Qt.AlignCenter)

            for col, item in enumerate(items):
                self._table.setItem(row, col, item)

            # Row background by score (5 bands)
            if r.total_score >= 65:
                bg = _GREEN_BG
            elif r.total_score >= 50:
                bg = _YELLOW_BG
            elif r.total_score >= 35:
                bg = _ORANGE_BG
            elif r.total_score >= 20:
                bg = _DARK_ORANGE_BG
            else:
                bg = _RED_BG

            for col in range(len(_COLS)):
                cell = self._table.item(row, col)
                if cell:
                    cell.setBackground(bg)

        self._table.setSortingEnabled(True)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_add_selected(self) -> None:
        selected_rows = set(
            index.row() for index in self._table.selectedIndexes()
        )
        if not selected_rows:
            QMessageBox.information(
                self, "Add to Watchlist", "Select one or more rows first."
            )
            return

        # Map table row → ScanResult via symbol column
        selected_results = []
        for row in selected_rows:
            sym_item = self._table.item(row, _COL_IDX["Symbol"])
            if sym_item:
                sym = sym_item.text()
                for r in self._results:
                    if r.symbol == sym:
                        selected_results.append(r)
                        break

        if selected_results:
            self.add_to_watchlist.emit(selected_results)

    def _on_selection_changed(self) -> None:
        selected_rows = {index.row() for index in self._table.selectedIndexes()}
        self._research_btn.setEnabled(len(selected_rows) == 1)

    def _on_research_clicked(self) -> None:
        selected_rows = {index.row() for index in self._table.selectedIndexes()}
        if len(selected_rows) != 1:
            return
        row = next(iter(selected_rows))
        sym_item = self._table.item(row, _COL_IDX["Symbol"])
        if sym_item:
            self.research_requested.emit(sym_item.text())

    def _on_export_csv(self) -> None:
        if not self._results:
            QMessageBox.information(self, "Export CSV", "No scan results to export.")
            return

        default_name = (
            f"scan_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", default_name, "CSV files (*.csv)"
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Rank", "Symbol", "Total", "Value", "Growth", "Tech",
                    "P/E", "PEG", "D/E", "RevGrow%", "ROE%", "RSI",
                    "Sector", "Price", "52wkHigh", "Mode", "Timestamp",
                    "AI Rank",
                ])
                for rank, r in enumerate(self._results, start=1):
                    def _pct(v):
                        return f"{v * 100:.2f}%" if v is not None else ""

                    # Look up AI Rank from table cell
                    ai_rank_val = ""
                    ai_col = _COL_IDX["AI Rank"]
                    for row in range(self._table.rowCount()):
                        sym_item = self._table.item(row, _COL_IDX["Symbol"])
                        if sym_item and sym_item.text() == r.symbol:
                            cell = self._table.item(row, ai_col)
                            if cell and cell.text() not in ("--", "ERR"):
                                ai_rank_val = cell.text()
                            break

                    writer.writerow([
                        rank, r.symbol,
                        r.total_score, r.score_value, r.score_growth, r.score_technical,
                        r.pe_ratio or "", r.peg_ratio or "", r.debt_equity or "",
                        _pct(r.revenue_growth), _pct(r.roe),
                        r.rsi or "",
                        r.sector or "", r.price or "", r.week52_high or "",
                        r.scan_mode, r.timestamp.isoformat(),
                        ai_rank_val,
                    ])
            QMessageBox.information(
                self, "Export CSV", f"Exported {len(self._results)} rows to:\n{path}"
            )
        except OSError as exc:
            QMessageBox.warning(self, "Export CSV", f"Failed to write file:\n{exc}")
