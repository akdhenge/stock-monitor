import sys
import os

# Ensure the project root is on sys.path so all imports work regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from gui.main_window import MainWindow

FLAT_STYLESHEET = """
/* ── App-wide ─────────────────────────────────────────────── */
QWidget {
    background-color: #f5f5f5;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
    color: #1a1a1a;
}

/* ── Menu bar ─────────────────────────────────────────────── */
QMenuBar {
    background-color: #f0f0f0;
    border-bottom: 1px solid #d0d0d0;
}
QMenuBar::item:selected {
    background-color: #cce4f7;
}
QMenu {
    background-color: #ffffff;
    border: 1px solid #c0c0c0;
}
QMenu::item:selected {
    background-color: #cce4f7;
}

/* ── Toolbar ──────────────────────────────────────────────── */
QToolBar {
    background-color: #f0f0f0;
    border-bottom: 1px solid #d0d0d0;
    spacing: 4px;
    padding: 3px 6px;
}

/* ── Buttons ──────────────────────────────────────────────── */
QPushButton {
    background-color: #0078d4;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 5px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #006cbf;
}
QPushButton:pressed {
    background-color: #005a9e;
}
QPushButton:disabled {
    background-color: #b0c4de;
    color: #e0e0e0;
}

/* ── Tab widget ───────────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid #d0d0d0;
    background-color: #f5f5f5;
}
QTabBar::tab {
    background-color: #e8e8e8;
    border: 1px solid #c8c8c8;
    border-bottom: none;
    border-radius: 4px 4px 0 0;
    padding: 6px 18px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #f5f5f5;
    color: #0078d4;
    font-weight: 600;
}
QTabBar::tab:hover:!selected {
    background-color: #d8e8f5;
}

/* ── Tables ───────────────────────────────────────────────── */
QTableWidget {
    background-color: #ffffff;
    gridline-color: #e0e0e0;
    border: 1px solid #d0d0d0;
    selection-background-color: #cce4f7;
    selection-color: #1a1a1a;
    alternate-background-color: #f9f9f9;
}
QHeaderView::section {
    background-color: #f0f0f0;
    border: none;
    border-right: 1px solid #d0d0d0;
    border-bottom: 1px solid #d0d0d0;
    padding: 4px 8px;
    font-weight: 600;
}

/* ── Inputs ───────────────────────────────────────────────── */
QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit {
    background-color: #ffffff;
    border: 1px solid #c0c0c0;
    border-radius: 3px;
    padding: 4px 6px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus {
    border: 1px solid #0078d4;
}

/* ── Progress bar ─────────────────────────────────────────── */
QProgressBar {
    background-color: #e0e0e0;
    border: 1px solid #c0c0c0;
    border-radius: 4px;
    text-align: center;
    height: 16px;
}
QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 3px;
}

/* ── Status bar ───────────────────────────────────────────── */
QStatusBar {
    background-color: #f0f0f0;
    border-top: 1px solid #d0d0d0;
}

/* ── Dialogs ──────────────────────────────────────────────── */
QDialog {
    background-color: #f5f5f5;
}

/* ── Splitter ─────────────────────────────────────────────── */
QSplitter::handle {
    background-color: #d0d0d0;
}
QSplitter::handle:horizontal {
    width: 4px;
}
QSplitter::handle:vertical {
    height: 4px;
}

/* ── Scrollbar ────────────────────────────────────────────── */
QScrollBar:vertical {
    background: #f0f0f0;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #b0b0b0;
    border-radius: 5px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background: #0078d4;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: #f0f0f0;
    height: 10px;
    border: none;
}
QScrollBar::handle:horizontal {
    background: #b0b0b0;
    border-radius: 5px;
    min-width: 20px;
}
QScrollBar::handle:horizontal:hover {
    background: #0078d4;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ── Checkboxes ───────────────────────────────────────────── */
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #c0c0c0;
    border-radius: 2px;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #0078d4;
    border-color: #0078d4;
}
"""


def main():
    # Enable high-DPI scaling for Windows 11
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Stock Monitor")
    app.setOrganizationName("Antika")
    app.setStyleSheet(FLAT_STYLESHEET)

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
