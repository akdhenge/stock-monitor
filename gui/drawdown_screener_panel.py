"""
DrawdownScreenerPanel — Tab widget for the sentiment-driven drawdown screener.
Finds quality stocks hammered by non-fundamental sentiment concerns.
"""
from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
    QWidget,
)

from core.drawdown_result import DrawdownResult

# Candidate table columns
_COLS = [
    "#", "Symbol", "Score", "% Below High", "Analyst Upside",
    "Buy%", "Rev Growth", "Earnings Beat", "Next Earnings", "Cause", "Confidence", "Commodity",
]
_COL_TOOLTIPS = {
    "#":             "Composite rank (1 = highest score)",
    "Symbol":        "Stock ticker symbol",
    "Score":         "Composite score 0–100. Weighted: Analyst Upside 40%, Fundamentals 25%, Drawdown 20%, Options 15%. MEDIUM commodity exposure applies a 15% score penalty.",
    "% Below High":  "How far the stock is below its 52-week high. Screen targets 20–50% range.",
    "Analyst Upside":"Consensus analyst target vs current price. Screen requires ≥25% upside.",
    "Buy%":          "% of covering analysts rating Buy or Strong Buy. Screen requires ≥70%.",
    "Rev Growth":    "Year-over-year revenue growth. Screen requires ≥10%.",
    "Earnings Beat": "Whether the most recent quarter beat EPS estimates (Finnhub data).",
    "Next Earnings": "Next scheduled earnings date. Key for expiration selection.",
    "Cause":         "LLM-classified primary cause of the drop. Sentiment causes pass; fundamental damage fails.",
    "Confidence":    "LLM confidence in the cause classification (high/medium/low).",
    "Commodity":     "Commodity exposure assessed by LLM. HIGH = rejected (price driven by commodity it doesn't control). MEDIUM = flagged with 15% score penalty. LOW = clean.",
}

# Miss table: same columns plus "Failed Gate"
_MISS_COLS = _COLS + ["Failed Gate"]

_GREEN  = QColor("#d4edda")
_YELLOW = QColor("#fff3cd")
_ORANGE = QColor("#ffe0b2")
_GRAY   = QColor("#f0f0f0")

_CAUSE_COLORS = {
    "capex_concern":           QColor("#d4edda"),
    "margin_pressure":         QColor("#d4edda"),
    "sector_rotation":         QColor("#d4edda"),
    "one_time_legal":          QColor("#fff3cd"),
    "macro_panic":             QColor("#d4edda"),
    "guidance_cut":            QColor("#fff3cd"),
    "unclear":                 QColor("#ffe0b2"),
    "SKIPPED":                 QColor("#f0f0f0"),
    "demand_decline":          QColor("#ffcdd2"),
    "share_loss":              QColor("#ffcdd2"),
    "product_failure":         QColor("#ffcdd2"),
    "accounting":              QColor("#ffcdd2"),
    "exec_departure":          QColor("#ffcdd2"),
    "existential_regulatory":  QColor("#ffcdd2"),
    "secular_decline":         QColor("#ffcdd2"),
}


class _NumericItem(QTableWidgetItem):
    def __lt__(self, other: "QTableWidgetItem") -> bool:
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except (TypeError, ValueError):
            return super().__lt__(other)


def _num_item(value: Optional[float], fmt: str = ".1f") -> _NumericItem:
    if value is None:
        item = _NumericItem("—")
        item.setData(Qt.UserRole, -9999.0)
    else:
        item = _NumericItem(format(value, fmt))
        item.setData(Qt.UserRole, float(value))
    item.setTextAlignment(Qt.AlignCenter)
    return item


def _pct_item(value: Optional[float]) -> _NumericItem:
    if value is None:
        item = _NumericItem("—")
        item.setData(Qt.UserRole, -9999.0)
    else:
        item = _NumericItem(f"{value*100:.1f}%")
        item.setData(Qt.UserRole, float(value))
    item.setTextAlignment(Qt.AlignCenter)
    return item


