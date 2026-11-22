"""Small pure widgets shared by the settings dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QPushButton, QWidget

from wsjtx_autopilot.config import ActivityPolicy

def _csv_set(value: str) -> set[str]:
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def _activity_policy_combo(selected: ActivityPolicy) -> QComboBox:
    combo = QComboBox()
    combo.addItem("Normal", ActivityPolicy.NORMAL)
    combo.addItem("Priority", ActivityPolicy.PRIORITY)
    combo.addItem("Ignore", ActivityPolicy.IGNORE)
    combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


def _path_picker(parent: QWidget, line_edit: QLineEdit, file_filter: str, save: bool = False) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QPushButton("Browse")

    def browse() -> None:
        start = str(Path(line_edit.text()).parent) if line_edit.text() else ""
        if save:
            selected, _ = QFileDialog.getSaveFileName(parent, "Select file", start, file_filter)
        else:
            selected, _ = QFileDialog.getOpenFileName(parent, "Select file", start, file_filter)
        if selected:
            line_edit.setText(selected)

    button.clicked.connect(browse)
    layout.addWidget(line_edit, 1)
    layout.addWidget(button)
    return widget
