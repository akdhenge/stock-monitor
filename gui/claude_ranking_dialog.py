import json
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)


def _relative_time(ts_str: str) -> str:
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return ts_str


class ClaudeRankingDialog(QDialog):
    """Displays Claude's portfolio ranking of the top-10 scan results."""

    def __init__(self, result: dict, parent=None, on_rerun: Optional[Callable] = None):
        super().__init__(parent)
        self.setWindowTitle("Claude Portfolio Ranking")
        self.setMinimumSize(1000, 600)
        self.setModal(False)
        self._result = result
        self._on_rerun = on_rerun
        self._setup_ui(result)

    def _setup_ui(self, result: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Title + relative timestamp
        ts = result.get("generated_at", "")
        model = result.get("model", "")
        rel = _relative_time(ts)
        title = QLabel(f"Claude Portfolio Ranking — last ran {rel}  [{model}]")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        # Ranked table
        ranked = result.get("ranked", [])
        cols = ["Rank", "Symbol", "Sector", "Risk", "Rationale", "Stock Play", "Options Play", "Alloc %"]
        table = QTableWidget(len(ranked), len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.horizontalHeader().setStretchLastSection(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)

        rank_colors = {1: "#FFD700", 2: "#C0C0C0", 3: "#CD7F32"}

        for row, entry in enumerate(ranked):
            rank = entry.get("rank", row + 1)
            alloc = entry.get("allocation_pct")
            alloc_str = f"{alloc}%" if alloc is not None else "—"

            values = [
                str(rank),
                entry.get("symbol", ""),
                "",          # sector filled below from scan results
                entry.get("risk", ""),
                entry.get("rationale", ""),
                entry.get("stock_play", ""),
                entry.get("options_play") or "—",
                alloc_str,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0 and rank in rank_colors:
                    item.setBackground(QColor(rank_colors[rank]))
                    item.setForeground(QColor("#000000"))
                table.setItem(row, col, item)

        table.resizeColumnsToContents()
        table.setColumnWidth(4, 300)  # Rationale
        table.setColumnWidth(5, 220)  # Stock Play
        table.setColumnWidth(6, 200)  # Options Play
        table.setWordWrap(True)
        table.resizeRowsToContents()
        layout.addWidget(table, stretch=3)

        # Portfolio notes + hidden gem
        notes = result.get("portfolio_notes", "")
        gem   = result.get("hidden_gem")
        notes_text = notes
        if gem:
            notes_text += f"\n\nHidden Gem: {gem}"
        notes_box = QTextEdit()
        notes_box.setReadOnly(True)
        notes_box.setPlainText(notes_text)
        notes_box.setFixedHeight(120)
        layout.addWidget(notes_box, stretch=1)

        # Footer: token usage + cost
        input_t  = result.get("input_tokens", 0)
        output_t = result.get("output_tokens", 0)
        cost     = result.get("cost_usd", 0.0)
        footer = QLabel(
            f"Tokens: {input_t:,} in / {output_t:,} out  —  Est. cost: ${cost:.4f}"
        )
        footer.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(footer)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        view_log_btn = QPushButton("View Usage Log")
        view_log_btn.clicked.connect(self._show_usage_log)
        btn_row.addWidget(view_log_btn)

        if self._on_rerun:
            rerun_btn = QPushButton("Re-run Analysis")
            rerun_btn.clicked.connect(self._trigger_rerun)
            btn_row.addWidget(rerun_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def _trigger_rerun(self) -> None:
        self.accept()
        if self._on_rerun:
            self._on_rerun()

    def _show_usage_log(self) -> None:
        path = os.path.join("data", "claude_usage_log.json")
        if not os.path.exists(path):
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.information(self, "Usage Log", "No usage log found yet.")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception as exc:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Error", f"Could not read usage log: {exc}")
            return

        total_cost = sum(e.get("cost_usd", 0) for e in log)
        lines = [f"{'Timestamp':<25} {'Model':<30} {'In':>7} {'Out':>7} {'Cost':>8} Trigger"]
        lines.append("-" * 95)
        for e in log[-50:]:  # show last 50 entries
            lines.append(
                f"{e.get('ts',''):<25} {e.get('model',''):<30} "
                f"{e.get('input_tokens',0):>7,} {e.get('output_tokens',0):>7,} "
                f"${e.get('cost_usd',0):>7.4f} {e.get('trigger','')}"
            )
        lines.append("-" * 95)
        lines.append(f"Total cost across {len(log)} calls: ${total_cost:.4f}")

        dlg = QDialog(self)
        dlg.setWindowTitle("Claude API Usage Log")
        dlg.setMinimumSize(900, 400)
        vl = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setReadOnly(True)
        te.setFontFamily("Courier New")
        te.setPlainText("\n".join(lines))
        vl.addWidget(te)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        vl.addWidget(close)
        dlg.exec_()
