from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLabel, QLineEdit, QMessageBox, QVBoxLayout,
)

from core.models import StockEntry


class AddEditDialog(QDialog):
    def __init__(self, entry: Optional[StockEntry] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Stock" if entry else "Add Stock")
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._symbol_edit = QLineEdit()
        self._symbol_edit.setPlaceholderText("e.g. AAPL")
        self._symbol_edit.setMaxLength(10)
        form.addRow("Symbol:", self._symbol_edit)

        self._low_spin = QDoubleSpinBox()
        self._low_spin.setRange(0.01, 999999.99)
        self._low_spin.setDecimals(2)
        self._low_spin.setPrefix("$")
        form.addRow("Low Target (Buy):", self._low_spin)

        self._high_spin = QDoubleSpinBox()
        self._high_spin.setRange(0.01, 999999.99)
        self._high_spin.setDecimals(2)
        self._high_spin.setPrefix("$")
        form.addRow("High Target (Sell):", self._high_spin)

        self._notes_edit = QLineEdit()
        self._notes_edit.setPlaceholderText("Optional notes")
        form.addRow("Notes:", self._notes_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if entry:
            self._symbol_edit.setText(entry.symbol)
            self._symbol_edit.setReadOnly(True)  # symbol cannot be changed during edit
            self._low_spin.setValue(entry.low_target)
            self._high_spin.setValue(entry.high_target)
            self._notes_edit.setText(entry.notes)
        else:
            self._low_spin.setValue(100.0)
            self._high_spin.setValue(200.0)

    def _validate_and_accept(self) -> None:
        symbol = self._symbol_edit.text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Validation", "Symbol cannot be empty.")
            return
        if self._low_spin.value() >= self._high_spin.value():
            QMessageBox.warning(self, "Validation", "Low target must be less than high target.")
            return
        self.accept()

    def get_entry(self) -> StockEntry:
        return StockEntry(
            symbol=self._symbol_edit.text().strip().upper(),
            low_target=self._low_spin.value(),
            high_target=self._high_spin.value(),
            notes=self._notes_edit.text().strip(),
        )
