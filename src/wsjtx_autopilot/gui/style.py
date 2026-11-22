"""Central dark theme for the main window.

Kept separate from widget construction so the stylesheet can be reviewed and
tested without instantiating Qt widgets.
"""

from __future__ import annotations

from typing import Iterable

MAIN_WINDOW_STYLE = """
            QMainWindow, QWidget { background: #0b1116; color: #d8e1e8; font-family: 'Segoe UI'; }
            #appTitle { color: #f2f6f8; font-size: 22px; font-weight: 800; letter-spacing: 3px; }
            #subtitle, #panelTitle { color: #6f8798; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
            #panel, #qsoPanel { background: #101921; border: 1px solid #20303b; border-radius: 5px; }
            QTableWidget { background: #0d151b; alternate-background-color: #111d25; border: 0; gridline-color: #1c2a34; }
            QHeaderView::section { background: #14212a; color: #8299a8; border: 0; border-bottom: 1px solid #2b3d48; padding: 7px; font-weight: 700; }
            QTableWidget::item { padding: 5px; }
            QPushButton { background: #17242d; border: 1px solid #334651; border-radius: 4px; padding: 7px 11px; font-weight: 700; }
            QPushButton:hover { background: #20323e; }
            #updateButton[available='true'] { color: #efbd67; border-color: #8c6936; background: #2a2115; }
            #armButton { color: #efbd67; border-color: #8c6936; min-width: 115px; }
            #armButton[armed='true'] { background: #65262a; color: #ffffff; border-color: #df5a60; }
            #statusPill { background: #222b31; color: #92a1aa; border-radius: 10px; padding: 5px 10px; font-weight: 700; }
            #statusPill[online='true'] { background: #173f37; color: #70dfbd; }
            #warningText { color: #efbd67; background: #2a2115; border: 1px solid #6f522b; padding: 8px; }
            #adaptiveAvailable { color: #70dfbd; font-weight: 700; }
            #adaptiveUnavailable { color: #efbd67; font-weight: 700; }
            #comingSoon { color: #78c8ff; font-size: 28px; font-weight: 800; }
            #opportunityStatus { color: #8dccff; font-weight: 800; letter-spacing: 0.5px; }
            #infoText { color: #8299a8; padding: 5px; }
            #qsoState { color: #70dfbd; font-size: 15px; font-weight: 800; padding: 2px 8px; }
            #antennaSafety { border-radius: 12px; padding: 6px 10px; font-weight: 900; }
            #antennaSafety[level='ok'] { background: #173b63; color: #8dccff; border: 1px solid #3479b9; }
            #antennaSafety[level='warning'] { background: #59420f; color: #ffd76a; border: 1px solid #b78718; }
            #antennaSafety[level='fault'] { background: #671e24; color: #ffffff; border: 1px solid #e34f5b; }
            QPlainTextEdit { background: #0d151b; border: 1px solid #20303b; color: #c8d2d9; padding: 4px; }
            QLineEdit, QSpinBox, QComboBox { background: #111b22; border: 1px solid #30424d; border-radius: 3px; padding: 6px; }
            QGroupBox { border: 1px solid #263640; border-radius: 4px; margin-top: 10px; padding: 8px; font-weight: 700; }
            QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; color: #9db0bc; }
            QCheckBox:disabled, QLabel:disabled { color: #53636d; }
            QSpinBox:disabled, QComboBox:disabled, QLineEdit:disabled { color: #53636d; background: #0e151a; border-color: #202c33; }
            QTabWidget::pane { border: 1px solid #263640; }
            QTabBar::tab { background: #111b22; padding: 9px 16px; }
            QTabBar::tab:selected { background: #1b2b35; color: #70dfbd; }
            """


def apply_main_window_style(window) -> None:
    window.setStyleSheet(MAIN_WINDOW_STYLE)


def fit_control_buttons(buttons: Iterable) -> None:
    for button in buttons:
        button.setMinimumWidth(button.sizeHint().width())
