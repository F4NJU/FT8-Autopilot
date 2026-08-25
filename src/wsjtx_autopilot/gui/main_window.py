from dataclasses import replace
import logging
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.config import ActivityPolicy, AppPaths, DirectCallPolicy, SettingsStore, UserSettings
from wsjtx_autopilot.engine.models import EngineEvent, EngineEventKind

from .viewmodels import ActivityRow, CandidateRow, StatusView
from .worker import BackendWorker

LOGGER = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, settings: UserSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AutoPilot Preferences")
        self.setMinimumWidth(560)
        self._settings = settings
        tabs = QTabWidget()
        tabs.addTab(self._station_tab(), "Station & UDP")
        tabs.addTab(self._priority_tab(), "Priority")
        tabs.addTab(self._smart_tx_tab(), "Smart TX")
        tabs.addTab(self._safety_tab(), "Safety & Data")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def settings(self) -> UserSettings:
        minimum_snr = None if self.minimum_snr.value() == -51 else self.minimum_snr.value()
        return replace(
            self._settings,
            local_callsign=self.callsign.text(),
            bind_address=self.bind_address.text(),
            udp_port=self.udp_port.value(),
            preferred_continents=_csv_set(self.continents.text()),
            preferred_dxcc=_csv_set(self.dxcc.text()),
            direct_call_policy=self.direct_policy.currentData(),
            allow_direct_call_dupes=self.direct_dupes.isChecked(),
            allow_dupes=self.allow_dupes.isChecked(),
            minimum_snr=minimum_snr,
            favor_strong_signals=self.strong_signals.isChecked(),
            pota_policy=self.pota_policy.currentData(),
            sota_policy=self.sota_policy.currentData(),
            qrp_policy=self.qrp_policy.currentData(),
            respond_to_cq_dx=self.respond_cq_dx.isChecked(),
            blacklist=_csv_set(self.blacklist.text()),
            ignore_minutes=self.ignore_minutes.value(),
            cty_dat_path=self.cty_path.text().strip() or None,
            wsjtx_log_path=self.adif_path.text().strip() or None,
            sync_adif_on_startup=self.sync_adif.isChecked(),
            direct_reply_patched=self.direct_patched.isChecked(),
            max_no_progress_periods=self.max_no_progress.value(),
            stalled_qso_cooldown_seconds=float(self.stalled_cooldown.value()),
            remote_busy_cooldown_seconds=float(self.remote_busy_cooldown.value()),
            max_remote_cq_during_attempt=self.max_remote_cq.value(),
            remote_returned_to_cq_cooldown_seconds=float(self.remote_cq_cooldown.value()),
            finalization_hold_periods=self.finalization_hold.value(),
            max_final_retries=self.max_final_retries.value(),
            smart_tx_frequency=self.smart_tx_enabled.isChecked(),
            smart_tx_find_free=self.smart_tx_find_free.isChecked(),
            smart_tx_fallback_remote=self.smart_tx_fallback.isChecked(),
            occupied_guard_hz=self.occupied_guard.value(),
            occupancy_history_seconds=float(self.occupancy_history.value()),
            tx_df_min=self.tx_df_min.value(),
            tx_df_max=self.tx_df_max.value(),
            minimum_free_gap_hz=self.minimum_gap.value(),
        )

    def _station_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.callsign = QLineEdit(self._settings.local_callsign)
        self.bind_address = QLineEdit(self._settings.bind_address)
        self.udp_port = QSpinBox()
        self.udp_port.setRange(1, 65_535)
        self.udp_port.setValue(self._settings.udp_port)
        form.addRow("Local callsign", self.callsign)
        form.addRow("UDP bind address", self.bind_address)
        form.addRow("UDP port", self.udp_port)
        return widget

    def _priority_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.continents = QLineEdit(", ".join(sorted(self._settings.preferred_continents)))
        self.dxcc = QLineEdit(", ".join(sorted(self._settings.preferred_dxcc)))
        self.direct_policy = QComboBox()
        self.direct_policy.addItem("Always priority", DirectCallPolicy.ALWAYS_PRIORITY)
        self.direct_policy.addItem("Compete by score", DirectCallPolicy.NORMAL)
        self.direct_policy.addItem("Ignore", DirectCallPolicy.IGNORE)
        self.direct_policy.setCurrentIndex(max(0, self.direct_policy.findData(self._settings.direct_call_policy)))
        self.minimum_snr = QSpinBox()
        self.minimum_snr.setRange(-51, 30)
        self.minimum_snr.setSpecialValueText("Disabled")
        self.minimum_snr.setValue(self._settings.minimum_snr if self._settings.minimum_snr is not None else -51)
        self.strong_signals = QCheckBox("Add a bounded SNR bonus")
        self.strong_signals.setChecked(self._settings.favor_strong_signals)
        self.pota_policy = _activity_policy_combo(self._settings.pota_policy)
        self.sota_policy = _activity_policy_combo(self._settings.sota_policy)
        self.qrp_policy = _activity_policy_combo(self._settings.qrp_policy)
        self.respond_cq_dx = QCheckBox("Respond to relative CQ DX calls")
        self.respond_cq_dx.setChecked(self._settings.respond_to_cq_dx)
        form.addRow("Preferred continents", self.continents)
        form.addRow("Preferred DXCC prefixes", self.dxcc)
        form.addRow("Direct calls", self.direct_policy)
        form.addRow("Minimum SNR", self.minimum_snr)
        form.addRow("Signal scoring", self.strong_signals)
        form.addRow("POTA", self.pota_policy)
        form.addRow("SOTA", self.sota_policy)
        form.addRow("QRP", self.qrp_policy)
        form.addRow("CQ DX", self.respond_cq_dx)
        return widget

    def _safety_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.allow_dupes = QCheckBox("Allow Worked Today duplicates")
        self.allow_dupes.setChecked(self._settings.allow_dupes)
        self.direct_dupes = QCheckBox("Allow duplicates only for direct callers")
        self.direct_dupes.setChecked(self._settings.allow_direct_call_dupes)
        self.direct_patched = QCheckBox("WSJT-X build supports AutoPilot Direct Reply + SetTxDF patch")
        self.direct_patched.setChecked(self._settings.direct_reply_patched)
        self.blacklist = QLineEdit(", ".join(sorted(self._settings.blacklist)))
        self.ignore_minutes = QSpinBox()
        self.ignore_minutes.setRange(1, 24 * 60)
        self.ignore_minutes.setValue(self._settings.ignore_minutes)
        self.max_no_progress = QSpinBox()
        self.max_no_progress.setRange(1, 100)
        self.max_no_progress.setValue(self._settings.max_no_progress_periods)
        self.stalled_cooldown = QSpinBox()
        self.stalled_cooldown.setRange(0, 86_400)
        self.stalled_cooldown.setSuffix(" seconds")
        self.stalled_cooldown.setValue(int(self._settings.stalled_qso_cooldown_seconds))
        self.remote_busy_cooldown = QSpinBox()
        self.remote_busy_cooldown.setRange(0, 86_400)
        self.remote_busy_cooldown.setSuffix(" seconds")
        self.remote_busy_cooldown.setValue(int(self._settings.remote_busy_cooldown_seconds))
        self.max_remote_cq = QSpinBox()
        self.max_remote_cq.setRange(1, 10)
        self.max_remote_cq.setValue(self._settings.max_remote_cq_during_attempt)
        self.remote_cq_cooldown = QSpinBox()
        self.remote_cq_cooldown.setRange(0, 86_400)
        self.remote_cq_cooldown.setSuffix(" seconds")
        self.remote_cq_cooldown.setValue(int(self._settings.remote_returned_to_cq_cooldown_seconds))
        self.finalization_hold = QSpinBox()
        self.finalization_hold.setRange(1, 5)
        self.finalization_hold.setSuffix(" RX period(s)")
        self.finalization_hold.setValue(self._settings.finalization_hold_periods)
        self.max_final_retries = QSpinBox()
        self.max_final_retries.setRange(0, 10)
        self.max_final_retries.setValue(self._settings.max_final_retries)
        self.cty_path = QLineEdit(self._settings.cty_dat_path or "")
        default_adif = AppPaths.from_environment().default_adif_path
        self.adif_path = QLineEdit(self._settings.wsjtx_log_path or str(default_adif))
        self.sync_adif = QCheckBox("Synchronize ADIF at startup")
        self.sync_adif.setChecked(self._settings.sync_adif_on_startup)
        form.addRow(self.allow_dupes)
        form.addRow(self.direct_dupes)
        form.addRow(self.direct_patched)
        form.addRow("Blacklist", self.blacklist)
        form.addRow("Temporary ignore (minutes)", self.ignore_minutes)
        form.addRow("Max periods without progress", self.max_no_progress)
        form.addRow("Stalled QSO cooldown", self.stalled_cooldown)
        form.addRow("Remote busy cooldown", self.remote_busy_cooldown)
        form.addRow("Remote CQ attempt limit", self.max_remote_cq)
        form.addRow("Remote returned-to-CQ cooldown", self.remote_cq_cooldown)
        form.addRow("Finalization hold", self.finalization_hold)
        form.addRow("Maximum final 73 retries", self.max_final_retries)
        form.addRow("Offline cty.dat", _path_picker(self, self.cty_path, "CTY data (*.dat);;All files (*)"))
        form.addRow("WSJT-X ADIF log", _path_picker(self, self.adif_path, "ADIF (*.adi *.adif);;All files (*)"))
        form.addRow(self.sync_adif)
        return widget

    def _smart_tx_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.smart_tx_enabled = QCheckBox("Smart TX Frequency")
        self.smart_tx_enabled.setChecked(self._settings.smart_tx_frequency)
        self.smart_tx_find_free = QRadioButton("Find a free decode-occupancy slot")
        self.smart_tx_remote = QRadioButton("Always reply on the correspondent frequency")
        self.smart_tx_find_free.setChecked(self._settings.smart_tx_find_free)
        self.smart_tx_remote.setChecked(not self._settings.smart_tx_find_free)
        self.smart_tx_fallback = QCheckBox("If no suitable gap: TX on caller frequency")
        self.smart_tx_fallback.setChecked(self._settings.smart_tx_fallback_remote)
        self.occupied_guard = _hz_spin(0, 500, self._settings.occupied_guard_hz)
        self.occupancy_history = QSpinBox()
        self.occupancy_history.setRange(1, 300)
        self.occupancy_history.setSuffix(" seconds")
        self.occupancy_history.setValue(int(self._settings.occupancy_history_seconds))
        self.tx_df_min = _hz_spin(0, 5000, self._settings.tx_df_min)
        self.tx_df_max = _hz_spin(1, 5000, self._settings.tx_df_max)
        self.minimum_gap = _hz_spin(1, 1000, self._settings.minimum_free_gap_hz)
        note = QLabel("Decode occupancy only; undecoded carriers are not visible.")
        note.setWordWrap(True)
        form.addRow(self.smart_tx_enabled)
        form.addRow("Mode", self.smart_tx_find_free)
        form.addRow("", self.smart_tx_remote)
        form.addRow("Fallback", self.smart_tx_fallback)
        form.addRow("Occupied guard", self.occupied_guard)
        form.addRow("Recent history", self.occupancy_history)
        form.addRow("Minimum Tx DF", self.tx_df_min)
        form.addRow("Maximum Tx DF", self.tx_df_max)
        form.addRow("Minimum free gap", self.minimum_gap)
        form.addRow(note)
        return widget


