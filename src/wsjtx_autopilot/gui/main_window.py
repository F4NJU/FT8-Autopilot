from dataclasses import replace
import logging
from pathlib import Path

from PySide6.QtCore import QSize, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QDesktopServices, QGuiApplication, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.config import (
    ActivityPolicy,
    AppPaths,
    DirectCallPolicy,
    SettingsStore,
    UserSettings,
)
from wsjtx_autopilot.engine.models import EngineEvent, EngineEventKind
from wsjtx_autopilot.logging_setup import (
    current_logging_session,
    export_diagnostic,
    set_file_log_level,
)

from .viewmodels import ActivityRow, CandidateRow, StatusView
from .worker import BackendWorker

LOGGER = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    settings_applied = Signal(object)

    def __init__(self, settings: UserSettings, parent: QWidget | None = None, ap1_available: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("AutoPilot Preferences")
        available = self._available_size(parent)
        self.setMinimumSize(min(650, available.width()), min(450, available.height()))
        self.resize(self._size_for_available_geometry(available))
        self._settings = settings
        self._ap1_available = ap1_available
        self.tabs = QTabWidget()
        self.tabs.addTab(self._station_tab(), "Station & UDP")
        self.tabs.addTab(self._priority_tab(), "Priority")
        safety_tab = self._safety_tab()
        self.tabs.addTab(safety_tab, "Safety & Data")
        self.tabs.addTab(self._ftx1_tab(), "FTX-1 CAT-2")
        self.tabs.addTab(self._diagnostic_tab(), "Diagnostic")
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setWidget(self.tabs)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        layout = QVBoxLayout(self)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.buttons)

    @staticmethod
    def _available_size(parent: QWidget | None) -> QSize:
        screen = parent.screen() if parent is not None and parent.screen() is not None else QGuiApplication.primaryScreen()
        return screen.availableGeometry().size() if screen is not None else QSize(750, 700)

    @staticmethod
    def _size_for_available_geometry(available: QSize) -> QSize:
        return QSize(min(750, available.width()), min(700, available.height()))

    def settings(self) -> UserSettings:
        minimum_snr = None if self.minimum_snr.value() == -51 else self.minimum_snr.value()
        return replace(
            self._settings,
            local_callsign=self.callsign.text(),
            bind_address=self.bind_address.text(),
            udp_port=self.udp_port.value(),
            preferred_continents=_csv_set(self.continents.text()),
            preferred_dxcc=_csv_set(self.dxcc.text()),
            direct_call_policy=DirectCallPolicy(self.direct_policy.currentData()),
            allow_direct_call_dupes=self.direct_dupes.isChecked(),
            allow_dupes=self.allow_dupes.isChecked(),
            minimum_snr=minimum_snr,
            favor_strong_signals=self.strong_signals.isChecked(),
            pota_policy=ActivityPolicy(self.pota_policy.currentData()),
            sota_policy=ActivityPolicy(self.sota_policy.currentData()),
            qrp_policy=ActivityPolicy(self.qrp_policy.currentData()),
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
            final_tx_timeout_periods=self.final_tx_timeout.value(),
            max_final_retries=self.max_final_retries.value(),
            adaptive_operation_enabled=self.adaptive_enabled.isChecked(),
            stagnation_attempt_window=self.stagnation_window.value(),
            stagnation_min_failed_attempts=self.stagnation_failed.value(),
            stagnation_max_unique_calls=self.stagnation_unique.value(),
            adaptive_parity_enabled=self.adaptive_parity.isChecked(),
            parity_trial_failed_attempts=self.parity_trial.value(),
            automatic_band_hopping_enabled=self.auto_band_hop.isChecked(),
            allowed_auto_hop_bands=[band for band, checkbox in self.auto_band_checkboxes.items() if checkbox.isChecked()],
            minimum_band_dwell_minutes=float(self.band_dwell.value()),
            ftx1_cat2_enabled=self.ftx1_cat2_enabled.isChecked(),
            ftx1_cat2_confirmed_ftx1=self.ftx1_cat2_confirmed_ftx1.isChecked(),
            ftx1_cat2_port=self.ftx1_cat2_port.text(),
            ftx1_cat2_baudrate=self.ftx1_cat2_baudrate.value(),
            ftx1_cat2_timeout_seconds=self.ftx1_cat2_timeout.value() / 1000,
            ftx1_auto_apply_band_profiles=self.ftx1_auto_apply_band_profiles.isChecked(),
            ftx1_band_profiles={
                band: {
                    "usb_mod_gain": int(gain.text()),
                    "tx_audio_attenuation": int(attenuation.text()),
                }
                for band, (gain, attenuation) in self.ftx1_profile_fields.items()
                if gain.text() and attenuation.text()
            },
            logging_level=self.logging_level.currentData(),
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
        self.direct_patched = QCheckBox("WSJT-X build supports AutoPilot Direct Reply")
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
        self.final_tx_timeout = QSpinBox()
        self.final_tx_timeout.setRange(1, 10)
        self.final_tx_timeout.setSuffix(" period(s)")
        self.final_tx_timeout.setValue(self._settings.final_tx_timeout_periods)
        self.max_final_retries = QSpinBox()
        self.max_final_retries.setRange(0, 10)
        self.max_final_retries.setValue(self._settings.max_final_retries)
        self.adaptive_enabled = QCheckBox("Enable anti-stagnation")
        self.adaptive_enabled.setChecked(self._settings.adaptive_operation_enabled)
        self.stagnation_window = QSpinBox()
        self.stagnation_window.setRange(1, 50)
        self.stagnation_window.setValue(self._settings.stagnation_attempt_window)
        self.stagnation_failed = QSpinBox()
        self.stagnation_failed.setRange(1, 50)
        self.stagnation_failed.setValue(self._settings.stagnation_min_failed_attempts)
        self.stagnation_unique = QSpinBox()
        self.stagnation_unique.setRange(1, 20)
        self.stagnation_unique.setValue(self._settings.stagnation_max_unique_calls)
        self.adaptive_parity = QCheckBox("Flip TX First/Second")
        self.adaptive_parity.setChecked(self._settings.adaptive_parity_enabled)
        self.parity_trial = QSpinBox()
        self.parity_trial.setRange(1, 50)
        self.parity_trial.setValue(self._settings.parity_trial_failed_attempts)
        self.auto_band_hop = QCheckBox("Automatic band hopping")
        self.auto_band_hop.setChecked(self._settings.automatic_band_hopping_enabled)
        self.band_dwell = QSpinBox()
        self.band_dwell.setRange(0, 120)
        self.band_dwell.setSuffix(" minutes")
        self.band_dwell.setValue(int(self._settings.minimum_band_dwell_minutes))
        self.auto_band_checkboxes = {}
        bands = ("160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m")
        band_box = QWidget()
        band_layout = QGridLayout(band_box)
        band_layout.setContentsMargins(0, 0, 0, 0)
        band_layout.setHorizontalSpacing(8)
        band_layout.setVerticalSpacing(0)
        for band in bands:
            checkbox = QCheckBox(band)
            checkbox.setChecked(band in self._settings.allowed_auto_hop_bands)
            self.auto_band_checkboxes[band] = checkbox
            index = len(self.auto_band_checkboxes) - 1
            band_layout.addWidget(checkbox, index // 5, index % 5)
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
        form.addRow("Final TX timeout", self.final_tx_timeout)
        form.addRow("Maximum final 73 retries", self.max_final_retries)
        form.addRow(self.adaptive_enabled)
        form.addRow("Adaptive attempt window", self.stagnation_window)
        form.addRow("Adaptive failed attempts", self.stagnation_failed)
        form.addRow("Adaptive max repeated calls", self.stagnation_unique)
        form.addRow(self.adaptive_parity)
        form.addRow("Parity trial failures", self.parity_trial)
        form.addRow(self.auto_band_hop)
        form.addRow("Minimum band dwell", self.band_dwell)
        form.addRow("Allowed bands (ordered)", band_box)
        form.addRow("Offline cty.dat", _path_picker(self, self.cty_path, "CTY data (*.dat);;All files (*)"))
        form.addRow("WSJT-X ADIF log", _path_picker(self, self.adif_path, "ADIF (*.adi *.adif);;All files (*)"))
        form.addRow(self.sync_adif)
        self._update_ap1_controls()
        return widget

    def accept(self) -> None:
        if not self._apply():
            return
        super().accept()

    def _apply(self) -> bool:
        if self.auto_band_hop.isChecked() and not any(box.isChecked() for box in self.auto_band_checkboxes.values()):
            QMessageBox.warning(self, "Adaptive operation", "Select at least one allowed band before enabling automatic band hopping.")
            return False
        if self.ftx1_cat2_enabled.isChecked() and not self.ftx1_cat2_confirmed_ftx1.isChecked():
            QMessageBox.warning(self, "FTX-1 CAT-2", "Confirm that this port belongs to a Yaesu FTX-1 before enabling CAT-2.")
            return False
        if self.ftx1_cat2_enabled.isChecked() and not self.ftx1_cat2_port.text().strip():
            QMessageBox.warning(self, "FTX-1 CAT-2", "Specify the FTX-1 Standard COM / CAT-2 port.")
            return False
        self.settings_applied.emit(self.settings())
        return True

    def _update_ap1_controls(self) -> None:
        tooltip = "Requires WSJT-X AutoPilot AP1"
        for control in (self.adaptive_parity, self.parity_trial, self.auto_band_hop, self.band_dwell):
            control.setEnabled(self._ap1_available)
            control.setToolTip(tooltip if not self._ap1_available else "")
        for checkbox in self.auto_band_checkboxes.values():
            checkbox.setEnabled(self._ap1_available)
            checkbox.setToolTip(tooltip if not self._ap1_available else "")

    def _ftx1_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        cat_group = QGroupBox("FTX-1 CAT-2")
        cat_form = QFormLayout(cat_group)
        self.ftx1_cat2_enabled = QCheckBox("Enable FTX-1 CAT-2 (Standard COM only)")
        self.ftx1_cat2_enabled.setChecked(self._settings.ftx1_cat2_enabled)
        self.ftx1_cat2_port = QLineEdit(self._settings.ftx1_cat2_port)
        self.ftx1_cat2_port.setPlaceholderText("COM5")
        self.ftx1_cat2_baudrate = QSpinBox()
        self.ftx1_cat2_baudrate.setRange(1_200, 115_200)
        self.ftx1_cat2_baudrate.setValue(self._settings.ftx1_cat2_baudrate)
        self.ftx1_cat2_timeout = QSpinBox()
        self.ftx1_cat2_timeout.setRange(50, 5_000)
        self.ftx1_cat2_timeout.setSuffix(" ms")
        self.ftx1_cat2_timeout.setValue(round(self._settings.ftx1_cat2_timeout_seconds * 1000))
        cat_form.addRow(self.ftx1_cat2_enabled)
        cat_form.addRow("Standard COM / CAT-2 port", self.ftx1_cat2_port)
        cat_form.addRow("Baud rate", self.ftx1_cat2_baudrate)
        cat_form.addRow("CAT timeout", self.ftx1_cat2_timeout)
        layout.addWidget(cat_group)

        self.ftx1_cat2_confirmed_ftx1 = QCheckBox("I confirm this CAT-2 port belongs to a Yaesu FTX-1")
        self.ftx1_cat2_confirmed_ftx1.setChecked(self._settings.ftx1_cat2_confirmed_ftx1)
        cat_form.addRow(self.ftx1_cat2_confirmed_ftx1)
        profiles_group = QGroupBox("FTX-1 Band Drive")
        profiles_layout = QVBoxLayout(profiles_group)
        self.ftx1_auto_apply_band_profiles = QCheckBox("Auto Apply profiles after a band change")
        self.ftx1_auto_apply_band_profiles.setChecked(self._settings.ftx1_auto_apply_band_profiles)
        profiles_layout.addWidget(self.ftx1_auto_apply_band_profiles)
        profiles_layout.addWidget(QLabel("Leave both values empty to omit a band profile."))
        profiles = QTableWidget(11, 3)
        profiles.setHorizontalHeaderLabels(["Band", "USB MOD GAIN", "TX audio attenuation"])
        profiles.verticalHeader().hide()
        profiles.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        profiles.setMaximumHeight(330)
        self.ftx1_profile_fields = {}
        for row, band in enumerate(("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m")):
            band_item = QTableWidgetItem(band)
            band_item.setFlags(band_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            profiles.setItem(row, 0, band_item)
            gain = QLineEdit()
            gain.setValidator(QIntValidator(0, 100, gain))
            gain.setPlaceholderText("0-100")
            attenuation = QLineEdit()
            attenuation.setValidator(QIntValidator(0, 450, attenuation))
            attenuation.setPlaceholderText("0-450")
            configured = self._settings.ftx1_band_profiles.get(band, {})
            if "usb_mod_gain" in configured:
                gain.setText(str(configured["usb_mod_gain"]))
            if "tx_audio_attenuation" in configured:
                attenuation.setText(str(configured["tx_audio_attenuation"]))
            profiles.setCellWidget(row, 1, gain)
            profiles.setCellWidget(row, 2, attenuation)
            self.ftx1_profile_fields[band] = (gain, attenuation)
        profiles_layout.addWidget(profiles)
        layout.addWidget(profiles_group)
        note = QLabel("FTX-1 only. CAT-2 uses no RTS/DTR PTT.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.ftx1_cat2_enabled.toggled.connect(self._update_ftx1_controls)
        self.ftx1_cat2_confirmed_ftx1.toggled.connect(self._update_ftx1_controls)
        self._update_ftx1_controls()
        return widget

    def _update_ftx1_controls(self) -> None:
        enabled = self.ftx1_cat2_enabled.isChecked()
        confirmed = enabled and self.ftx1_cat2_confirmed_ftx1.isChecked()
        self.ftx1_cat2_confirmed_ftx1.setEnabled(enabled)
        for control in (
            self.ftx1_cat2_port,
            self.ftx1_cat2_baudrate,
            self.ftx1_cat2_timeout,
        ):
            control.setEnabled(enabled)
        self.ftx1_auto_apply_band_profiles.setEnabled(confirmed)
        for gain, attenuation in self.ftx1_profile_fields.values():
            gain.setEnabled(confirmed)
            attenuation.setEnabled(confirmed)

    def _diagnostic_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self.logging_level = QComboBox()
        self.logging_level.addItem("Normal", "normal")
        self.logging_level.addItem("Debug", "debug")
        self.logging_level.setCurrentIndex(max(0, self.logging_level.findData(self._settings.logging_level)))
        session = current_logging_session()
        log_path = session.path if session is not None else AppPaths.from_environment().log_dir
        current_log = QLabel(str(log_path))
        current_log.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        current_log.setWordWrap(True)
        form.addRow("File logging", self.logging_level)
        form.addRow("Current session log", current_log)
        layout.addLayout(form)
        open_folder = QPushButton("OPEN LOG FOLDER")
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(AppPaths.from_environment().log_dir)))
        )
        export = QPushButton("EXPORT DIAGNOSTIC ZIP")
        export.clicked.connect(self._export_diagnostic)
        layout.addWidget(open_folder)
        layout.addWidget(export)
        layout.addStretch()
        return widget

    def _export_diagnostic(self) -> None:
        try:
            path = export_diagnostic(AppPaths.from_environment().ensure_directories(), self.settings())
        except Exception as exc:
            LOGGER.exception("[DIAGNOSTIC] export failed")
            QMessageBox.critical(self, "Diagnostic export failed", str(exc))
            return
        QMessageBox.information(self, "Diagnostic exported", f"Diagnostic ZIP created:\n{path}")


class MainWindow(QMainWindow):
    apply_settings_requested = Signal(object)
    arm_requested = Signal()
    disarm_requested = Signal(str)
    ignore_requested = Signal(str)
    reset_adaptive_requested = Signal()
    save_ftx1_current_band_profile_requested = Signal()
    delete_ftx1_current_band_profile_requested = Signal()
    reset_all_ftx1_band_profiles_requested = Signal()
    stop_requested = Signal()

    def __init__(self, settings_store: SettingsStore | None = None) -> None:
        super().__init__()
        self._store = settings_store or SettingsStore()
        self._settings = self._store.load()
        self._armed = False
        self._ap1_available = False
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
        self.reset_adaptive_requested.connect(self._worker.reset_adaptive_strategy)
        self.save_ftx1_current_band_profile_requested.connect(self._worker.save_ftx1_current_band_profile)
        self.delete_ftx1_current_band_profile_requested.connect(self._worker.delete_ftx1_current_band_profile)
        self.reset_all_ftx1_band_profiles_requested.connect(self._worker.reset_all_ftx1_band_profiles)
        self._worker.connection_changed.connect(self._show_connection)
        self._worker.armed_changed.connect(self._show_armed)
        self._worker.activity_received.connect(self._add_activity)
        self._worker.engine_event.connect(self._handle_engine_event)
        self._worker.status_changed.connect(self._show_status)
        self._worker.adaptive_changed.connect(self._show_adaptive)
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

        adaptive = QFrame()
        adaptive.setObjectName("panel")
        adaptive_layout = QHBoxLayout(adaptive)
        self.ap1_status = QLabel("WSJT-X AP1: Not detected")
        self.ap1_status.setObjectName("adaptiveUnavailable")
        self.adaptive_status = QLabel("Adaptive: Normal")
        self.tx_period_status = QLabel("TX period: Unknown")
        self.band_status = QLabel("Band: —")
        self.next_band_status = QLabel("Next adaptive band: —")
        self.ftx1_status = QLabel("")
        self.ftx1_status.setVisible(False)
        reset_adaptive = QPushButton("RESET ADAPTIVE STRATEGY")
        reset_adaptive.clicked.connect(self.reset_adaptive_requested.emit)
        adaptive_layout.addWidget(self.ap1_status)
        adaptive_layout.addWidget(self.adaptive_status)
        adaptive_layout.addWidget(self.tx_period_status)
        adaptive_layout.addWidget(self.band_status)
        adaptive_layout.addWidget(self.next_band_status)
        adaptive_layout.addWidget(self.ftx1_status)
        adaptive_layout.addStretch()
        adaptive_layout.addWidget(reset_adaptive)
        self.save_ftx1_current_band_profile_button = QPushButton("SAVE CURRENT BAND")
        self.save_ftx1_current_band_profile_button.clicked.connect(self.save_ftx1_current_band_profile_requested.emit)
        self.save_ftx1_current_band_profile_button.setVisible(False)
        self.delete_ftx1_current_band_profile_button = QPushButton("DELETE CURRENT BAND PROFILE")
        self.delete_ftx1_current_band_profile_button.clicked.connect(self.delete_ftx1_current_band_profile_requested.emit)
        self.delete_ftx1_current_band_profile_button.setVisible(False)
        self.reset_all_ftx1_band_profiles_button = QPushButton("RESET ALL BAND PROFILES")
        self.reset_all_ftx1_band_profiles_button.clicked.connect(self.reset_all_ftx1_band_profiles_requested.emit)
        self.reset_all_ftx1_band_profiles_button.setVisible(False)
        adaptive_layout.addWidget(self.save_ftx1_current_band_profile_button)
        adaptive_layout.addWidget(self.delete_ftx1_current_band_profile_button)
        adaptive_layout.addWidget(self.reset_all_ftx1_band_profiles_button)
        layout.addWidget(adaptive)

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
        self.tx_state = QLabel("RX")
        qso_layout.addWidget(self.qso_state)
        qso_layout.addWidget(self.qso_remote)
        qso_layout.addWidget(self.qso_remote_detail)
        qso_layout.addWidget(self.qso_stage)
        qso_layout.addWidget(self.qso_progress)
        qso_layout.addWidget(self.remote_cq_progress)
        qso_layout.addWidget(self.qso_notice)
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
        self.pending_direct_label = QLabel("Pending Direct Calls: 0")
        self._pending_direct_stations: set[str] = set()
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
        layout.addWidget(self.pending_direct_label)
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
        if event.kind is EngineEventKind.PENDING_DIRECT_ADDED:
            self._pending_direct_stations.add(event.station)
            self._update_pending_direct_label()
            self.statusBar().showMessage(f"Pending Direct Call: {event.station}", 5000)
        elif event.kind is EngineEventKind.PENDING_DIRECT_REMOVED:
            self._pending_direct_stations.discard(event.station)
            self._update_pending_direct_label()
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

    def _update_pending_direct_label(self) -> None:
        count = len(self._pending_direct_stations)
        next_station = next(iter(sorted(self._pending_direct_stations)), "-")
        suffix = f"\nNext: {next_station}" if count else ""
        self.pending_direct_label.setText(f"Pending Direct Calls: {count}{suffix}")

    def _show_connection(self, connected: bool) -> None:
        self.connection.setText("WSJT-X ONLINE" if connected else "OFFLINE")
        self.connection.setProperty("online", connected)
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)

    def _show_armed(self, armed: bool) -> None:
        if self._armed != armed:
            LOGGER.warning("[GUI] ARM state divergence backend_armed=%s ui_armed=%s; resynchronizing", armed, self._armed)
        self._armed = armed
        self.arm_button.setText("DISARM NOW" if armed else "ARM CONTROL")
        self.arm_button.setProperty("armed", armed)
        self.arm_button.style().unpolish(self.arm_button)
        self.arm_button.style().polish(self.arm_button)
        LOGGER.info("[GUI] ARM state update armed=%s", armed)
        LOGGER.info("[GUI] control state backend_armed=%s ui_armed=%s", armed, self._armed)

    def _show_status(self, status: StatusView) -> None:
        self.frequency.setText(status.frequency)
        self.mode.setText(status.mode)
        self.tx_state.setText(status.tx_state)

    def _show_adaptive(self, snapshot: dict[str, object]) -> None:
        available = bool(snapshot["ap1_available"])
        self._ap1_available = available
        self.ap1_status.setText("WSJT-X AP1: Available" if available else "WSJT-X AP1: Not detected")
        self.ap1_status.setObjectName("adaptiveAvailable" if available else "adaptiveUnavailable")
        self.ap1_status.style().unpolish(self.ap1_status)
        self.ap1_status.style().polish(self.ap1_status)
        labels = {
            "NORMAL": "Normal",
            "PARITY_CHANGE_PENDING": "TX period change pending",
            "PARITY_TRIAL": "Testing First/Second change",
            "BAND_HOP_PENDING": "Band hop pending",
            "BAND_CHANGING": "Changing band",
            "BAND_TRIAL": "Testing new band",
        }
        state = str(snapshot["state"])
        changing_to = snapshot.get("band_changing_to")
        current_band = snapshot.get("current_band") or "—"
        adaptive_text = labels.get(state, state)
        if state == "BAND_CHANGING" and changing_to:
            adaptive_text = f"Changing {current_band} -> {changing_to}"
        self.adaptive_status.setText(f"Adaptive: {adaptive_text}")
        requested = snapshot.get("requested_tx_period")
        current = snapshot.get("current_tx_first")
        period = requested if requested is not None else current
        if period is None:
            self.tx_period_status.setText("TX period: Unknown")
        else:
            text = "First" if period else "Second"
            self.tx_period_status.setText(f"TX period: {text}" + (" (Auto)" if requested is not None else ""))
        self.band_status.setText(f"Band: {current_band}")
        self.next_band_status.setText(f"Next adaptive band: {snapshot.get('next_band') or '—'}")
        ftx1 = snapshot.get("ftx1")
        if isinstance(ftx1, dict):
            ftx1_band = ftx1.get("band") or current_band
            saved_usb = ftx1.get("saved_usb_mod_gain")
            saved_att = ftx1.get("saved_tx_audio_attenuation")
            profile_text = (
                f"Saved USB {saved_usb} / ATT {saved_att}"
                if saved_usb is not None and saved_att is not None
                else "No profile"
            )
            self.ftx1_status.setText(
                f"FTX-1 CAT-2: {'Connected' if ftx1.get('connected') else 'Disconnected'} / "
                f"{ftx1_band or '—'} / Current USB {ftx1.get('current_usb_mod_gain') if ftx1.get('current_usb_mod_gain') is not None else '—'} "
                f"/ ATT {ftx1.get('current_tx_audio_attenuation') if ftx1.get('current_tx_audio_attenuation') is not None else '—'} / {profile_text}"
            )
            self.ftx1_status.setVisible(True)
            self.save_ftx1_current_band_profile_button.setVisible(True)
            self.delete_ftx1_current_band_profile_button.setVisible(True)
            self.reset_all_ftx1_band_profiles_button.setVisible(True)
            controls_enabled = bool(ftx1.get("connected")) and current_band != "—"
            self.save_ftx1_current_band_profile_button.setEnabled(controls_enabled)
            self.delete_ftx1_current_band_profile_button.setEnabled(controls_enabled)
        else:
            self.ftx1_status.setVisible(False)
            self.save_ftx1_current_band_profile_button.setVisible(False)
            self.delete_ftx1_current_band_profile_button.setVisible(False)
            self.reset_all_ftx1_band_profiles_button.setVisible(False)

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
    ) -> None:
        self.qso_state.setText(state)
        self.qso_remote.setText(f"REMOTE  {remote}")
        self.qso_remote_detail.setText(remote_detail)
        self.qso_stage.setText(f"STAGE  {stage}")
        self.qso_progress.setText(f"NO PROGRESS  {count} / {maximum}")
        self.remote_cq_progress.setText(f"REMOTE CQ  {remote_cq_count} / {remote_cq_maximum}")
        self.qso_notice.setText(notice)

    def _toggle_arm(self) -> None:
        LOGGER.info("[GUI] ARM button clicked current_ui_state=%s", self._armed)
        if self._armed:
            self.disarm_requested.emit("user_request")
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
        dialog = SettingsDialog(self._settings, self, self._ap1_available)
        dialog.settings_applied.connect(self._apply_settings)
        dialog.exec()

    def _apply_settings(self, settings: UserSettings) -> None:
        self._settings = settings
        self._store.save(self._settings)
        set_file_log_level(self._settings.logging_level)
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
            #warningText { color: #efbd67; background: #2a2115; border: 1px solid #6f522b; padding: 8px; }
            #adaptiveAvailable { color: #70dfbd; font-weight: 700; }
            #adaptiveUnavailable { color: #efbd67; font-weight: 700; }
            #infoText { color: #8299a8; padding: 5px; }
            #qsoState { color: #70dfbd; font-size: 16px; font-weight: 800; padding: 4px 14px; }
            QLineEdit, QSpinBox, QComboBox { background: #111b22; border: 1px solid #30424d; border-radius: 3px; padding: 6px; }
            QGroupBox { border: 1px solid #263640; border-radius: 4px; margin-top: 10px; padding: 8px; font-weight: 700; }
            QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; color: #9db0bc; }
            QCheckBox:disabled, QLabel:disabled { color: #53636d; }
            QSpinBox:disabled, QComboBox:disabled, QLineEdit:disabled { color: #53636d; background: #0e151a; border-color: #202c33; }
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
