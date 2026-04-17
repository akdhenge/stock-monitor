"""
SmartScannerPanel — Tab widget for the undervalued stock scanner.
"""
import csv
import os
from datetime import datetime
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from core.ai_research_store import get_cached_entry, get_cached_symbols
from core.scan_result import ScanResult

_COLS = [
    "#", "Symbol", "Total", "Value", "Growth", "Tech",
    "P/E", "PEG", "D/E", "RevGrow%", "ROE%", "RSI", "AI Rank", "Rsrch",
]
_COL_IDX = {name: i for i, name in enumerate(_COLS)}

_COL_TOOLTIPS = {
    "#":        "Rank by total score (1 = highest score)",
    "Symbol":   "Stock ticker symbol",
    "Total":    "Composite score (0–100).\nWeighted: Value×40% + Growth×30% + Technical×30%.\nHigher is better.",
    "Value":    "Value score (0–100).\nDerived from P/E ratio, PEG ratio, and Debt/Equity.\nMeasures how cheap the stock is relative to its fundamentals.",
    "Growth":   "Growth score (0–100).\nDerived from revenue growth rate and return on equity.\nMeasures business momentum.",
    "Tech":     "Technical score (0–100).\nDerived from RSI, MACD signal, proximity to 200-day moving average, and volume spikes.",
    "P/E":      "Price-to-Earnings ratio.\nLower = cheaper relative to earnings.\nNegative = company is currently unprofitable.",
    "PEG":      "Price/Earnings-to-Growth ratio.\nPEG < 1.0 often indicates the stock may be undervalued relative to its growth rate.",
    "D/E":      "Debt-to-Equity ratio.\nLower = less leveraged.\nAbove 2.0 can indicate elevated financial risk.",
    "RevGrow%": "Year-over-year revenue growth percentage.\nHigher = faster growing company.",
    "ROE%":     "Return on Equity.\nHow efficiently the company turns shareholder equity into profit.\nHigher is better.",
    "RSI":      "Relative Strength Index (0–100).\n< 30 = oversold (potential buy signal).\n> 70 = overbought (potential sell signal).",
    "AI Rank":  "AI-generated rank (1 = best).\nAfter a deep/complete scan, the top 10 stocks are ranked by the LLM.\nLower number = more compelling opportunity according to the AI.",
    "Rsrch":    "AI research status.\n🟢 Green = cached analysis available (< 6 hrs).\n🟡 Yellow = AI ranking in progress.\n🔴 Red = no cached research.\nRight-click any row to run AI Research.",
}

_GREEN_BG       = QColor("#d4edda")  # >= 65  strongest
_YELLOW_BG      = QColor("#fff3cd")  # 50–64  stronger
_ORANGE_BG      = QColor("#ffe0b2")  # 35–49  weak
_DARK_ORANGE_BG = QColor("#ffb74d")  # 20–34  weaker
_RED_BG         = QColor("#ffcdd2")  # < 20   weakest

# Traffic light foreground colors for the Rsrch column dot (●)
_LIGHT_RED    = QColor("#e53935")   # no cached research
_LIGHT_YELLOW = QColor("#f9a825")   # AI ranking in progress
_LIGHT_GREEN  = QColor("#43a047")   # cached research available