class MainWindow(QMainWindow):
    apply_settings_requested = Signal(object)
    arm_requested = Signal()
    disarm_requested = Signal(str)
    ignore_requested = Signal(str)
    stop_requested = Signal()

    def __init__(self, settings_store: SettingsStore | None = None) -> None:
        super().__init__()
        self._store = settings_store or SettingsStore()
        self._settings = self._store.load()
        self._armed = False
        self._candidate_rows: dict[str, int] = {}
        self.setWindowTitle("WSJTX AutoPilot")
        self.resize(1280, 780)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._apply_style()
        self._thread = QThread(self)
        self._worker = BackendWorker(self._settings)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self.stop_requested.connect(self._worker.stop)
        self.apply_settings_requested.connect(self._worker.apply_settings)
        self.arm_requested.connect(self._worker.arm)
        self.disarm_requested.connect(self._worker.disarm)
        self.ignore_requested.connect(self._worker.ignore_station)
        self._worker.connection_changed.connect(self._show_connection)
        self._worker.armed_changed.connect(self._show_armed)
        self._worker.activity_received.connect(self._add_activity)
        self._worker.engine_event.connect(self._handle_engine_event)
        self._worker.status_changed.connect(self._show_status)
        self._worker.qso_changed.connect(self._show_qso)
        self._worker.error.connect(self._show_error)
        self._worker.stopped.connect(self._thread.quit)
        self._thread.start()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("AUTOPILOT")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"{self._settings.local_callsign}  /  FT8 OPERATIONS CONSOLE")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.connection = QLabel("OFFLINE")
        self.connection.setObjectName("statusPill")
        self.frequency = QLabel("-")
        self.mode = QLabel("-")
        settings_button = QPushButton("PREFERENCES")
        settings_button.clicked.connect(self._edit_settings)
        self.arm_button = QPushButton("ARM CONTROL")
        self.arm_button.setObjectName("armButton")
        self.arm_button.clicked.connect(self._toggle_arm)
        header.addWidget(self.connection)
        header.addWidget(self.frequency)
        header.addWidget(self.mode)
        header.addWidget(settings_button)
        header.addWidget(self.arm_button)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._activity_panel())
        splitter.addWidget(self._candidate_panel())
        splitter.setSizes([700, 500])
        layout.addWidget(splitter, 1)

        qso = QFrame()
        qso.setObjectName("qsoPanel")
        qso_layout = QHBoxLayout(qso)
        qso_layout.addWidget(QLabel("QSO STATE"))
        self.qso_state = QLabel("IDLE")
        self.qso_state.setObjectName("qsoState")
        self.qso_remote = QLabel("REMOTE  -")
        self.qso_remote_detail = QLabel("-")
        self.qso_stage = QLabel("STAGE  -")
        self.qso_progress = QLabel(f"NO PROGRESS  0 / {self._settings.max_no_progress_periods}")
        self.remote_cq_progress = QLabel(f"REMOTE CQ  0 / {self._settings.max_remote_cq_during_attempt}")
        self.qso_notice = QLabel("")
        self.qso_rx_df = QLabel("RX  -")
        self.qso_tx_df = QLabel("TX  -")
        self.qso_smart_tx = QLabel("Smart TX  -")
        self.tx_state = QLabel("RX")
        qso_layout.addWidget(self.qso_state)
        qso_layout.addWidget(self.qso_remote)
        qso_layout.addWidget(self.qso_remote_detail)
        qso_layout.addWidget(self.qso_stage)
        qso_layout.addWidget(self.qso_progress)
        qso_layout.addWidget(self.remote_cq_progress)
        qso_layout.addWidget(self.qso_notice)
        qso_layout.addWidget(self.qso_rx_df)
        qso_layout.addWidget(self.qso_tx_df)
        qso_layout.addWidget(self.qso_smart_tx)
        qso_layout.addStretch()
        qso_layout.addWidget(self.tx_state)
        layout.addWidget(qso)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Waiting for WSJT-X UDP traffic")

    def _activity_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        label = QLabel("DECODE ACTIVITY")
        label.setObjectName("panelTitle")
        self.activity = QTableWidget(0, 7)
        self.activity.setHorizontalHeaderLabels(["UTC", "SNR", "DF", "MODE", "TARGET", "ACTIVITY", "MESSAGE"])
        self.activity.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.activity.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.activity.verticalHeader().hide()
        self.activity.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(label)
        layout.addWidget(self.activity)
        return panel

    def _candidate_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        label = QLabel("CANDIDATE RANKING")
        label.setObjectName("panelTitle")
        self.candidates = QTableWidget(0, 9)
        self.candidates.setHorizontalHeaderLabels(["CALL", "TYPE", "SNR", "DXCC", "CONT", "CQ TARGET", "ACTIVITY", "SCORE", "WHY"])
        self.candidates.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidates.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidates.verticalHeader().hide()
        self.candidates.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.candidates.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        ignore = QAction(f"Ignore for {self._settings.ignore_minutes} minutes", self.candidates)
        ignore.triggered.connect(self._ignore_selected)
        blacklist = QAction("Add to blacklist", self.candidates)
        blacklist.triggered.connect(self._blacklist_selected)
        self.candidates.addAction(ignore)
        self.candidates.addAction(blacklist)
        layout.addWidget(label)
        layout.addWidget(self.candidates)
        return panel

    def _add_activity(self, row: ActivityRow) -> None:
        target = 0
        self.activity.insertRow(target)
        values = [
            row.observed_at.strftime("%H:%M:%S"),
            f"{row.snr:+d}",
            str(row.delta_frequency),
            row.mode,
            row.cq_target or "-",
            ", ".join(row.activity_tags) or "-",
            row.message,
        ]
        for column, value in enumerate(values):
            self.activity.setItem(target, column, QTableWidgetItem(value))
        while self.activity.rowCount() > 250:
            self.activity.removeRow(self.activity.rowCount() - 1)

    def _handle_engine_event(self, event: EngineEvent) -> None:
        row = CandidateRow.from_engine_event(event)
        if event.kind is EngineEventKind.CANDIDATE_REFUSED:
            self.statusBar().showMessage(f"Refused {event.station or '-'}: {event.reason}", 5000)
            return
        if row is None:
            return
        if event.kind is EngineEventKind.CANDIDATE_ADDED:
            kind = "DIRECT CALL / priority override" if row.kind == "DIRECT_CALLER" else row.kind
            self.statusBar().showMessage(
                f"Candidate {row.station} ({kind}) - waiting for decode window..."
            )
        target = self._candidate_rows.get(row.station)
        if target is None:
            target = self.candidates.rowCount()
            self.candidates.insertRow(target)
            self._candidate_rows[row.station] = target
        dxcc = f"{row.country} / {row.dxcc}"
        values = [
            row.station,
            row.kind,
            f"{row.snr:+d}",
            dxcc,
            row.continent,
            row.cq_target,
            row.activities,
            str(row.score),
            row.score_detail,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if event.kind is EngineEventKind.CANDIDATE_SELECTED:
                item.setBackground(QColor("#173f37"))
            self.candidates.setItem(target, column, item)
        if event.kind is EngineEventKind.CANDIDATE_SELECTED:
            self.statusBar().showMessage(f"Selected {row.station}: {row.score_detail}", 8000)

    def _show_connection(self, connected: bool) -> None:
        self.connection.setText("WSJT-X ONLINE" if connected else "OFFLINE")
        self.connection.setProperty("online", connected)
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)

    def _show_armed(self, armed: bool) -> None:
        self._armed = armed
        self.arm_button.setText("DISARM NOW" if armed else "ARM CONTROL")
        self.arm_button.setProperty("armed", armed)
        self.arm_button.style().unpolish(self.arm_button)
        self.arm_button.style().polish(self.arm_button)

    def _show_status(self, status: StatusView) -> None:
        self.frequency.setText(status.frequency)
        self.mode.setText(status.mode)
        self.tx_state.setText(status.tx_state)

    def _show_qso(
        self,
        state: str,
        remote: str,
        remote_detail: str,
        stage: str,
        count: int,
        maximum: int,
        remote_cq_count: int,
        remote_cq_maximum: int,
        notice: str,
        rx_df: str,
        tx_df: str,
        smart_tx: str,
    ) -> None:
        self.qso_state.setText(state)
        self.qso_remote.setText(f"REMOTE  {remote}")
        self.qso_remote_detail.setText(remote_detail)
        self.qso_stage.setText(f"STAGE  {stage}")
        self.qso_progress.setText(f"NO PROGRESS  {count} / {maximum}")
        self.remote_cq_progress.setText(f"REMOTE CQ  {remote_cq_count} / {remote_cq_maximum}")
        self.qso_notice.setText(notice)
        self.qso_rx_df.setText(rx_df)
        self.qso_tx_df.setText(tx_df)
        self.qso_smart_tx.setText(smart_tx)

    def _toggle_arm(self) -> None:
        if self._armed:
            self.disarm_requested.emit("operator pressed DISARM")
            return
        answer = QMessageBox.warning(
            self,
            "Arm automatic control?",
            "Armed control can send WSJT-X Reply packets and may initiate transmission.\n\n"
            "Control remains armed until DISARM, connection loss, shutdown, or a safety fault. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        LOGGER.info("[GUI] ARM confirmation result=%r", answer)
        if answer == QMessageBox.StandardButton.Yes:
            LOGGER.info("[GUI] ARM requested by operator")
            self.arm_requested.emit()

    def _edit_settings(self) -> None:
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._settings = dialog.settings()
        self._store.save(self._settings)
        self.apply_settings_requested.emit(self._settings)
        self.statusBar().showMessage("Preferences saved; backend restarted DISARMED", 6000)

    def _selected_station(self) -> str | None:
        row = self.candidates.currentRow()
        item = self.candidates.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def _ignore_selected(self) -> None:
        station = self._selected_station()
        if station:
            self.ignore_requested.emit(station)
            self.statusBar().showMessage(f"Ignoring {station} for {self._settings.ignore_minutes} minutes", 5000)

    def _blacklist_selected(self) -> None:
        station = self._selected_station()
        if not station:
            return
        self._settings.blacklist.add(station)
        self._store.save(self._settings)
        self.apply_settings_requested.emit(self._settings)
        self.statusBar().showMessage(f"Blacklisted {station}; backend restarted DISARMED", 5000)

    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage(message)
        QMessageBox.critical(self, "AutoPilot backend error", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.stop_requested.emit()
        if not self._thread.wait(2_000):
            self._thread.quit()
            self._thread.wait(500)
        event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0b1116; color: #d8e1e8; font-family: 'Segoe UI'; }
            #appTitle { color: #f2f6f8; font-size: 25px; font-weight: 800; letter-spacing: 3px; }
            #subtitle, #panelTitle { color: #6f8798; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
            #panel, #qsoPanel { background: #101921; border: 1px solid #20303b; border-radius: 5px; }
            QTableWidget { background: #0d151b; alternate-background-color: #111d25; border: 0; gridline-color: #1c2a34; }
            QHeaderView::section { background: #14212a; color: #8299a8; border: 0; border-bottom: 1px solid #2b3d48; padding: 7px; font-weight: 700; }
            QTableWidget::item { padding: 5px; }
            QPushButton { background: #17242d; border: 1px solid #334651; border-radius: 4px; padding: 9px 14px; font-weight: 700; }
            QPushButton:hover { background: #20323e; }
            #armButton { color: #efbd67; border-color: #8c6936; min-width: 115px; }
            #armButton[armed='true'] { background: #65262a; color: #ffffff; border-color: #df5a60; }
            #statusPill { background: #222b31; color: #92a1aa; border-radius: 10px; padding: 5px 10px; font-weight: 700; }
            #statusPill[online='true'] { background: #173f37; color: #70dfbd; }
            #qsoState { color: #70dfbd; font-size: 16px; font-weight: 800; padding: 4px 14px; }
            QLineEdit, QSpinBox, QComboBox { background: #111b22; border: 1px solid #30424d; border-radius: 3px; padding: 6px; }
            QTabWidget::pane { border: 1px solid #263640; }
            QTabBar::tab { background: #111b22; padding: 9px 16px; }
            QTabBar::tab:selected { background: #1b2b35; color: #70dfbd; }
            """
        )


def _csv_set(value: str) -> set[str]:
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def _activity_policy_combo(selected: ActivityPolicy) -> QComboBox:
    combo = QComboBox()
    combo.addItem("Normal", ActivityPolicy.NORMAL)
    combo.addItem("Priority", ActivityPolicy.PRIORITY)
    combo.addItem("Ignore", ActivityPolicy.IGNORE)
    combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


def _hz_spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSuffix(" Hz")
    spin.setValue(value)
    return spin


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
