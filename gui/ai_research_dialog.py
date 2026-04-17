"""
AIResearchDialog — Non-blocking QDialog that shows a loading state while
AIResearcher fetches and analyzes news, then renders structured results.
"""
from datetime import datetime
from typing import Any, Dict, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QSizePolicy, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from core.ai_researcher import AIResearcher
from core.scan_result import ScanResult


def render_research_html(data: Dict[str, Any]) -> str:
    """Build the research content HTML string for display in a QTextBrowser.

    Shared by AIResearchDialog and the inline ResearchPanel in SmartScannerPanel.
    Does not include the sentiment badge or footer — those are rendered as separate
    widgets in each consumer.
    """
    html_parts = []
    if data.get("summary"):
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Summary</h3>"
            f"<p>{data['summary']}</p>"
        )
    if data.get("short_term"):
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Short-Term Outlook</h3>"
            f"<p>{data['short_term']}</p>"
        )
    if data.get("long_term"):
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Long-Term Outlook</h3>"
            f"<p>{data['long_term']}</p>"
        )
    if data.get("catalysts"):
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Key Catalysts</h3>"
            f"<p>{data['catalysts']}</p>"
        )

    direction = data.get("direction", "")
    timeframe = data.get("timeframe", "")
    if direction or timeframe:
        dir_colors = {"UP": "#155724", "DOWN": "#721c24", "SIDEWAYS": "#383d41"}
        dir_bg     = {"UP": "#d4edda", "DOWN": "#f8d7da", "SIDEWAYS": "#e2e3e5"}
        d_color = dir_colors.get(direction, "#383d41")
        d_bg    = dir_bg.get(direction, "#e2e3e5")
        dir_html = (
            f"<span style='background:{d_bg}; color:{d_color}; "
            f"padding:2px 8px; border-radius:4px; font-weight:bold;'>"
            f"{direction}</span>"
        )
        tf_html = f"  —  {timeframe}" if timeframe else ""
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Direction & Timeframe</h3>"
            f"<p>{dir_html}{tf_html}</p>"
        )

    cong_signal = data.get("congressional_signal", "NONE").upper()
    if cong_signal and cong_signal != "NONE":
        cong_colors = {"BULLISH": "#155724", "BEARISH": "#721c24", "NEUTRAL": "#383d41"}
        cong_bg     = {"BULLISH": "#d4edda",  "BEARISH": "#f8d7da",  "NEUTRAL": "#e2e3e5"}
        cong_word = cong_signal.split()[0] if cong_signal.split() else cong_signal
        c_color = cong_colors.get(cong_word, "#383d41")
        c_bg    = cong_bg.get(cong_word, "#fff3cd")
        badge = (
            f"<span style='background:{c_bg}; color:{c_color}; "
            f"padding:2px 8px; border-radius:4px; font-weight:bold;'>"
            f"{cong_word}</span>"
        )
        rest = cong_signal[len(cong_word):].strip(" —-:")
        cong_text = f"{badge}  {rest}" if rest else badge
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Congressional Signal</h3>"
            f"<p>{cong_text}</p>"
        )

    if data.get("stock_strategy"):
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Stock Strategy</h3>"
            f"<p>{data['stock_strategy']}</p>"
        )
    if data.get("options_strategy"):
        html_parts.append(
            f"<h3 style='margin-bottom:4px;'>Options Strategy</h3>"
            f"<p>{data['options_strategy']}</p>"
        )

    return "<br>".join(html_parts)