_LEGEND = [
    (_GREEN_BG,       "≥ 65  Strong"),
    (_YELLOW_BG,      "50–64  Good"),
    (_ORANGE_BG,      "35–49  Moderate"),
    (_DARK_ORANGE_BG, "20–34  Weak"),
    (_RED_BG,         "< 20  Poor"),
]


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically via Qt.UserRole instead of display text."""
    def __lt__(self, other: "QTableWidgetItem") -> bool:
        self_val  = self.data(Qt.UserRole)
        other_val = other.data(Qt.UserRole)
        try:
            return float(self_val) < float(other_val)
        except (TypeError, ValueError):
            return super().__lt__(other)


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

        # Refresh the "X min ago" label every 60 seconds
        self._freshness_timer = QTimer(self)
        self._freshness_timer.setInterval(60_000)
        self._freshness_timer.timeout.connect(self._refresh_last_scan_label)
        self._freshness_timer.start()

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

        # AI ranking status — hidden until ranking is active
        self._ai_rank_status_label = QLabel("")
        self._ai_rank_status_label.setStyleSheet(
            "color: #1a237e; background: #e8eaf6; border-radius: 4px; "
            "padding: 2px 10px; font-size: 11px; font-weight: bold;"
        )
        self._ai_rank_status_label.setVisible(False)
        status_row.addWidget(self._ai_rank_status_label)

        self._ai_rank_detail_label = QLabel("")
        self._ai_rank_detail_label.setStyleSheet(
            "color: #555; font-size: 10px; padding: 1px 6px;"
        )
        self._ai_rank_detail_label.setVisible(False)
        status_row.addWidget(self._ai_rank_detail_label)

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
        self._table.currentItemChanged.connect(self._on_current_item_changed)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        # Column header tooltips
        _model = self._table.model()
        for col, name in enumerate(_COLS):
            if name in _COL_TOOLTIPS:
                _model.setHeaderData(col, Qt.Horizontal, _COL_TOOLTIPS[name], Qt.ToolTipRole)

        # --- Horizontal splitter: table (left) + research panel (right) ---
        self._research_panel = _ResearchPanel()
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._table)
        splitter.addWidget(self._research_panel)
        splitter.setSizes([650, 350])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        # --- Color legend row ---
        legend_row = QHBoxLayout()
        legend_row.setSpacing(6)
        legend_label = QLabel("Score:")
        legend_label.setStyleSheet("font-size: 11px; color: #555;")
        legend_row.addWidget(legend_label)
        for color, text in _LEGEND:
            chip = QLabel(f"  {text}  ")
            chip.setStyleSheet(
                f"background-color: {color.name()}; "
                "border: 1px solid #bbb; border-radius: 3px; "
                "font-size: 11px; padding: 1px 4px;"
            )
            legend_row.addWidget(chip)
        legend_row.addStretch()
        layout.addLayout(legend_row)

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

    def set_ai_rank_status(
        self, line1: str, line2: str = "", visible: bool = True
    ) -> None:
        """Show/update (or hide) the AI ranking status labels in the status row."""
        self._ai_rank_status_label.setText(line1)
        self._ai_rank_status_label.setVisible(visible and bool(line1))
        self._ai_rank_detail_label.setText(line2)
        self._ai_rank_detail_label.setVisible(visible and bool(line2))

    def update_progress(self, pct: int) -> None:
        self._progress_bar.setValue(pct)

    def update_status(self, text: str) -> None:
        self._mode_label.setText(text)

    def display_results(self, results: List[ScanResult], scan_time: Optional[datetime] = None) -> None:
        self._results = results
        self._last_scan_time = scan_time or datetime.now()
        self._refresh_last_scan_label()
        self._populate_table(results)

    def _refresh_last_scan_label(self) -> None:
        """Update the last-scan label with a relative time string and staleness color."""
        if self._last_scan_time is None:
            self._last_scan_label.setText("Last scan: —")
            self._last_scan_label.setStyleSheet("")
            return
        delta = datetime.now() - self._last_scan_time
        total_minutes = int(delta.total_seconds() / 60)
        if total_minutes < 1:
            age = "just now"
        elif total_minutes < 60:
            age = f"{total_minutes} min ago"
        else:
            hours = total_minutes // 60
            mins = total_minutes % 60
            age = f"{hours}h {mins}m ago" if mins else f"{hours}h ago"

        # Color: green < 1h, yellow 1–4h, red > 4h
        if total_minutes < 60:
            color = "#155724"   # green
        elif total_minutes < 240:
            color = "#856404"   # amber
        else:
            color = "#721c24"   # red

        ts = self._last_scan_time.strftime("%Y-%m-%d %H:%M")
        self._last_scan_label.setText(f"Last scan: {ts}  ({age})")
        self._last_scan_label.setStyleSheet(f"color: {color}; font-weight: bold;")

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
                    item = _NumericItem("ERR")
                    item.setData(Qt.UserRole, 9998.0)  # sort last (but before unranked --)
                else:
                    item = _NumericItem(str(int(score)))
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

        # Load AI research cache once for the whole table render (avoids 500 file reads)
        cached_symbols = get_cached_symbols()

        for rank, r in enumerate(results, start=1):
            row = self._table.rowCount()
            self._table.insertRow(row)

            def _num_item(val, fmt="{:.1f}") -> _NumericItem:
                if val is None:
                    item = _NumericItem("—")
                    item.setData(Qt.UserRole, -1.0)
                else:
                    item = _NumericItem(fmt.format(val))
                    item.setData(Qt.UserRole, float(val))
                item.setTextAlignment(Qt.AlignCenter)
                return item

            def _pct_item(val) -> _NumericItem:
                if val is None:
                    item = _NumericItem("—")
                    item.setData(Qt.UserRole, -999.0)
                else:
                    item = _NumericItem(f"{val * 100:.1f}%")
                    item.setData(Qt.UserRole, float(val))
                item.setTextAlignment(Qt.AlignCenter)
                return item

            if r.ai_rank is not None:
                ai_rank_item = _NumericItem(str(r.ai_rank))
                ai_rank_item.setData(Qt.UserRole, float(r.ai_rank))
            else:
                ai_rank_item = _NumericItem("--")
                ai_rank_item.setData(Qt.UserRole, 9999.0)  # sort unranked last
            ai_rank_item.setTextAlignment(Qt.AlignCenter)

            rank_item = _NumericItem(str(rank))
            rank_item.setData(Qt.UserRole, float(rank))
            rank_item.setTextAlignment(Qt.AlignCenter)

            is_cached = r.symbol in cached_symbols
            ai_status_item = QTableWidgetItem("●")
            ai_status_item.setForeground(_LIGHT_GREEN if is_cached else _LIGHT_RED)
            ai_status_item.setTextAlignment(Qt.AlignCenter)

            items = [
                rank_item,
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
                ai_status_item,
            ]
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
        """Enable/disable the AI Research button based on selection count."""
        selected_rows = {item.row() for item in self._table.selectedItems()}
        self._research_btn.setEnabled(len(selected_rows) == 1)

    def _on_current_item_changed(self, current, previous) -> None:
        """Update the inline research panel when the user moves to a new row.

        Uses currentItemChanged instead of itemSelectionChanged because the latter
        does not re-fire if the user clicks the same already-selected row, and can
        behave inconsistently while sorting is active.

        Uses current.row() directly rather than selectedItems() because Qt can fire
        currentItemChanged before the selection state is fully updated, making
        selectedItems() return stale or empty results.
        """
        if current is None:
            return
        sym_item = self._table.item(current.row(), _COL_IDX["Symbol"])
        if sym_item:
            self._research_panel.show_symbol(sym_item.text())

    def _on_research_clicked(self) -> None:
        selected_items = self._table.selectedItems()
        selected_rows = {item.row() for item in selected_items}
        if len(selected_rows) != 1:
            return
        for item in selected_items:
            if item.column() == _COL_IDX["Symbol"]:
                self.research_requested.emit(item.text())
                break

    def _show_context_menu(self, pos) -> None:
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        sym_item = self._table.item(row, _COL_IDX["Symbol"])
        if not sym_item:
            return
        symbol = sym_item.text()
        menu = QMenu(self)
        action = menu.addAction(f"AI Research: {symbol}")
        chosen = menu.exec_(self._table.viewport().mapToGlobal(pos))
        if chosen == action:
            self.research_requested.emit(symbol)

    def set_research_light(self, symbol: str, state: str) -> None:
        """Update the Rsrch traffic light for a symbol.

        Args:
            symbol: Ticker to update.
            state:  "red" | "yellow" | "green"
        """
        color_map = {"red": _LIGHT_RED, "yellow": _LIGHT_YELLOW, "green": _LIGHT_GREEN}
        color = color_map.get(state, _LIGHT_RED)
        col = _COL_IDX["Rsrch"]
        for row in range(self._table.rowCount()):
            sym_item = self._table.item(row, _COL_IDX["Symbol"])
            if sym_item and sym_item.text() == symbol:
                cell = self._table.item(row, col)
                if cell:
                    cell.setForeground(color)
                break

    def refresh_research_panel(self, symbol: str) -> None:
        """Re-render the inline research panel if it's currently showing `symbol`."""
        self._research_panel.refresh_if_showing(symbol)

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