def _text_item(text: str, center: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    if center:
        item.setTextAlignment(Qt.AlignCenter)
    return item


def _make_table(cols: List[str]) -> QTableWidget:
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setSortingEnabled(True)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(False)
    return t


class DrawdownScreenerPanel(QWidget):
    request_scan   = pyqtSignal()
    request_cancel = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: List[DrawdownResult] = []
        self._selected_result: Optional[DrawdownResult] = None
        self._setup_ui()

    # ── UI setup ──────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Control bar
        ctrl = QHBoxLayout()
        self._btn_run = QPushButton("Run Screener")
        self._btn_run.setToolTip(
            "Screen S&P 500 for sentiment-driven drawdown candidates.\n"
            "Applies 5 sequential gates: drawdown, fundamentals, analyst conviction, "
            "options liquidity, and LLM cause classification."
        )
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setEnabled(False)
        self._lbl_status = QLabel("Idle — click Run Screener to start")
        self._lbl_status.setStyleSheet("color: gray;")
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedWidth(200)

        ctrl.addWidget(self._btn_run)
        ctrl.addWidget(self._btn_cancel)
        ctrl.addSpacing(12)
        ctrl.addWidget(self._lbl_status, stretch=1)
        ctrl.addWidget(self._progress)
        root.addLayout(ctrl)

        # Disclaimer
        disc = QLabel(
            "Candidate-finder only — not financial advice. "
            "Final trade decisions stay with the user. Backtest before trading."
        )
        disc.setStyleSheet("color: #888; font-size: 10px;")
        root.addWidget(disc)

        # Cost summary bar
        cost_row = QHBoxLayout()
        cost_lbl = QLabel("Last run cost:")
        cost_lbl.setStyleSheet("color: #666; font-size: 10px;")
        self._cost_deepseek = QLabel("DeepSeek: —")
        self._cost_finnhub  = QLabel("Finnhub: —")
        self._cost_tokens   = QLabel("Tokens: —")
        for w in (self._cost_deepseek, self._cost_finnhub, self._cost_tokens):
            w.setStyleSheet("color: #444; font-size: 10px; padding: 0 8px;")
        cost_row.addWidget(cost_lbl)
        cost_row.addWidget(self._cost_deepseek)
        cost_row.addWidget(self._cost_finnhub)
        cost_row.addWidget(self._cost_tokens)
        cost_row.addStretch()
        root.addLayout(cost_row)

        # Splitter: tables top, detail pane bottom
        splitter = QSplitter(Qt.Vertical)

        # Top: candidates + near-misses
        tables_widget = QWidget()
        tables_layout = QVBoxLayout(tables_widget)
        tables_layout.setContentsMargins(0, 0, 0, 0)

        # Candidates table
        cand_group = QGroupBox("Ranked Candidates")
        cand_layout = QVBoxLayout(cand_group)
        self._table = _make_table(_COLS)
        self._table.setToolTip("Click a row to see full details below.")
        for i, col in enumerate(_COLS):
            if col in _COL_TOOLTIPS:
                self._table.horizontalHeaderItem(i).setToolTip(_COL_TOOLTIPS[col])
        cand_layout.addWidget(self._table)
        tables_layout.addWidget(cand_group)

        # Near-misses table
        miss_group = QGroupBox("Rejected-but-Close (passed 4 of 5 gates — worth monitoring)")
        miss_group.setCheckable(True)
        miss_group.setChecked(False)
        miss_layout = QVBoxLayout(miss_group)
        self._miss_table = _make_table(_MISS_COLS)
        miss_layout.addWidget(self._miss_table)
        tables_layout.addWidget(miss_group)

        splitter.addWidget(tables_widget)

        # Bottom: detail pane
        detail_group = QGroupBox("Candidate Detail")
        detail_layout = QVBoxLayout(detail_group)
        self._detail = QTextBrowser()
        self._detail.setPlaceholderText("Click a candidate row to see full details here.")
        self._detail.setOpenExternalLinks(True)
        detail_layout.addWidget(self._detail)
        splitter.addWidget(detail_group)

        splitter.setSizes([450, 200])
        root.addWidget(splitter, stretch=1)

        # Wire buttons
        self._btn_run.clicked.connect(self.request_scan)
        self._btn_cancel.clicked.connect(self.request_cancel)
        self._table.currentItemChanged.connect(self._on_selection_changed)

    # ── Public slots ──────────────────────────────────────────────────────────

    def set_status(self, msg: str) -> None:
        self._lbl_status.setText(msg)

    def update_progress(self, pct: int) -> None:
        self._progress.setValue(pct)

    def update_cost(self, cost: dict) -> None:
        calls   = cost.get("deepseek_calls", 0)
        in_tok  = cost.get("input_tokens", 0)
        out_tok = cost.get("output_tokens", 0)
        usd     = cost.get("cost_usd", 0.0)
        fh      = cost.get("finnhub_calls", 0)

        if calls == 0:
            self._cost_deepseek.setText("DeepSeek: skipped")
        else:
            self._cost_deepseek.setText(
                f"DeepSeek: {calls} calls  ${usd:.4f}"
            )
        self._cost_finnhub.setText(f"Finnhub: {fh} calls (free)")
        self._cost_tokens.setText(f"Tokens: {in_tok:,} in / {out_tok:,} out")

    def set_scan_running(self) -> None:
        self._btn_run.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._progress.setValue(0)
        self._lbl_status.setStyleSheet("color: #333;")

    def set_scan_idle(self) -> None:
        self._btn_run.setEnabled(True)
        self._btn_cancel.setEnabled(False)

    def display_results(self, results: List[DrawdownResult]) -> None:
        self._results = results
        candidates = [r for r in results if r.failed_gate is None]
        misses = [r for r in results if r.failed_gate is not None]

        self._populate_table(self._table, candidates, include_failed_gate=False)
        self._populate_table(self._miss_table, misses, include_failed_gate=True)
        self.set_scan_idle()

    # ── Table population ──────────────────────────────────────────────────────

    def _populate_table(
        self,
        table: QTableWidget,
        results: List[DrawdownResult],
        include_failed_gate: bool,
    ) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(0)

        for rank, r in enumerate(results, start=1):
            row = table.rowCount()
            table.insertRow(row)

            bg = self._row_color(r)

            comm = r.commodity_exposure or ""
            if comm == "MEDIUM":
                comm_str = "⚠ Med"
            elif comm == "HIGH":
                comm_str = "❌ High"
            else:
                comm_str = "—"

            items = [
                _num_item(float(rank), ".0f"),
                _text_item(r.symbol),
                _num_item(r.score),
                _pct_item(r.pct_below_high),
                _pct_item(r.analyst_upside_pct),
                _pct_item(r.buy_rating_pct),
                _pct_item(r.revenue_growth_yoy),
                _text_item("Yes" if r.earnings_beat else "No"),
                _text_item(r.next_earnings_date or "—"),
                _text_item(r.cause_label.replace("_", " ")),
                _text_item(r.cause_confidence),
                _text_item(comm_str),
            ]

            if include_failed_gate:
                items.append(_text_item(
                    (r.failed_gate or "").replace("_", " ").replace("gate", "Gate")
                ))

            for col, item in enumerate(items):
                item.setBackground(bg)
                table.setItem(row, col, item)

        table.setSortingEnabled(True)

    def _row_color(self, r: DrawdownResult) -> QColor:
        if r.failed_gate is not None:
            return _GRAY
        if r.score >= 70:
            return _GREEN
        if r.score >= 50:
            return _YELLOW
        return _ORANGE

    # ── Detail pane ───────────────────────────────────────────────────────────

    def _on_selection_changed(self, current, previous) -> None:
        if current is None:
            return
        row = current.row()
        candidates = [r for r in self._results if r.failed_gate is None]
        if 0 <= row < len(candidates):
            self._show_detail(candidates[row])

    def _show_detail(self, r: DrawdownResult) -> None:
        cause_bg = _CAUSE_COLORS.get(r.cause_label, QColor("white"))
        cause_hex = cause_bg.name()

        comm = r.commodity_exposure or ""
        if comm == "MEDIUM":
            commodity_section = (
                '<h3>Commodity Exposure</h3>'
                '<div style="background:#fff3cd; padding:8px; border-radius:4px;">'
                '<b>⚠ MEDIUM — commodity adjacent</b><br>'
                f'{r.commodity_rationale or "Assessed by LLM — meaningful commodity exposure with non-commodity growth angles."}'
                '</div>'
            )
        elif comm == "HIGH":
            commodity_section = (
                '<h3>Commodity Exposure</h3>'
                '<div style="background:#ffcdd2; padding:8px; border-radius:4px;">'
                '<b>❌ HIGH — commodity-driven</b><br>'
                f'{r.commodity_rationale or "Excluded: price primarily driven by a commodity the company doesn\'t control."}'
                '</div>'
            )
        else:
            commodity_section = ""

        html = f"""
<h2>{r.symbol}</h2>
<table width="100%" cellspacing="4">
<tr>
  <td><b>Composite Score:</b></td><td>{r.score:.1f} / 100</td>
  <td><b>Market Cap:</b></td><td>${r.market_cap_b:.1f}B</td>
</tr>
<tr>
  <td><b>Current Price:</b></td><td>${r.current_price:.2f}</td>
  <td><b>% Below 52w High:</b></td><td>{r.pct_below_high*100:.1f}%</td>
</tr>
<tr>
  <td><b>Days Since High:</b></td><td>{r.days_since_high}</td>
  <td><b>Options Verified:</b></td><td>{"Yes" if r.options_verified else "Unverified"}</td>
</tr>
</table>

<h3>Analyst Conviction</h3>
<table width="100%" cellspacing="4">
<tr>
  <td><b>Consensus Upside:</b></td><td>{r.analyst_upside_pct*100:.1f}%</td>
  <td><b>Buy/Strong Buy:</b></td><td>{r.buy_rating_pct*100:.1f}%</td>
</tr>
<tr>
  <td><b>Analyst Count:</b></td><td>{r.analyst_count}</td>
  <td><b>Next Earnings:</b></td><td>{r.next_earnings_date or "Unknown"}</td>
</tr>
</table>

<h3>Fundamentals</h3>
<table width="100%" cellspacing="4">
<tr>
  <td><b>Revenue Growth YoY:</b></td><td>{r.revenue_growth_yoy*100:.1f}%</td>
  <td><b>Earnings Beat:</b></td><td>{"Yes" if r.earnings_beat else "No"}</td>
</tr>
<tr>
  <td><b>Operating Cash Flow:</b></td>
  <td>${r.operating_cashflow/1e9:.2f}B</td>
</tr>
</table>

<h3>Sub-Scores</h3>
<table width="100%" cellspacing="4">
<tr>
  <td><b>Analyst Upside (40%):</b></td><td>{r.score_analyst:.1f}</td>
  <td><b>Fundamentals (25%):</b></td><td>{r.score_fundamentals:.1f}</td>
</tr>
<tr>
  <td><b>Drawdown (20%):</b></td><td>{r.score_drawdown:.1f}</td>
  <td><b>Options (15%):</b></td><td>{r.score_options:.1f}</td>
</tr>
</table>

<h3>Cause of Drop</h3>
<div style="background:{cause_hex}; padding:8px; border-radius:4px;">
  <b>Classification:</b> {r.cause_label.replace("_", " ").title()}
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <b>Confidence:</b> {r.cause_confidence}<br><br>
  {r.cause_summary or "<i>No summary available.</i>"}
</div>

{commodity_section}

<p style="color:#888; font-size:10px;">
  Screened {r.timestamp.strftime("%Y-%m-%d %H:%M")} &nbsp;|&nbsp;
  This is a candidate finder only — not financial advice.
  Verify all data independently before making any trading decisions.
</p>
"""
        self._detail.setHtml(html)