class AIResearchDialog(QDialog):
    # Emitted when research completes successfully (carries the symbol)
    research_complete = pyqtSignal(str)

    def __init__(
        self,
        symbol: str,
        scan_result: Optional[ScanResult],
        settings: Dict[str, Any],
        parent=None,
    ):
        super().__init__(parent)
        self._symbol      = symbol
        self._scan_result = scan_result
        self._settings    = settings
        self._thread: Optional[AIResearcher] = None

        self.setWindowTitle(f"AI Research — {symbol}")
        self.setModal(False)
        self.setMinimumSize(620, 520)

        self._setup_ui()
        self._start_research(force_refresh=False)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # Title bar
        title = QLabel(f"<b>AI Research: {self._symbol}</b>")
        title.setStyleSheet("font-size: 14px;")
        outer.addWidget(title)

        # Stacked: page 0 = loading, page 1 = results, page 2 = error
        self._stack = QStackedWidget()
        outer.addWidget(self._stack, stretch=1)

        # Page 0 — loading
        loading_page = QWidget()
        ll = QVBoxLayout(loading_page)
        ll.setAlignment(Qt.AlignCenter)
        self._loading_label = QLabel("Fetching news and running AI analysis…")
        self._loading_label.setAlignment(Qt.AlignCenter)
        ll.addWidget(self._loading_label)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setFixedWidth(300)
        ll.addWidget(self._progress, alignment=Qt.AlignCenter)
        self._stack.addWidget(loading_page)

        # Page 1 — results
        results_page = QWidget()
        rl = QVBoxLayout(results_page)

        self._sentiment_label = QLabel()
        self._sentiment_label.setAlignment(Qt.AlignCenter)
        self._sentiment_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; padding: 4px 12px;"
            "border-radius: 6px;"
        )
        rl.addWidget(self._sentiment_label)

        self._results_browser = QTextBrowser()
        self._results_browser.setOpenExternalLinks(False)
        rl.addWidget(self._results_browser, stretch=1)

        self._footer_label = QLabel()
        self._footer_label.setStyleSheet("color: gray; font-size: 11px;")
        self._footer_label.setAlignment(Qt.AlignRight)
        rl.addWidget(self._footer_label)

        self._stack.addWidget(results_page)

        # Page 2 — error
        error_page = QWidget()
        el = QVBoxLayout(error_page)
        el.setAlignment(Qt.AlignCenter)
        self._error_label = QLabel()
        self._error_label.setAlignment(Qt.AlignCenter)
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #c0392b; font-size: 12px;")
        el.addWidget(self._error_label)
        self._stack.addWidget(error_page)

        # Button row
        btn_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(lambda: self._start_research(force_refresh=True))
        btn_row.addWidget(self._refresh_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        self._stack.setCurrentIndex(0)

    # ── Research ──────────────────────────────────────────────────────────────

    def _start_research(self, force_refresh: bool = False) -> None:
        self._stop_thread()
        self._stack.setCurrentIndex(0)
        self._refresh_btn.setEnabled(False)

        self._thread = AIResearcher(
            symbol=self._symbol,
            scan_result=self._scan_result,
            settings=self._settings,
            force_refresh=force_refresh,
        )
        self._thread.research_complete.connect(self._on_complete)
        self._thread.research_error.connect(self._on_error)
        self._thread.start()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.stop()
            if self._thread.isRunning():
                self._thread.wait(3000)
            self._thread = None

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_complete(self, data: Dict[str, Any]) -> None:
        sentiment = data.get("sentiment", "NEUTRAL").upper()
        _sentiment_styles = {
            "BULLISH": ("BULLISH", "background: #d4edda; color: #155724;"),
            "BEARISH": ("BEARISH", "background: #f8d7da; color: #721c24;"),
        }
        text, style = _sentiment_styles.get(sentiment, ("NEUTRAL", "background: #e2e3e5; color: #383d41;"))
        self._sentiment_label.setText(text)
        self._sentiment_label.setStyleSheet(
            f"font-size: 15px; font-weight: bold; padding: 4px 16px;"
            f"border-radius: 6px; {style}"
        )

        self._results_browser.setHtml(render_research_html(data))

        source = data.get("source", "")
        ts_str = data.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = ts_str
        self._footer_label.setText(f"Source: {source}  |  Generated: {ts}")

        self._stack.setCurrentIndex(1)
        self._refresh_btn.setEnabled(True)
        self.research_complete.emit(self._symbol)

    def _on_error(self, msg: str) -> None:
        self._error_label.setText(msg)
        self._stack.setCurrentIndex(2)
        self._refresh_btn.setEnabled(True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._stop_thread()
        super().closeEvent(event)