# ── Inline Research Panel ──────────────────────────────────────────────────────

class _ResearchPanel(QWidget):
    """Read-only inline panel that renders cached AI research for the selected stock."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_symbol: Optional[str] = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 4, 4)
        layout.setSpacing(4)

        self._header = QLabel("AI Research")
        self._header.setStyleSheet("font-weight: bold; font-size: 13px; padding-bottom: 2px;")
        layout.addWidget(self._header)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, stretch=1)

        # Page 0 — no row selected
        p0 = QWidget()
        l0 = QVBoxLayout(p0)
        l0.setAlignment(Qt.AlignCenter)
        lbl = QLabel("Select a stock to view\ncached AI research")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color: #999; font-size: 12px;")
        l0.addWidget(lbl)
        self._stack.addWidget(p0)

        # Page 1 — selected but no cache
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setAlignment(Qt.AlignCenter)
        self._no_cache_label = QLabel()
        self._no_cache_label.setAlignment(Qt.AlignCenter)
        self._no_cache_label.setStyleSheet("color: #888; font-size: 12px;")
        self._no_cache_label.setWordWrap(True)
        l1.addWidget(self._no_cache_label)
        self._stack.addWidget(p1)

        # Page 2 — cached data
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(0, 0, 0, 0)
        l2.setSpacing(4)

        self._sentiment_label = QLabel()
        self._sentiment_label.setAlignment(Qt.AlignCenter)
        l2.addWidget(self._sentiment_label)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        l2.addWidget(self._browser, stretch=1)

        self._footer_label = QLabel()
        self._footer_label.setStyleSheet("color: gray; font-size: 10px;")
        self._footer_label.setAlignment(Qt.AlignRight)
        l2.addWidget(self._footer_label)

        self._stack.addWidget(p2)
        self._stack.setCurrentIndex(0)

    def show_symbol(self, symbol: str) -> None:
        """Display cached research for `symbol`, or a 'no cache' placeholder."""
        self._current_symbol = symbol
        self._header.setText(f"AI Research — {symbol}")
        data = get_cached_entry(symbol)
        if data is None:
            self._no_cache_label.setText(
                f"No cached research for {symbol}.\n\n"
                "Right-click the row → AI Research to run it."
            )
            self._stack.setCurrentIndex(1)
        else:
            self._render(data)

    def refresh_if_showing(self, symbol: str) -> None:
        """Re-render if this panel is currently showing `symbol`."""
        if self._current_symbol == symbol:
            self.show_symbol(symbol)

    def _render(self, data: dict) -> None:
        from gui.ai_research_dialog import render_research_html

        sentiment = data.get("sentiment", "NEUTRAL").upper()
        _styles = {
            "BULLISH": ("BULLISH", "background: #d4edda; color: #155724;"),
            "BEARISH": ("BEARISH", "background: #f8d7da; color: #721c24;"),
        }
        text, style = _styles.get(sentiment, ("NEUTRAL", "background: #e2e3e5; color: #383d41;"))
        self._sentiment_label.setText(text)
        self._sentiment_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; padding: 3px 12px; "
            f"border-radius: 5px; {style}"
        )

        html = render_research_html(data)
        if html.strip():
            self._browser.setHtml(html)
        else:
            self._browser.setHtml(
                "<p style='color:#888; font-size:12px;'>"
                "Cached research has no content.<br>"
                "Right-click the row → AI Research to refresh it.</p>"
            )

        source = data.get("source", "")
        ts_str = data.get("timestamp", "")
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(ts_str).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = ts_str
        self._footer_label.setText(f"{source}  |  {ts}")
        self._stack.setCurrentIndex(2)
