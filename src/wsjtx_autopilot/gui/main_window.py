from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path

from PySide6.QtCore import QProcess, QSize, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QDesktopServices, QGuiApplication, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.logging_setup import (
    build_metadata,
    current_logging_session,
    export_diagnostic,
    set_file_log_level,
)
from wsjtx_autopilot.hamlib import list_supported_radios
from wsjtx_autopilot.updates import RELEASES_PAGE_URL, UpdateCheckResult, UpdateRelease, version_key

from .hunt import HuntPanel, PskReporterMqttPoller, SpaceWeatherPoller, SpecialCallPoller, WavelogLotwPoller
from .updates import UpdateCheckPoller, UpdateDownloadPoller
from .session_map import SessionQsoMapDialog
from .viewmodels import ActivityRow, CandidateRow, SessionQsoMapPoint, StatusView
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
        self.tabs.addTab(self._ftx1_tab(), "Radio / Hamlib")
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
            allow_direct_call_dupes=self.dupe_policy.currentData() == "direct",
            allow_dupes=self.dupe_policy.currentData() == "all",
            exclude_previously_worked_calls=self._settings.exclude_previously_worked_calls,
            minimum_snr=minimum_snr,
            favor_strong_signals=self.strong_signals.isChecked(),
            pota_policy=ActivityPolicy(self.pota_policy.currentData()),
            sota_policy=ActivityPolicy(self.sota_policy.currentData()),
            qrp_policy=ActivityPolicy(self.qrp_policy.currentData()),
            prioritize_special_calls_in_reply=self.special_call_priority.isChecked(),
            respond_to_cq_dx=self.respond_cq_dx.isChecked(),
            blacklist=_csv_set(self.blacklist.text()),
            ignore_minutes=self.ignore_minutes.value(),
            cty_dat_path=self.cty_path.text().strip() or None,
            wsjtx_log_path=self.adif_path.text().strip() or None,
            launch_wsjtx_with_autopilot=self.manage_wsjtx.isChecked(),
            wsjtx_executable_path=self.wsjtx_executable_path.text().strip(),
            sync_adif_on_startup=self.sync_adif.isChecked(),
            direct_reply_patched=False,
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
            adaptive_opportunity_quiet_minutes=float(self.opportunity_quiet.value()),
            automatic_band_hopping_enabled=self.auto_band_hop.isChecked(),
            allowed_auto_hop_bands=[band for band, checkbox in self.auto_band_checkboxes.items() if checkbox.isChecked()],
            minimum_band_dwell_minutes=float(self.band_dwell.value()),
            maximum_band_dwell_minutes=float(self.maximum_band_dwell.value()),
            forced_band_hopping_enabled=self.forced_band_hop.isChecked(),
            forced_band_hop_bands=[value.strip().lower() for value in self.forced_band_bands.text().split(",") if value.strip()],
            forced_band_hop_interval_minutes=float(self.forced_band_interval.value()),
            forced_band_hop_settle_seconds=float(self.forced_band_settle.value()),
            ftx1_cat2_enabled=self.ftx1_cat2_enabled.isChecked(),
            ftx1_cat2_confirmed_ftx1=False,
            ftx1_cat2_port=self.ftx1_cat2_port.text(),
            ftx1_cat2_baudrate=self.ftx1_cat2_baudrate.value(),
            ftx1_cat2_timeout_seconds=self.ftx1_cat2_timeout.value() / 1000,
            ftx1_hamlib_enabled=True,
            hamlib_rig_model=int(self.hamlib_radio.currentData() or self._settings.hamlib_rig_model),
            ftx1_hamlib_rigctld_path=self.ftx1_hamlib_path.text(),
            ftx1_hamlib_tcp_port=self.ftx1_hamlib_tcp_port.value(),
            ftx1_meter_poll_seconds=self.ftx1_meter_poll.value(),
            ftx1_alc_control_enabled=self.ftx1_alc_control.isChecked(),
            ftx1_alc_minimum=self.ftx1_alc_min.value(),
            ftx1_alc_maximum=self.ftx1_alc_max.value(),
            ftx1_alc_attenuation_step=self.ftx1_alc_step.value(),
            ftx1_swr_protection_enabled=self.ftx1_swr_protection.isChecked(),
            ftx1_swr_limit=self.ftx1_swr_limit.value(),
            ftx1_swr_trip_samples=self.ftx1_swr_samples.value(),
            atu100_tune_on_band_change=self.atu100_automatic_tune.isChecked(),
            ftx1_auto_apply_band_profiles=self.ftx1_alc_control.isChecked(),
            ftx1_band_profiles=self._settings.ftx1_band_profiles,
            discord_presence_enabled=self.discord_presence_enabled.isChecked(),
            discord_application_id=self.discord_application_id.text().strip(),
            special_calls_api_key=self.special_calls_api_key.text().strip(),
            special_calls_refresh_minutes=self.special_calls_refresh.value(),
            special_calls_lookback_minutes=self.special_calls_lookback.value(),
            wavelog_api_url=self.wavelog_api_url.text().strip(),
            wavelog_api_token=self.wavelog_api_token.text().strip(),
            wavelog_lotw_refresh_minutes=self.wavelog_lotw_refresh.value(),
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
        self.manage_wsjtx = QCheckBox("Démarrer AP1 intégré avec AutoPilot (réduit)")
        self.manage_wsjtx.setChecked(self._settings.launch_wsjtx_with_autopilot)
        self.wsjtx_executable_path = QLineEdit(self._settings.wsjtx_executable_path)
        self.wsjtx_executable_path.setPlaceholderText("AP1 intégré (détection automatique)")
        self.wsjtx_browse = QPushButton("Parcourir…")
        executable_row = QWidget()
        executable_layout = QHBoxLayout(executable_row)
        executable_layout.setContentsMargins(0, 0, 0, 0)
        executable_layout.addWidget(self.wsjtx_executable_path, 1)
        executable_layout.addWidget(self.wsjtx_browse)
        self.wsjtx_browse.clicked.connect(self._choose_wsjtx_executable)
        self.manage_wsjtx.toggled.connect(self.wsjtx_executable_path.setEnabled)
        self.manage_wsjtx.toggled.connect(self.wsjtx_browse.setEnabled)
        self.wsjtx_executable_path.setEnabled(self.manage_wsjtx.isChecked())
        self.wsjtx_browse.setEnabled(self.manage_wsjtx.isChecked())
        form.addRow(self.manage_wsjtx)
        form.addRow("Fichier WSJT-X", executable_row)
        note = QLabel(
            "AutoPilot démarre WSJT-X réduit et ne ferme que l’instance qu’il a lancée. "
            "Une instance ouverte manuellement reste intacte."
        )
        note.setWordWrap(True)
        form.addRow(note)
        return widget

    def _choose_wsjtx_executable(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Sélectionner wsjtx.exe",
            self.wsjtx_executable_path.text().strip(),
            "WSJT-X (wsjtx.exe);;Programmes (*.exe)",
        )
        if filename:
            self.wsjtx_executable_path.setText(filename)

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
        self.special_call_priority = QCheckBox("Prioriser les indicatifs spéciaux actifs en mode Reply")
        self.special_call_priority.setChecked(self._settings.prioritize_special_calls_in_reply)
        self.special_call_priority.setToolTip(
            "Utilise la liste FT8 vérifiée de l'onglet Hunt. Les appels directs qui vous sont adressés restent prioritaires."
        )
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
        form.addRow("Indicatifs spéciaux", self.special_call_priority)
        form.addRow("CQ DX", self.respond_cq_dx)
        return widget

    def _safety_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.dupe_policy = QComboBox()
        self.dupe_policy.addItem("Jamais", "never")
        self.dupe_policy.addItem("Seulement si la station m'appelle", "direct")
        self.dupe_policy.addItem("Toujours", "all")
        selected_dupe_policy = "all" if self._settings.allow_dupes else "direct" if self._settings.allow_direct_call_dupes else "never"
        self.dupe_policy.setCurrentIndex(max(0, self.dupe_policy.findData(selected_dupe_policy)))
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
        self.adaptive_parity = QCheckBox("Essayer l'autre cycle TX avant de changer de bande")
        self.adaptive_parity.setChecked(self._settings.adaptive_parity_enabled)
        self.parity_trial = QSpinBox()
        self.parity_trial.setRange(1, 50)
        self.parity_trial.setValue(self._settings.parity_trial_failed_attempts)
        self.opportunity_quiet = QSpinBox()
        self.opportunity_quiet.setRange(1, 60)
        self.opportunity_quiet.setSuffix(" minutes")
        self.opportunity_quiet.setValue(int(self._settings.adaptive_opportunity_quiet_minutes))
        self.auto_band_hop = QCheckBox("Changement adaptatif : bande épuisée")
        self.auto_band_hop.setChecked(self._settings.automatic_band_hopping_enabled)
        self.band_dwell = QSpinBox()
        self.band_dwell.setRange(0, 120)
        self.band_dwell.setSuffix(" minutes")
        self.band_dwell.setValue(int(self._settings.minimum_band_dwell_minutes))
        self.maximum_band_dwell = QSpinBox()
        self.maximum_band_dwell.setRange(0, 24 * 60)
        self.maximum_band_dwell.setSuffix(" minutes")
        self.maximum_band_dwell.setSpecialValueText("Désactivé")
        self.maximum_band_dwell.setValue(int(self._settings.maximum_band_dwell_minutes))
        self.forced_band_hop = QCheckBox("Rotation programmée des bandes")
        self.forced_band_hop.setChecked(self._settings.forced_band_hopping_enabled)
        self.forced_band_bands = QLineEdit(", ".join(self._settings.forced_band_hop_bands))
        self.forced_band_bands.setPlaceholderText("20m, 30m, 40m")
        self.forced_band_interval = QSpinBox()
        self.forced_band_interval.setRange(0, 24 * 60)
        self.forced_band_interval.setSuffix(" minutes")
        self.forced_band_interval.setValue(int(self._settings.forced_band_hop_interval_minutes))
        self.forced_band_settle = QSpinBox()
        self.forced_band_settle.setRange(0, 600)
        self.forced_band_settle.setSuffix(" seconds")
        self.forced_band_settle.setValue(int(self._settings.forced_band_hop_settle_seconds))
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
        form.addRow("Doublons déjà contactés aujourd'hui", self.dupe_policy)
        form.addRow("Blacklist", self.blacklist)
        form.addRow("Temporary ignore (minutes)", self.ignore_minutes)
        form.addRow("Max periods without progress", self.max_no_progress)
        form.addRow("Maximum final 73 retries", self.max_final_retries)
        form.addRow(self.adaptive_enabled)
        form.addRow("No new station before switching", self.opportunity_quiet)
        form.addRow(self.adaptive_parity)
        form.addRow(self.auto_band_hop)
        form.addRow("Minimum band dwell", self.band_dwell)
        form.addRow("Durée maximale sur une bande", self.maximum_band_dwell)
        form.addRow(self.forced_band_hop)
        form.addRow("Bandes de la rotation programmée", self.forced_band_bands)
        form.addRow("Intervalle de rotation", self.forced_band_interval)
        form.addRow("Stabilisation après changement", self.forced_band_settle)
        form.addRow("Bandes autorisées en adaptatif", band_box)
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
        if self.forced_band_hop.isChecked() and not self.settings().forced_band_hop_bands:
            QMessageBox.warning(self, "Forced band hopping", "Enter at least one ordered band before enabling forced band hopping.")
            return False
        if self.ftx1_cat2_enabled.isChecked() and not self.ftx1_hamlib_enabled.isChecked() and not self.ftx1_cat2_confirmed_ftx1.isChecked():
            QMessageBox.warning(self, "FTX-1 CAT-2", "Confirm that this port belongs to a Yaesu FTX-1 before enabling CAT-2.")
            return False
        if self.ftx1_cat2_enabled.isChecked() and not self.ftx1_cat2_port.text().strip():
            QMessageBox.warning(self, "FTX-1 CAT-2", "Specify the FTX-1 Standard COM / CAT-2 port.")
            return False
        if self.ftx1_alc_min.value() > self.ftx1_alc_max.value():
            QMessageBox.warning(self, "Hamlib ALC", "Minimum ALC must not exceed maximum ALC.")
            return False
        self.settings_applied.emit(self.settings())
        return True

    def _update_ap1_controls(self) -> None:
        tooltip = "Requires WSJT-X AutoPilot AP1"
        self.adaptive_parity.setEnabled(self._ap1_available)
        self.adaptive_parity.setToolTip(tooltip if not self._ap1_available else "")
        for control in (
            self.opportunity_quiet,
            self.auto_band_hop,
            self.band_dwell,
            self.maximum_band_dwell,
            self.forced_band_hop,
            self.forced_band_bands,
            self.forced_band_interval,
            self.forced_band_settle,
        ):
            control.setEnabled(True)
            control.setToolTip("")
        for checkbox in self.auto_band_checkboxes.values():
            checkbox.setEnabled(True)
            checkbox.setToolTip("")

    def _ftx1_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        cat_group = QGroupBox("Radio CAT via Hamlib")
        cat_form = QFormLayout(cat_group)
        self.ftx1_cat2_enabled = QCheckBox("Activer le contrôle radio Hamlib")
        self.ftx1_cat2_enabled.setChecked(self._settings.ftx1_cat2_enabled)
        self.hamlib_radio = QComboBox()
        self.hamlib_radio.setEditable(True)
        self.hamlib_radio.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.hamlib_radio.setMaxVisibleItems(20)
        self.hamlib_radio.lineEdit().setPlaceholderText("Rechercher une marque ou un modèle")
        selected_index = -1
        for model_number, manufacturer, model_name in list_supported_radios(self._settings.ftx1_hamlib_rigctld_path):
            self.hamlib_radio.addItem(f"{manufacturer} {model_name} (Hamlib {model_number})", model_number)
            if model_number == self._settings.hamlib_rig_model:
                selected_index = self.hamlib_radio.count() - 1
        if selected_index < 0:
            self.hamlib_radio.insertItem(
                0,
                f"Modèle Hamlib {self._settings.hamlib_rig_model}",
                self._settings.hamlib_rig_model,
            )
            selected_index = 0
        self.hamlib_radio.setCurrentIndex(selected_index)
        if self.hamlib_radio.completer() is not None:
            self.hamlib_radio.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self.hamlib_radio.completer().setFilterMode(Qt.MatchFlag.MatchContains)
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
        cat_form.addRow("Poste", self.hamlib_radio)
        cat_form.addRow("Port CAT", self.ftx1_cat2_port)
        cat_form.addRow("Baud rate", self.ftx1_cat2_baudrate)
        cat_form.addRow("CAT timeout", self.ftx1_cat2_timeout)
        layout.addWidget(cat_group)

        hamlib_group = QGroupBox("Hamlib meters and protection")
        hamlib_form = QFormLayout(hamlib_group)
        self.ftx1_hamlib_enabled = QCheckBox()
        self.ftx1_hamlib_enabled.setChecked(True)
        self.ftx1_hamlib_path = QLineEdit(self._settings.ftx1_hamlib_rigctld_path)
        self.ftx1_hamlib_path.setPlaceholderText("Automatic detection (Hamlib 4.7.1)")
        self.ftx1_hamlib_tcp_port = QSpinBox()
        self.ftx1_hamlib_tcp_port.setRange(1_024, 65_535)
        self.ftx1_hamlib_tcp_port.setValue(self._settings.ftx1_hamlib_tcp_port)
        self.ftx1_meter_poll = QDoubleSpinBox()
        self.ftx1_meter_poll.setRange(0.2, 5.0)
        self.ftx1_meter_poll.setDecimals(1)
        self.ftx1_meter_poll.setSingleStep(0.1)
        self.ftx1_meter_poll.setSuffix(" s")
        self.ftx1_meter_poll.setValue(self._settings.ftx1_meter_poll_seconds)

        self.ftx1_alc_control = QCheckBox("Automatic bidirectional ALC control")
        self.ftx1_alc_control.setChecked(self._settings.ftx1_alc_control_enabled)
        self.ftx1_alc_min = QDoubleSpinBox()
        self.ftx1_alc_min.setRange(0.0, 1.0)
        self.ftx1_alc_min.setDecimals(2)
        self.ftx1_alc_min.setSingleStep(0.01)
        self.ftx1_alc_min.setValue(self._settings.ftx1_alc_minimum)
        self.ftx1_alc_max = QDoubleSpinBox()
        self.ftx1_alc_max.setRange(0.0, 1.0)
        self.ftx1_alc_max.setDecimals(2)
        self.ftx1_alc_max.setSingleStep(0.01)
        self.ftx1_alc_max.setValue(self._settings.ftx1_alc_maximum)
        self.ftx1_alc_step = QSpinBox()
        self.ftx1_alc_step.setRange(1, 50)
        self.ftx1_alc_step.setValue(self._settings.ftx1_alc_attenuation_step)
        hamlib_form.addRow(self.ftx1_alc_control)

        self.ftx1_swr_protection = QCheckBox("SWR safety: Halt TX and disarm")
        self.ftx1_swr_protection.setChecked(self._settings.ftx1_swr_protection_enabled)
        self.ftx1_swr_limit = QDoubleSpinBox()
        self.ftx1_swr_limit.setRange(1.1, 5.0)
        self.ftx1_swr_limit.setDecimals(1)
        self.ftx1_swr_limit.setSingleStep(0.1)
        self.ftx1_swr_limit.setValue(self._settings.ftx1_swr_limit)
        self.ftx1_swr_samples = QSpinBox()
        self.ftx1_swr_samples.setRange(1, 10)
        self.ftx1_swr_samples.setValue(self._settings.ftx1_swr_trip_samples)
        self.atu100_automatic_tune = QCheckBox("ATU-100 : Tune après changement de bande")
        self.atu100_automatic_tune.setToolTip(
            "Demande une porteuse Tune à AP1 après une bascule de bande confirmée. "
            "N'active pas une mesure SWR si le poste ne la fournit pas."
        )
        self.atu100_automatic_tune.setChecked(self._settings.atu100_tune_on_band_change)
        hamlib_form.addRow(self.ftx1_swr_protection)
        hamlib_form.addRow("SWR trip threshold", self.ftx1_swr_limit)
        hamlib_form.addRow(self.atu100_automatic_tune)
        hamlib_form.addRow(QLabel("Les délais, intervalles et seuils internes sont gérés automatiquement."))
        layout.addWidget(hamlib_group)

        self.ftx1_cat2_confirmed_ftx1 = QCheckBox("Mode CAT direct ancien : je confirme que le poste est un FTX-1")
        self.ftx1_cat2_confirmed_ftx1.setChecked(self._settings.ftx1_cat2_confirmed_ftx1)
        self.ftx1_cat2_confirmed_ftx1.setVisible(False)
        profiles_group = QGroupBox("FTX-1 Band Drive")
        profiles_layout = QVBoxLayout(profiles_group)
        self.ftx1_auto_apply_band_profiles = QCheckBox("Auto Apply profiles after a band change")
        self.ftx1_auto_apply_band_profiles.setChecked(self._settings.ftx1_auto_apply_band_profiles)
        profiles_layout.addWidget(self.ftx1_auto_apply_band_profiles)
        profiles_layout.addWidget(QLabel("USB MOD GAIN reste manuel sur le poste. Laisser vide pour omettre la bande."))
        profiles = QTableWidget(11, 2)
        self.ftx1_profiles_table = profiles
        profiles.setHorizontalHeaderLabels(["Band", "TX audio attenuation"])
        profiles.verticalHeader().hide()
        profiles.verticalHeader().setDefaultSectionSize(34)
        header = profiles.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        profiles.setColumnWidth(0, 90)
        profiles.setColumnWidth(1, 300)
        profiles.setMinimumWidth(540)
        profiles.setMaximumHeight(330)
        self.ftx1_profile_fields = {}
        for row, band in enumerate(("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m")):
            band_item = QTableWidgetItem(band)
            band_item.setFlags(band_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            profiles.setItem(row, 0, band_item)
            attenuation = QLineEdit()
            attenuation.setValidator(QIntValidator(0, 450, attenuation))
            attenuation.setPlaceholderText("0-450")
            attenuation.setMinimumHeight(max(30, attenuation.sizeHint().height()))
            attenuation.setMinimumWidth(200)
            configured = self._settings.ftx1_band_profiles.get(band, {})
            if "tx_audio_attenuation" in configured:
                attenuation.setText(str(configured["tx_audio_attenuation"]))
            profiles.setCellWidget(row, 1, attenuation)
            self.ftx1_profile_fields[band] = attenuation
        profiles_layout.addWidget(profiles)
        layout.addWidget(profiles_group)
        profiles_group.setVisible(False)
        note = QLabel("Hamlib assure la compatibilité radio. Aucun réglage USB MOD GAIN n'est modifié.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.ftx1_cat2_enabled.toggled.connect(self._update_ftx1_controls)
        self.ftx1_cat2_confirmed_ftx1.toggled.connect(self._update_ftx1_controls)
        self.ftx1_hamlib_enabled.toggled.connect(self._update_ftx1_controls)
        self._update_ftx1_controls()
        return widget

    def _update_ftx1_controls(self) -> None:
        enabled = self.ftx1_cat2_enabled.isChecked()
        hamlib_enabled = enabled and self.ftx1_hamlib_enabled.isChecked()
        confirmed = enabled and (hamlib_enabled or self.ftx1_cat2_confirmed_ftx1.isChecked())
        self.ftx1_cat2_confirmed_ftx1.setEnabled(enabled and not hamlib_enabled)
        for control in (
            self.ftx1_cat2_port,
            self.hamlib_radio,
            self.ftx1_cat2_baudrate,
            self.ftx1_cat2_timeout,
        ):
            control.setEnabled(enabled)
        self.ftx1_hamlib_enabled.setEnabled(enabled)
        for control in (
            self.ftx1_hamlib_path,
            self.ftx1_hamlib_tcp_port,
            self.ftx1_meter_poll,
            self.ftx1_alc_control,
            self.ftx1_alc_min,
            self.ftx1_alc_max,
            self.ftx1_alc_step,
            self.ftx1_swr_protection,
            self.ftx1_swr_limit,
            self.ftx1_swr_samples,
        ):
            control.setEnabled(hamlib_enabled)
        self.ftx1_auto_apply_band_profiles.setEnabled(confirmed)
        for attenuation in self.ftx1_profile_fields.values():
            attenuation.setEnabled(confirmed)

    def _diagnostic_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self.logging_level = QComboBox()
        self.logging_level.addItem("Normal", "normal")
        self.logging_level.addItem("Debug", "debug")
        self.logging_level.setCurrentIndex(max(0, self.logging_level.findData(self._settings.logging_level)))
        self.discord_presence_enabled = QCheckBox("Afficher l'activité FT8 sur Discord")
        self.discord_presence_enabled.setChecked(self._settings.discord_presence_enabled)
        self.discord_application_id = QLineEdit(self._settings.discord_application_id)
        self.discord_application_id.setPlaceholderText("Discord Application ID")
        self.special_calls_api_key = QLineEdit(self._settings.special_calls_api_key)
        self.special_calls_api_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.special_calls_api_key.setPlaceholderText("Clé gratuite afu.tools")
        self.special_calls_refresh = QSpinBox()
        self.special_calls_refresh.setRange(5, 24 * 60)
        self.special_calls_refresh.setSuffix(" minutes")
        self.special_calls_refresh.setValue(self._settings.special_calls_refresh_minutes)
        self.special_calls_lookback = QSpinBox()
        self.special_calls_lookback.setRange(60, 2880)
        self.special_calls_lookback.setSuffix(" minutes")
        self.special_calls_lookback.setValue(self._settings.special_calls_lookback_minutes)
        self.wavelog_api_url = QLineEdit(self._settings.wavelog_api_url)
        self.wavelog_api_url.setPlaceholderText("https://wavelog.example/index.php/api/v2")
        self.wavelog_api_token = QLineEdit(self._settings.wavelog_api_token)
        self.wavelog_api_token.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.wavelog_api_token.setPlaceholderText("Jeton wl2_ avec confirmation:read")
        self.wavelog_lotw_refresh = QSpinBox()
        self.wavelog_lotw_refresh.setRange(5, 24 * 60)
        self.wavelog_lotw_refresh.setSuffix(" minutes")
        self.wavelog_lotw_refresh.setValue(self._settings.wavelog_lotw_refresh_minutes)
        session = current_logging_session()
        log_path = session.path if session is not None else AppPaths.from_environment().log_dir
        current_log = QLabel(str(log_path))
        current_log.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        current_log.setWordWrap(True)
        form.addRow("File logging", self.logging_level)
        form.addRow(self.discord_presence_enabled)
        form.addRow("Discord Application ID", self.discord_application_id)
        form.addRow("Clé activité DX/FT8 (afu.tools)", self.special_calls_api_key)
        form.addRow("Actualisation indicatifs spéciaux", self.special_calls_refresh)
        form.addRow("Historique activité FT8", self.special_calls_lookback)
        form.addRow("URL API Wavelog v2", self.wavelog_api_url)
        form.addRow("Jeton Wavelog (confirmation:read)", self.wavelog_api_token)
        form.addRow("Actualisation validations LoTW", self.wavelog_lotw_refresh)
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
    reset_swr_safety_requested = Signal()
    save_ftx1_current_band_profile_requested = Signal()
    delete_ftx1_current_band_profile_requested = Signal()
    reset_all_ftx1_band_profiles_requested = Signal()
    hunt_requested = Signal(object, str)
    hunt_stop_requested = Signal()
    special_calls_updated_requested = Signal(object)
    reply_rare_tracking_requested = Signal(bool)
    favorable_path_priority_requested = Signal(bool)
    rare_stations_only_requested = Signal(bool)
    unconfirmed_dxcc_only_requested = Signal(bool)
    exclude_worked_history_requested = Signal(bool)
    wavelog_lotw_updated_requested = Signal(object)
    space_weather_updated_requested = Signal(object)
    remote_spot_received = Signal(object)
    unconfirmed_dxcc_remote_spot_received = Signal(object)
    stop_requested = Signal()

    def __init__(self, settings_store: SettingsStore | None = None) -> None:
        super().__init__()
        self._store = settings_store or SettingsStore()
        self._settings = self._store.load()
        self._armed = False
        self._ap1_available = False
        self._antenna_reset_available = False
        self._candidate_rows: dict[str, int] = {}
        self._special_call_poller: SpecialCallPoller | None = None
        self._wavelog_lotw_poller: WavelogLotwPoller | None = None
        self._space_weather_poller: SpaceWeatherPoller | None = None
        self._pskreporter_mqtt: PskReporterMqttPoller | None = None
        self._update_check_poller: UpdateCheckPoller | None = None
        self._update_download_poller: UpdateDownloadPoller | None = None
        self._available_update: UpdateRelease | None = None
        self._current_version = str(build_metadata().get("app_version", ""))
        self._local_grid = ""
        self._session_qso_points: list[SessionQsoMapPoint] = []
        self._session_map_dialog: SessionQsoMapDialog | None = None
        self.setWindowTitle("WSJTX AutoPilot")
        self.resize(1120, 700)
        self.setMinimumSize(820, 540)
        self._build_ui()
        self._apply_style()
        self._fit_control_buttons()
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
        self.reset_swr_safety_requested.connect(self._worker.reset_swr_safety)
        self.save_ftx1_current_band_profile_requested.connect(self._worker.save_ftx1_current_band_profile)
        self.delete_ftx1_current_band_profile_requested.connect(self._worker.delete_ftx1_current_band_profile)
        self.reset_all_ftx1_band_profiles_requested.connect(self._worker.reset_all_ftx1_band_profiles)
        self.hunt_requested.connect(self._worker.start_hunt)
        self.hunt_stop_requested.connect(self._worker.stop_hunt)
        self.special_calls_updated_requested.connect(self._worker.update_special_calls)
        self.reply_rare_tracking_requested.connect(self._worker.set_reply_rare_tracking)
        self.favorable_path_priority_requested.connect(self._worker.set_favorable_path_priority)
        self.rare_stations_only_requested.connect(self._worker.set_rare_stations_only)
        self.unconfirmed_dxcc_only_requested.connect(self._worker.set_unconfirmed_dxcc_only)
        self.exclude_worked_history_requested.connect(self._worker.set_exclude_worked_history)
        self.wavelog_lotw_updated_requested.connect(self._worker.update_wavelog_lotw_calls)
        self.space_weather_updated_requested.connect(self._worker.update_space_weather)
        self.remote_spot_received.connect(self._worker.update_hunt_remote_spot)
        self.unconfirmed_dxcc_remote_spot_received.connect(self._worker.update_unconfirmed_dxcc_remote_spot)
        self._worker.connection_changed.connect(self._show_connection)
        self._worker.armed_changed.connect(self._show_armed)
        self._worker.swr_safety_triggered.connect(self._show_swr_safety_notification)
        self._worker.activity_received.connect(self._add_activity)
        self._worker.engine_event.connect(self._handle_engine_event)
        self._worker.unconfirmed_dxcc_spotted.connect(self._add_unconfirmed_dxcc_mqtt_spot)
        self._worker.status_changed.connect(self._show_status)
        self._worker.adaptive_changed.connect(self._show_adaptive)
        self._worker.qso_changed.connect(self._show_qso)
        self._worker.ftx1_band_profiles_changed.connect(self._update_ftx1_band_profiles)
        self._worker.hunt_changed.connect(self._show_hunt_state)
        self._worker.rare_station_worked.connect(self._add_rare_station_worked)
        self._worker.session_qso_logged.connect(self._add_session_qso_map_point)
        self._worker.wavelog_lotw_changed.connect(self._show_wavelog_lotw_state)
        self._worker.diagnostics_changed.connect(self._show_session_diagnostics)
        self._worker.error.connect(self._show_error)
        # ``closeEvent`` waits for the worker thread.  A normal queued
        # connection would schedule QThread.quit on the GUI event loop, which
        # is precisely the loop that is waiting.  Run this thread-safe slot in
        # the worker context instead so the event loop can really terminate.
        self._worker.stopped.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.start()
        self.rare_reply_tracking_action.toggled.connect(self._set_reply_rare_tracking)
        self.favorable_path_priority_action.toggled.connect(self._set_favorable_path_priority)
        self.rare_stations_only_action.toggled.connect(self._set_rare_stations_only)
        self.unconfirmed_dxcc_only_action.toggled.connect(self._set_unconfirmed_dxcc_only)
        self.exclude_worked_history_action.toggled.connect(self._set_exclude_worked_history)
        self._update_reply_mode_summary()
        self._special_refresh_timer = QTimer(self)
        self._special_refresh_timer.timeout.connect(self._refresh_special_calls)
        self._restart_special_refresh_timer()
        self._wavelog_refresh_timer = QTimer(self)
        self._wavelog_refresh_timer.timeout.connect(self._refresh_wavelog_lotw)
        self._restart_wavelog_refresh_timer()
        self._space_weather_refresh_timer = QTimer(self)
        self._space_weather_refresh_timer.setInterval(15 * 60_000)
        self._space_weather_refresh_timer.timeout.connect(self._refresh_space_weather)
        self._space_weather_refresh_timer.start()
        QTimer.singleShot(0, self._refresh_special_calls)
        if self._settings.wavelog_api_url and self._settings.wavelog_api_token:
            QTimer.singleShot(0, self._refresh_wavelog_lotw)
        if self._settings.pskreporter_mqtt_enabled or self._settings.wavelog_unconfirmed_dxcc_only:
            QTimer.singleShot(0, self._start_pskreporter_mqtt)
        if self._settings.prioritize_favorable_path_in_reply:
            QTimer.singleShot(0, self._refresh_space_weather)
        if version_key(self._current_version) is not None:
            QTimer.singleShot(2_500, self._check_for_updates)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

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
        self.update_button = QPushButton("MISES À JOUR")
        self.update_button.setObjectName("updateButton")
        self.update_button.clicked.connect(self._on_update_button_clicked)
        settings_button = QPushButton("PREFERENCES")
        settings_button.clicked.connect(self._edit_settings)
        self.antenna_indicator = QLabel("●  OK")
        self.antenna_indicator.setObjectName("antennaSafety")
        self.antenna_indicator.setProperty("level", "ok")
        self.reset_swr_safety_button = QPushButton("RÉINITIALISER SWR")
        self.reset_swr_safety_button.setObjectName("resetSwrSafetyButton")
        self.reset_swr_safety_button.setEnabled(False)
        self.reset_swr_safety_button.clicked.connect(self._confirm_reset_swr_safety)
        self.arm_button = QPushButton("ARM CONTROL")
        self.arm_button.setObjectName("armButton")
        self.arm_button.clicked.connect(self._toggle_arm)
        header.addWidget(self.connection)
        header.addWidget(self.frequency)
        header.addWidget(self.mode)
        header.addWidget(self.antenna_indicator)
        header.addWidget(self.reset_swr_safety_button)
        header.addWidget(self.update_button)
        header.addWidget(settings_button)
        header.addWidget(self.arm_button)
        layout.addLayout(header)

        adaptive = QFrame()
        adaptive.setObjectName("panel")
        adaptive_layout = QVBoxLayout(adaptive)
        adaptive_layout.setContentsMargins(10, 8, 10, 8)
        adaptive_layout.setSpacing(6)
        operational_row = QHBoxLayout()
        operational_row.setSpacing(12)
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
        self.ftx1_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        operational_row.addWidget(self.ap1_status)
        operational_row.addWidget(self.adaptive_status)
        operational_row.addWidget(self.tx_period_status)
        operational_row.addWidget(self.band_status)
        operational_row.addWidget(self.next_band_status)
        operational_row.addWidget(self.ftx1_status, 1)
        self.save_ftx1_current_band_profile_button = QPushButton("SAVE CURRENT BAND")
        self.save_ftx1_current_band_profile_button.clicked.connect(self.save_ftx1_current_band_profile_requested.emit)
        self.save_ftx1_current_band_profile_button.setVisible(False)
        self.delete_ftx1_current_band_profile_button = QPushButton("DELETE CURRENT BAND PROFILE")
        self.delete_ftx1_current_band_profile_button.clicked.connect(self.delete_ftx1_current_band_profile_requested.emit)
        self.delete_ftx1_current_band_profile_button.setVisible(False)
        self.reset_all_ftx1_band_profiles_button = QPushButton("RÉINITIALISER APPRENTISSAGE ALC")
        self.reset_all_ftx1_band_profiles_button.clicked.connect(self.reset_all_ftx1_band_profiles_requested.emit)
        self.reset_all_ftx1_band_profiles_button.setVisible(False)
        controls_row = QHBoxLayout()
        self.opportunity_status = QLabel("VIABLES : —   |   SANS NOUVELLE : — / 03:00   |   PROCHAINE BASCULE : —")
        self.opportunity_status.setObjectName("opportunityStatus")
        controls_row.addWidget(self.opportunity_status)
        controls_row.addStretch()
        self._profile_control_buttons = (
            reset_adaptive,
            self.save_ftx1_current_band_profile_button,
            self.delete_ftx1_current_band_profile_button,
            self.reset_all_ftx1_band_profiles_button,
        )
        for button in self._profile_control_buttons:
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
            controls_row.addWidget(button)
        adaptive_layout.addLayout(operational_row)
        adaptive_layout.addLayout(controls_row)
        layout.addWidget(adaptive)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._activity_panel())
        splitter.addWidget(self._rare_worked_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([660, 440])

        reply_page = QWidget()
        reply_layout = QVBoxLayout(reply_page)
        reply_layout.setContentsMargins(0, 0, 0, 0)
        reply_options = QHBoxLayout()
        self.reply_options_button = QPushButton("OPTIONS REPLY")
        reply_menu = QMenu(self.reply_options_button)
        self.rare_reply_tracking_action = reply_menu.addAction("Priorité rares et suivi MQTT PSK Reporter")
        self.rare_reply_tracking_action.setCheckable(True)
        self.rare_reply_tracking_action.setChecked(self._settings.pskreporter_mqtt_enabled)
        self.rare_reply_tracking_action.setToolTip(
            "Suit les indicatifs spéciaux FT8. Un spot distant ne provoque jamais une émission : un Decode local reste obligatoire."
        )
        self.rare_stations_only_action = reply_menu.addAction("Stations rares uniquement")
        self.rare_stations_only_action.setCheckable(True)
        self.rare_stations_only_action.setChecked(self._settings.rare_stations_only)
        self.rare_stations_only_action.setToolTip("Ignore les CQ et appels classiques.")
        self.unconfirmed_dxcc_only_action = reply_menu.addAction("Priorité DXCC non validés LoTW")
        self.unconfirmed_dxcc_only_action.setCheckable(True)
        self.unconfirmed_dxcc_only_action.setChecked(self._settings.wavelog_unconfirmed_dxcc_only)
        self.unconfirmed_dxcc_only_action.setToolTip("Priorise les entités DXCC non confirmées dans LoTW selon Wavelog.")
        self.favorable_path_priority_action = reply_menu.addAction("Priorité chemin favorable (SFI / Kp)")
        self.favorable_path_priority_action.setCheckable(True)
        self.favorable_path_priority_action.setChecked(self._settings.prioritize_favorable_path_in_reply)
        self.favorable_path_priority_action.setToolTip(
            "Départage les CQ ordinaires avec la distance, l'heure, le SFI et le Kp. "
            "Ne bloque aucun CQ et ne prévaut jamais sur un appel direct."
        )
        reply_menu.addSeparator()
        self.exclude_worked_history_action = reply_menu.addAction("Exclure les stations déjà contactées")
        self.exclude_worked_history_action.setCheckable(True)
        self.exclude_worked_history_action.setChecked(self._settings.exclude_previously_worked_calls)
        self.exclude_worked_history_action.setToolTip("Écarte une station présente dans l'historique ADIF/WSJT-X, même un autre jour ou sur une autre bande.")
        self.reply_options_button.setMenu(reply_menu)
        self.session_map_button = QPushButton("CARTE QSO (0)")
        self.session_map_button.setToolTip("Ouvre la carte des QSO journalisés pendant cette session AutoPilot.")
        self.session_map_button.clicked.connect(self._show_session_qso_map)
        self.reply_mode_summary = QLabel()
        self.reply_mode_summary.setObjectName("infoText")
        reply_options.addWidget(self.reply_options_button)
        reply_options.addWidget(self.session_map_button)
        reply_options.addWidget(self.reply_mode_summary, 1)
        reply_layout.addLayout(reply_options)
        diagnostics_panel = QFrame()
        diagnostics_panel.setObjectName("panel")
        diagnostics_panel.setMaximumHeight(108)
        diagnostics_layout = QVBoxLayout(diagnostics_panel)
        diagnostics_layout.setContentsMargins(10, 6, 10, 6)
        diagnostics_layout.setSpacing(3)
        diagnostics_title = QLabel("DÉCISIONS SESSION")
        diagnostics_title.setObjectName("panelTitle")
        self.session_diagnostics = QLabel("En attente des premiers décodages.")
        self.session_diagnostics.setObjectName("infoText")
        self.session_diagnostics.setWordWrap(True)
        self.session_diagnostic_log = QPlainTextEdit()
        self.session_diagnostic_log.setReadOnly(True)
        self.session_diagnostic_log.setMaximumHeight(46)
        self.session_diagnostic_log.setPlaceholderText("Les trois dernières décisions utiles apparaîtront ici.")
        diagnostics_layout.addWidget(diagnostics_title)
        diagnostics_layout.addWidget(self.session_diagnostics)
        diagnostics_layout.addWidget(self.session_diagnostic_log)
        reply_layout.addWidget(diagnostics_panel)
        reply_layout.addWidget(splitter)
        self.hunt_panel = HuntPanel()
        self.hunt_panel.refresh_requested.connect(self._refresh_special_calls)
        self.hunt_panel.target_requested.connect(self._start_target_hunt)
        self.hunt_panel.hunt_all_requested.connect(self._start_all_hunt)
        self.hunt_panel.stop_requested.connect(self.hunt_stop_requested.emit)
        cq_page = QWidget()
        cq_layout = QVBoxLayout(cq_page)
        cq_message = QLabel("CQ — À VENIR")
        cq_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cq_message.setObjectName("comingSoon")
        cq_layout.addWidget(cq_message, 1)
        self.operation_tabs = QTabWidget()
        self.operation_tabs.addTab(reply_page, "Reply")
        self.operation_tabs.addTab(self.hunt_panel, "Hunt")
        self.operation_tabs.addTab(cq_page, "CQ")
        layout.addWidget(self.operation_tabs, 1)

        working = QFrame()
        working.setObjectName("qsoPanel")
        qso_layout = QHBoxLayout(working)
        qso_layout.setContentsMargins(10, 6, 10, 6)
        qso_layout.addWidget(QLabel("WORKING"))
        self.working_status = QLabel("Working -")
        self.working_status.setObjectName("qsoState")
        self.tx_state = QLabel("RX")
        qso_layout.addWidget(self.working_status)
        qso_layout.addStretch()
        qso_layout.addWidget(self.tx_state)
        layout.addWidget(working)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Waiting for WSJT-X UDP traffic")

    def _activity_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        label = QLabel("DECODE ACTIVITY")
        label.setObjectName("panelTitle")
        self.activity = QTableWidget(0, 4)
        self.activity.setHorizontalHeaderLabels(["UTC", "SNR", "DF", "MESSAGE"])
        self.activity.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.activity.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.activity.verticalHeader().hide()
        activity_header = self.activity.horizontalHeader()
        activity_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(label)
        layout.addWidget(self.activity)
        return panel

    def _rare_worked_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        label = QLabel("STATIONS RARES SUIVIES")
        label.setObjectName("panelTitle")
        self.rare_worked_summary = QLabel("Aucune station rare reçue par MQTT cette session.")
        self.rare_worked_summary.setObjectName("infoText")
        self.rare_worked = QTableWidget(0, 4)
        self.rare_worked.setHorizontalHeaderLabels(["UTC", "CALL", "BANDE", "ÉTAT"])
        self.rare_worked.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rare_worked.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rare_worked.verticalHeader().hide()
        rare_header = self.rare_worked.horizontalHeader()
        rare_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        rare_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rare_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        rare_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._rare_worked_rows: dict[tuple[str, str], int] = {}
        # Candidate events remain handled in the background for status messages,
        # so replacing the visual panel cannot affect radio-control behavior.
        self.pending_direct_label = QLabel("Pending Direct Calls: 0")
        self._pending_direct_stations: set[str] = set()
        self.candidates = QTableWidget(0, 5)
        self.candidates.setHorizontalHeaderLabels(["CALL", "SNR", "DXCC", "CONT", "SCORE"])
        self.candidates.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidates.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidates.verticalHeader().hide()
        candidate_header = self.candidates.horizontalHeader()
        for column in range(5):
            candidate_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.candidates.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        ignore = QAction(f"Ignore for {self._settings.ignore_minutes} minutes", self.candidates)
        ignore.triggered.connect(self._ignore_selected)
        blacklist = QAction("Add to blacklist", self.candidates)
        blacklist.triggered.connect(self._blacklist_selected)
        self.candidates.addAction(ignore)
        self.candidates.addAction(blacklist)
        layout.addWidget(label)
        layout.addWidget(self.rare_worked_summary)
        layout.addWidget(self.rare_worked)
        return panel

    def _show_session_diagnostics(self, snapshot: dict[str, object]) -> None:
        selected = int(snapshot.get("candidates_selected", 0))
        sent = int(snapshot.get("replies_sent", 0))
        completed = int(snapshot.get("qso_completed", 0))
        refused = int(snapshot.get("candidates_refused", 0))
        busy = int(snapshot.get("stations_busy", 0))
        local_rejections = int(snapshot.get("local_rejections", 0))
        failures = int(snapshot.get("control_failures", 0))
        auto_watchdog = int(snapshot.get("autopilot_watchdogs", 0))
        wsjtx_watchdog = int(snapshot.get("wsjtx_watchdogs", 0))
        success_rate = snapshot.get("reply_success_percent")
        success_text = "—" if success_rate is None else f"{int(success_rate)} %"
        self.session_diagnostics.setText(
            f"SÉLECTIONS : {selected} · REPLIES ENVOYÉS : {sent} · QSO TERMINÉS : {completed} · RÉUSSITE : {success_text}\n"
            f"ÉCARTÉES : {refused} (DÉJÀ OCCUPÉES : {busy}) · REFUS WSJT-X : {local_rejections} · ÉCHECS : {failures}"
            f" · WATCHDOGS : AutoPilot {auto_watchdog} / WSJT-X {wsjtx_watchdog}"
        )
        recent = snapshot.get("recent", ())
        if isinstance(recent, (tuple, list)):
            self.session_diagnostic_log.setPlainText("\n".join(str(item) for item in recent[:3]))

    def _add_rare_station_worked(self, callsign: str, band: str) -> None:
        key = (callsign.upper(), band.lower())
        row = self._rare_worked_rows.get(key)
        if row is None:
            row = self.rare_worked.rowCount()
            self.rare_worked.insertRow(row)
            self._rare_worked_rows[key] = row
        for column, value in enumerate((datetime.now().strftime("%H:%M"), callsign.upper(), band, "TRAVAILLÉ")):
            self.rare_worked.setItem(row, column, QTableWidgetItem(value))
        self._update_rare_summary()

    def _add_rare_mqtt_spot(self, spot) -> None:
        callsign = str(getattr(spot, "callsign", "")).strip().upper()
        band = str(getattr(spot, "band", "")).strip().lower()
        observed_at = getattr(spot, "observed_at", None)
        if not callsign or not band:
            return
        key = (callsign, band)
        row = self._rare_worked_rows.get(key)
        if row is None:
            row = self.rare_worked.rowCount()
            self.rare_worked.insertRow(row)
            self._rare_worked_rows[key] = row
        timestamp = observed_at.strftime("%H:%M") if isinstance(observed_at, datetime) else datetime.now().strftime("%H:%M")
        state = self.rare_worked.item(row, 3)
        status = state.text() if state is not None and state.text() == "TRAVAILLÉ" else "VU MQTT"
        for column, value in enumerate((timestamp, callsign, band, status)):
            self.rare_worked.setItem(row, column, QTableWidgetItem(value))
        self._update_rare_summary()

    def _add_unconfirmed_dxcc_mqtt_spot(self, spot) -> None:
        """Show one accepted DXCC discovery without exposing the raw MQTT firehose."""
        callsign = str(getattr(spot, "callsign", "")).strip().upper()
        band = str(getattr(spot, "band", "")).strip().lower()
        observed_at = getattr(spot, "observed_at", None)
        if not callsign or not band:
            return
        key = (callsign, band)
        row = self._rare_worked_rows.get(key)
        if row is None:
            row = self.rare_worked.rowCount()
            self.rare_worked.insertRow(row)
            self._rare_worked_rows[key] = row
        timestamp = observed_at.strftime("%H:%M") if isinstance(observed_at, datetime) else datetime.now().strftime("%H:%M")
        state = self.rare_worked.item(row, 3)
        status = state.text() if state is not None and state.text() == "TRAVAILLÉ" else "DXCC MQTT"
        for column, value in enumerate((timestamp, callsign, band, status)):
            self.rare_worked.setItem(row, column, QTableWidgetItem(value))
        self._update_rare_summary()

    def _update_rare_summary(self) -> None:
        seen = self.rare_worked.rowCount()
        rare_seen = sum(
            1
            for row in range(seen)
            if (item := self.rare_worked.item(row, 3)) is not None and item.text() == "VU MQTT"
        )
        dxcc_seen = sum(
            1
            for row in range(seen)
            if (item := self.rare_worked.item(row, 3)) is not None and item.text() == "DXCC MQTT"
        )
        worked = sum(
            1
            for row in range(seen)
            if (item := self.rare_worked.item(row, 3)) is not None and item.text() == "TRAVAILLÉ"
        )
        self.rare_worked_summary.setText(
            f"MQTT : {rare_seen} rare(s) suivie(s) · {dxcc_seen} DXCC à confirmer · QSO : {worked}"
        )

    def _add_activity(self, row: ActivityRow) -> None:
        target = 0
        self.activity.insertRow(target)
        values = [
            row.observed_at.strftime("%H:%M:%S"),
            f"{row.snr:+d}",
            str(row.delta_frequency),
            row.message,
        ]
        for column, value in enumerate(values):
            self.activity.setItem(target, column, QTableWidgetItem(value))
        while self.activity.rowCount() > 250:
            self.activity.removeRow(self.activity.rowCount() - 1)
        parsed = parse_ft8_message(row.message)
        if parsed is not None:
            self.hunt_panel.note_local_decode(parsed.sender, row.observed_at)

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
        values = [
            row.station,
            f"{row.snr:+d}",
            row.dxcc,
            row.continent,
            str(row.score),
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
        suffix = f" | Next: {next_station}" if count else ""
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
        self._update_swr_reset_button()
        LOGGER.info("[GUI] ARM state update armed=%s", armed)
        LOGGER.info("[GUI] control state backend_armed=%s ui_armed=%s", armed, self._armed)

    def _show_swr_safety_notification(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "Protection SWR — AutoPilot désarmé",
            "AutoPilot a arrêté l’émission et s’est désarmé.\n\n"
            f"{message}\n\n"
            "Vérifiez l’antenne et l’accord avant de réarmer.",
        )

    def _show_status(self, status: StatusView) -> None:
        self.frequency.setText(status.frequency)
        self.mode.setText(status.mode)
        self.tx_state.setText(status.tx_state)
        self._local_grid = status.local_grid
        self._refresh_session_qso_map()

    def _add_session_qso_map_point(self, point: object) -> None:
        if not isinstance(point, SessionQsoMapPoint):
            return
        self._session_qso_points.append(point)
        self.session_map_button.setText(f"CARTE QSO ({len(self._session_qso_points)})")
        self._refresh_session_qso_map()

    def _show_session_qso_map(self) -> None:
        if self._session_map_dialog is None:
            self._session_map_dialog = SessionQsoMapDialog(self)
        self._refresh_session_qso_map()
        self._session_map_dialog.show()
        self._session_map_dialog.raise_()
        self._session_map_dialog.activateWindow()

    def _refresh_session_qso_map(self) -> None:
        if self._session_map_dialog is not None:
            self._session_map_dialog.set_snapshot(self._local_grid, tuple(self._session_qso_points))

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
        opportunities = snapshot.get("opportunities")
        if isinstance(opportunities, dict):
            available_calls = tuple(opportunities.get("available") or ())
            viable_count = int(opportunities.get("viable_count", len(available_calls)))
            elapsed = max(0, int(float(opportunities.get("quiet_elapsed_seconds") or 0)))
            threshold = max(1, int(float(opportunities.get("quiet_threshold_seconds") or 180)))
            remaining = max(0, int(float(opportunities.get("quiet_remaining_seconds") or 0)))

            def clock(seconds: int) -> str:
                minutes, seconds = divmod(seconds, 60)
                return f"{minutes:02d}:{seconds:02d}"

            if bool(opportunities.get("exhausted")):
                switch_text = "BANDE ÉPUISÉE"
            elif remaining <= 0:
                switch_text = "EN ATTENTE"
            else:
                switch_text = clock(remaining)
            self.opportunity_status.setText(
                f"VIABLES : {viable_count}   |   SANS NOUVELLE : {clock(elapsed)} / {clock(threshold)}"
                f"   |   PROCHAINE BASCULE : {switch_text}"
            )
        antenna = snapshot.get("antenna_safety")
        if isinstance(antenna, dict):
            level = str(antenna.get("level") or "ok")
            labels = {
                "ok": "●  OK",
                "warning": "⚠  SWR CORRIGÉ",
                "fault": "▲  !  ARRÊT SWR",
            }
            if antenna.get("recovering"):
                labels["warning"] = "⚠  ACCORD EN COURS"
            excluded = tuple(antenna.get("excluded_bands") or ())
            self._antenna_reset_available = level != "ok" or bool(excluded)
            self._update_swr_reset_button()
            suffix = f"  ({', '.join(excluded)} exclue{'s' if len(excluded) > 1 else ''})" if excluded else ""
            self.antenna_indicator.setText(labels.get(level, labels["ok"]) + suffix)
            self.antenna_indicator.setProperty("level", level)
            self.antenna_indicator.style().unpolish(self.antenna_indicator)
            self.antenna_indicator.style().polish(self.antenna_indicator)
        ftx1 = snapshot.get("ftx1")
        if isinstance(ftx1, dict):
            ftx1_band = ftx1.get("band") or current_band
            radio_model = str(ftx1.get("radio_model") or "Radio")
            saved_att = ftx1.get("saved_tx_audio_attenuation")
            po = ftx1.get("power_watts")
            requested_po = ftx1.get("requested_power_watts")
            power_text = (
                f"PO {float(po):.1f}/{float(requested_po):.0f} W | "
                if po is not None and requested_po is not None
                else f"PO {float(po):.1f} W | " if po is not None else ""
            )
            profile_text = (
                f"ATT mémorisée {saved_att}"
                if saved_att is not None
                else "No profile"
            )
            protection_labels = {
                "numeric": "SWR mesuré",
                "alarm": "alarme SWR",
                "unavailable": "protection SWR indisponible",
                "uncertain": "protection SWR incertaine",
            }
            protection_text = protection_labels.get(str(ftx1.get("swr_protection") or "uncertain"), "protection SWR incertaine")
            self.ftx1_status.setText(
                f"{radio_model}: {'Connectée' if ftx1.get('connected') else 'Déconnectée'} | "
                f"ATT {ftx1.get('current_tx_audio_attenuation') if ftx1.get('current_tx_audio_attenuation') is not None else '—'} | "
                f"{power_text}ALC {float(ftx1['alc']):.2f} / SWR {float(ftx1['swr']):.1f} | {protection_text} | {profile_text}"
                if ftx1.get("alc") is not None and ftx1.get("swr") is not None
                else
                f"{radio_model}: {'Connectée' if ftx1.get('connected') else 'Déconnectée'} | "
                f"ATT {ftx1.get('current_tx_audio_attenuation') if ftx1.get('current_tx_audio_attenuation') is not None else '—'} | {protection_text} | {profile_text}"
            )
            self.ftx1_status.setVisible(True)
            self.save_ftx1_current_band_profile_button.setVisible(False)
            self.delete_ftx1_current_band_profile_button.setVisible(False)
            self.reset_all_ftx1_band_profiles_button.setVisible(True)
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
        if state == "IDLE" or remote == "-":
            self.working_status.setText("Working -")
            return
        details = remote_detail.split(" / ") if remote_detail and remote_detail != "-" else []
        country = details[0] if details else ""
        if country.lower() == "unknown":
            country = ""
        location = details[3] if len(details) > 3 else ""
        self.working_status.setText(f"Working {remote}" + (f" {country}" if country else "") + (f" {location}" if location else ""))

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

    def _update_swr_reset_button(self) -> None:
        self.reset_swr_safety_button.setEnabled(self._antenna_reset_available and not self._armed)

    def _confirm_reset_swr_safety(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Réinitialiser la protection SWR ?",
            "Cette action efface toutes les bandes exclues et l'état d'arrêt SWR, puis réarme AutoPilot.\n\n"
            "Aucun accord ni test de bande ne sera lancé. La protection SWR restera active dès la prochaine émission.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        LOGGER.info("[GUI] SWR safety reset confirmation result=%r", answer)
        if answer == QMessageBox.StandardButton.Yes:
            self.reset_swr_safety_requested.emit()

    def _edit_settings(self) -> None:
        dialog = SettingsDialog(self._settings, self, self._ap1_available)
        dialog.settings_applied.connect(self._apply_settings)
        dialog.exec()

    def _apply_settings(self, settings: UserSettings) -> None:
        self.hunt_panel.set_hunting(False)
        self.hunt_stop_requested.emit()
        self._settings = settings
        self._store.save(self._settings)
        set_file_log_level(self._settings.logging_level)
        self.apply_settings_requested.emit(self._settings)
        self._restart_special_refresh_timer()
        self._restart_wavelog_refresh_timer()
        self._refresh_special_calls()
        self._refresh_wavelog_lotw()
        self.statusBar().showMessage("Preferences saved; backend restarted DISARMED", 6000)

    def _on_update_button_clicked(self) -> None:
        if self._available_update is None:
            self._check_for_updates(manual=True)
            return
        self._offer_update(self._available_update)

    def _check_for_updates(self, manual: bool = False) -> None:
        if self._update_check_poller is not None and self._update_check_poller.isRunning():
            return
        if version_key(self._current_version) is None:
            if manual:
                QDesktopServices.openUrl(QUrl(RELEASES_PAGE_URL))
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("VÉRIFICATION…")
        poller = UpdateCheckPoller(self._current_version)
        self._update_check_poller = poller
        poller.completed.connect(lambda result: self._show_update_check(result, manual))
        poller.finished.connect(poller.deleteLater)
        poller.start()

    def _show_update_check(self, result: object, manual: bool) -> None:
        self._update_check_poller = None
        self.update_button.setEnabled(True)
        if not isinstance(result, UpdateCheckResult):
            self.update_button.setText("MISES À JOUR")
            return
        if result.available and result.release is not None:
            self._available_update = result.release
            self.update_button.setText(f"MISE À JOUR v{result.release.version}")
            self.update_button.setProperty("available", True)
            self.update_button.style().unpolish(self.update_button)
            self.update_button.style().polish(self.update_button)
            self.statusBar().showMessage(
                f"Mise à jour v{result.release.version} disponible — clique sur le bouton MISE À JOUR.",
                10_000,
            )
            if manual:
                self._offer_update(result.release)
            return
        self.update_button.setText("À JOUR")
        self.update_button.setProperty("available", False)
        self.update_button.style().unpolish(self.update_button)
        self.update_button.style().polish(self.update_button)
        if manual:
            self.statusBar().showMessage(result.warning or "AutoPilot est déjà à jour.", 6_000)
        elif result.warning:
            LOGGER.info("[UPDATE] background check skipped: %s", result.warning)

    def _offer_update(self, release: UpdateRelease) -> None:
        if release.installer is None or release.checksums is None:
            answer = QMessageBox.information(
                self,
                "Mise à jour disponible",
                f"La version {release.version} est disponible, mais son installateur vérifiable n'est pas encore joint à la release.\n\n"
                "La page GitHub va s'ouvrir pour consulter les notes et téléchargements.",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Open,
            )
            if answer == QMessageBox.StandardButton.Open:
                QDesktopServices.openUrl(QUrl(release.release_url))
            return
        answer = QMessageBox.question(
            self,
            "Installer la mise à jour ?",
            f"Télécharger et vérifier WSJTX AutoPilot v{release.version} ?\n\n"
            "L'installateur ne sera lancé qu'après vérification SHA-256. AutoPilot restera ouvert pendant le téléchargement.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("TÉLÉCHARGEMENT…")
        paths = AppPaths.from_environment().ensure_directories()
        poller = UpdateDownloadPoller(release, paths.data_dir / "updates")
        self._update_download_poller = poller
        poller.completed.connect(self._offer_verified_installer)
        poller.failed.connect(self._show_update_download_error)
        poller.finished.connect(poller.deleteLater)
        poller.start()

    def _show_update_download_error(self, message: str) -> None:
        self._update_download_poller = None
        self.update_button.setEnabled(True)
        self.update_button.setText("MISE À JOUR")
        QMessageBox.warning(self, "Mise à jour non installée", message)

    def _offer_verified_installer(self, installer: object) -> None:
        self._update_download_poller = None
        self.update_button.setEnabled(True)
        self.update_button.setText("MISE À JOUR PRÊTE")
        path = Path(str(installer))
        answer = QMessageBox.question(
            self,
            "Mise à jour vérifiée",
            "La somme SHA-256 est correcte. AutoPilot va se fermer, s'installer discrètement, puis redémarrer. Continuer ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("Installateur vérifié, prêt lorsque tu cliqueras à nouveau sur MISE À JOUR.", 8_000)
            return
        installer_arguments = [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
        ]
        if not QProcess.startDetached(str(path), installer_arguments):
            QMessageBox.critical(self, "Mise à jour", "Impossible de démarrer l'installateur vérifié.")
            return
        self.statusBar().showMessage("Mise à jour silencieuse lancée : arrêt d'AutoPilot…")
        QTimer.singleShot(0, self.close)

    def _restart_special_refresh_timer(self) -> None:
        interval_ms = max(5, self._settings.special_calls_refresh_minutes) * 60_000
        self._special_refresh_timer.setInterval(interval_ms)
        self._special_refresh_timer.start()

    def _restart_wavelog_refresh_timer(self) -> None:
        interval_ms = max(5, self._settings.wavelog_lotw_refresh_minutes) * 60_000
        self._wavelog_refresh_timer.setInterval(interval_ms)
        self._wavelog_refresh_timer.start()

    def _refresh_wavelog_lotw(self) -> None:
        if self._wavelog_lotw_poller is not None and self._wavelog_lotw_poller.isRunning():
            return
        if not self._settings.wavelog_api_url or not self._settings.wavelog_api_token:
            if self.unconfirmed_dxcc_only_action.isChecked():
                self.statusBar().showMessage("DXCC LoTW : configure l'URL et le jeton API Wavelog v2", 7000)
            return
        from wsjtx_autopilot.wavelog import WavelogLotwCatalog

        paths = AppPaths.from_environment().ensure_directories()
        self._wavelog_lotw_poller = WavelogLotwPoller(
            WavelogLotwCatalog(paths.data_dir / "wavelog_lotw_confirmations.json"),
            self._settings.wavelog_api_url,
            self._settings.wavelog_api_token,
        )
        self._wavelog_lotw_poller.completed.connect(self._wavelog_lotw_updated)
        self._wavelog_lotw_poller.finished.connect(self._wavelog_lotw_poller.deleteLater)
        self._wavelog_lotw_poller.start()

    def _wavelog_lotw_updated(self, snapshot: object) -> None:
        self.wavelog_lotw_updated_requested.emit(snapshot)
        self._wavelog_lotw_poller = None

    def _show_wavelog_lotw_state(self, entities: int, unresolved: int, available: bool, warning: str) -> None:
        if not available:
            if self.unconfirmed_dxcc_only_action.isChecked():
                self.statusBar().showMessage(
                    f"DXCC LoTW : synchronisation indisponible ({warning or 'aucun cache'}) — appels bloqués par sécurité",
                    9000,
                )
            return
        suffix = f" · cache ({warning})" if warning else ""
        detail = f" · {unresolved} indicatif(s) non résolu(s)" if unresolved else ""
        self.statusBar().showMessage(f"Wavelog LoTW : {entities} DXCC validés{detail}{suffix}", 7000)

    def _refresh_special_calls(self) -> None:
        if self._special_call_poller is not None and self._special_call_poller.isRunning():
            return
        paths = AppPaths.from_environment().ensure_directories()
        from wsjtx_autopilot.special_calls import SpecialCallCatalog

        self.hunt_panel.refresh_button.setEnabled(False)
        self.hunt_panel.status.setText("Vérification des calendriers DX, clusters et PSK Reporter…")
        self._special_call_poller = SpecialCallPoller(
            SpecialCallCatalog(paths.data_dir / "special_calls.json"),
            self._settings.special_calls_api_key,
            self._settings.special_calls_lookback_minutes,
            self._settings.allowed_auto_hop_bands,
            self._local_grid,
        )
        self._special_call_poller.completed.connect(self._special_calls_updated)
        self._special_call_poller.finished.connect(self._special_call_poller.deleteLater)
        self._special_call_poller.start()

    def _special_calls_updated(self, snapshot) -> None:
        self.hunt_panel.refresh_button.setEnabled(True)
        self.hunt_panel.set_snapshot(snapshot)
        self.special_calls_updated_requested.emit(tuple(snapshot.activities))
        if self._pskreporter_mqtt is not None:
            self._pskreporter_mqtt.set_callsigns(self.hunt_panel.all_callsigns())
            self._pskreporter_mqtt.set_discovery_bands(self._unconfirmed_dxcc_discovery_bands())
        self._special_call_poller = None

    def _set_reply_rare_tracking(self, enabled: bool) -> None:
        self._settings.pskreporter_mqtt_enabled = bool(enabled)
        # This main-screen mode deliberately combines tracking and scoring:
        # a rare locally decoded CQ is ahead of ordinary CQ candidates.
        self._settings.prioritize_special_calls_in_reply = bool(enabled)
        self._store.save(self._settings)
        self.reply_rare_tracking_requested.emit(bool(enabled))
        if enabled:
            self._start_pskreporter_mqtt()
            self.statusBar().showMessage("Reply : indicatifs rares prioritaires, suivi MQTT PSK Reporter activé", 7000)
        else:
            if self.rare_stations_only_action.isChecked():
                self.rare_stations_only_action.setChecked(False)
            if not self.unconfirmed_dxcc_only_action.isChecked():
                self._stop_pskreporter_mqtt()
            self.statusBar().showMessage("Reply : suivi MQTT des indicatifs rares désactivé", 5000)
        self._update_reply_mode_summary()

    def _set_rare_stations_only(self, enabled: bool) -> None:
        if enabled and not self.rare_reply_tracking_action.isChecked():
            self.rare_reply_tracking_action.setChecked(True)
        self._settings.rare_stations_only = bool(enabled)
        self._store.save(self._settings)
        self.rare_stations_only_requested.emit(bool(enabled))
        self.statusBar().showMessage(
            "Reply : stations rares uniquement" if enabled else "Reply : stations classiques à nouveau autorisées",
            5000,
        )
        self._update_reply_mode_summary()

    def _set_unconfirmed_dxcc_only(self, enabled: bool) -> None:
        self._settings.wavelog_unconfirmed_dxcc_only = bool(enabled)
        self._store.save(self._settings)
        self.unconfirmed_dxcc_only_requested.emit(bool(enabled))
        if enabled:
            self._start_pskreporter_mqtt()
            self._refresh_wavelog_lotw()
            self.statusBar().showMessage(
                "Reply : priorité aux DXCC non validés LoTW — synchronisation Wavelog en attente",
                7000,
            )
        else:
            if self._pskreporter_mqtt is not None:
                self._pskreporter_mqtt.set_discovery_bands(())
            if not self.rare_reply_tracking_action.isChecked():
                self._stop_pskreporter_mqtt()
            self.statusBar().showMessage("Reply : priorité DXCC LoTW désactivée", 5000)
        self._update_reply_mode_summary()

    def _set_favorable_path_priority(self, enabled: bool) -> None:
        self._settings.prioritize_favorable_path_in_reply = bool(enabled)
        self._store.save(self._settings)
        self.favorable_path_priority_requested.emit(bool(enabled))
        if enabled:
            self._refresh_space_weather()
            self.statusBar().showMessage("Reply : priorité chemin favorable activée (SFI / Kp en cours de mise à jour)", 7000)
        else:
            self.statusBar().showMessage("Reply : priorité chemin favorable désactivée", 5000)
        self._update_reply_mode_summary()

    def _refresh_space_weather(self) -> None:
        if self._space_weather_poller is not None and self._space_weather_poller.isRunning():
            return
        self._space_weather_poller = SpaceWeatherPoller()
        self._space_weather_poller.completed.connect(self._space_weather_updated)
        self._space_weather_poller.finished.connect(self._space_weather_poller.deleteLater)
        self._space_weather_poller.start()

    def _space_weather_updated(self, snapshot: object) -> None:
        self.space_weather_updated_requested.emit(snapshot)
        if self.favorable_path_priority_action.isChecked():
            solar_flux = getattr(snapshot, "solar_flux", None)
            kp = getattr(snapshot, "kp", None)
            warning = str(getattr(snapshot, "warning", ""))
            if solar_flux is not None or kp is not None:
                details = " · ".join(
                    value for value in (
                        f"SFI {float(solar_flux):.0f}" if solar_flux is not None else "",
                        f"Kp {float(kp):.1f}" if kp is not None else "",
                    ) if value
                )
                self.statusBar().showMessage(f"Reply : chemin favorable — {details}", 6000)
            elif warning:
                self.statusBar().showMessage(f"Reply : chemin favorable sans données NOAA ({warning})", 7000)
        self._space_weather_poller = None

    def _set_exclude_worked_history(self, enabled: bool) -> None:
        self._settings.exclude_previously_worked_calls = bool(enabled)
        self._store.save(self._settings)
        self.exclude_worked_history_requested.emit(bool(enabled))
        self.statusBar().showMessage(
            "Reply : historique des contacts exclu" if enabled else "Reply : stations déjà contactées à nouveau autorisées",
            5000,
        )
        self._update_reply_mode_summary()

    def _update_reply_mode_summary(self) -> None:
        enabled: list[str] = []
        if self.rare_reply_tracking_action.isChecked():
            enabled.append("Rares + MQTT")
        if self.rare_stations_only_action.isChecked():
            enabled.append("Rares uniquement")
        if self.unconfirmed_dxcc_only_action.isChecked():
            enabled.append("DXCC LoTW")
        if self.favorable_path_priority_action.isChecked():
            enabled.append("Chemin favorable")
        if self.exclude_worked_history_action.isChecked():
            enabled.append("Historique exclu")
        self.reply_mode_summary.setText(
            "MODE STANDARD" if not enabled else "ACTIF : " + " · ".join(enabled)
        )

    def _unconfirmed_dxcc_discovery_bands(self) -> tuple[str, ...]:
        if not self.unconfirmed_dxcc_only_action.isChecked():
            return ()
        return tuple(self._settings.allowed_auto_hop_bands)

    def _start_pskreporter_mqtt(self) -> None:
        if self._pskreporter_mqtt is not None and self._pskreporter_mqtt.isRunning():
            self._pskreporter_mqtt.set_callsigns(self.hunt_panel.all_callsigns())
            self._pskreporter_mqtt.set_discovery_bands(self._unconfirmed_dxcc_discovery_bands())
            return
        self._pskreporter_mqtt = PskReporterMqttPoller(
            self.hunt_panel.all_callsigns(),
            self._unconfirmed_dxcc_discovery_bands(),
        )
        self._pskreporter_mqtt.spot_received.connect(self.hunt_panel.note_remote_spot)
        self._pskreporter_mqtt.spot_received.connect(self._add_rare_mqtt_spot)
        self._pskreporter_mqtt.spot_received.connect(self.remote_spot_received)
        self._pskreporter_mqtt.discovery_spot_received.connect(self.unconfirmed_dxcc_remote_spot_received)
        self._pskreporter_mqtt.state_changed.connect(self.hunt_panel.set_mqtt_state)
        self._pskreporter_mqtt.finished.connect(self._pskreporter_mqtt.deleteLater)
        self._pskreporter_mqtt.start()

    def _stop_pskreporter_mqtt(self) -> None:
        if self._pskreporter_mqtt is not None and self._pskreporter_mqtt.isRunning():
            self._pskreporter_mqtt.requestInterruption()

    def _start_target_hunt(self, callsign: str) -> None:
        activities = tuple(item for item in self.hunt_panel.activities() if item.callsign == callsign)
        if activities:
            self.hunt_requested.emit(activities, callsign)
            self.statusBar().showMessage(f"Hunt ciblé : {callsign} — attente d'un Decode local", 8000)

    def _start_all_hunt(self, activities) -> None:
        self.hunt_requested.emit(tuple(activities), "")
        self.statusBar().showMessage("Hunt automatique des indicatifs spéciaux actifs", 8000)

    def _show_hunt_state(self, active: bool, target: str) -> None:
        self.hunt_panel.set_hunting(active, target)
        if not active:
            self.statusBar().showMessage("Hunt arrêté", 5000)

    def _update_ftx1_band_profiles(self, profiles: dict[str, dict[str, int]]) -> None:
        self._settings.ftx1_band_profiles = deepcopy(profiles)
        self._store.save(self._settings)
        current_band = self.band_status.text().replace("BAND: ", "")
        self.statusBar().showMessage(f"Saved {current_band} profile", 5000)

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
        LOGGER.info("[SHUTDOWN] requested")
        self._special_refresh_timer.stop()
        self._wavelog_refresh_timer.stop()
        self._space_weather_refresh_timer.stop()
        self._stop_pskreporter_mqtt()
        if self._special_call_poller is not None and self._special_call_poller.isRunning():
            self._special_call_poller.requestInterruption()
        if self._wavelog_lotw_poller is not None and self._wavelog_lotw_poller.isRunning():
            self._wavelog_lotw_poller.requestInterruption()
        if self._space_weather_poller is not None and self._space_weather_poller.isRunning():
            self._space_weather_poller.requestInterruption()
        if self._update_check_poller is not None and self._update_check_poller.isRunning():
            self._update_check_poller.requestInterruption()
        if self._update_download_poller is not None and self._update_download_poller.isRunning():
            self._update_download_poller.requestInterruption()
        # Every external poller has a bounded socket/request timeout.  They
        # are asked to stop before waiting so they cannot leave a hidden
        # Python/Qt process behind after the main window disappears.
        if self._pskreporter_mqtt is not None and self._pskreporter_mqtt.isRunning():
            self._pskreporter_mqtt.wait(11_000)
        if self._special_call_poller is not None and self._special_call_poller.isRunning():
            self._special_call_poller.wait(6_000)
        if self._wavelog_lotw_poller is not None and self._wavelog_lotw_poller.isRunning():
            self._wavelog_lotw_poller.wait(9_000)
        if self._space_weather_poller is not None and self._space_weather_poller.isRunning():
            self._space_weather_poller.wait(6_000)
        if self._update_check_poller is not None and self._update_check_poller.isRunning():
            self._update_check_poller.wait(9_000)
        if self._update_download_poller is not None and self._update_download_poller.isRunning():
            self._update_download_poller.wait(22_000)
        self.stop_requested.emit()
        if not self._thread.wait(8_000):
            # Do not hide the window while a radio-owning backend is still
            # alive.  Keeping it visible makes the incomplete shutdown clear
            # and avoids the locked executable symptom on the next update.
            LOGGER.error("[SHUTDOWN] backend thread did not stop within timeout")
            self.statusBar().showMessage("Arrêt en cours : backend radio encore actif. Réessaie dans quelques secondes.")
            event.ignore()
            return
        lingering = [
            name
            for name, poller in (
                ("MQTT", self._pskreporter_mqtt),
                ("indicatifs spéciaux", self._special_call_poller),
                ("Wavelog", self._wavelog_lotw_poller),
                ("NOAA", self._space_weather_poller),
                ("mise à jour", self._update_check_poller),
                ("téléchargement mise à jour", self._update_download_poller),
            )
            if poller is not None and poller.isRunning()
        ]
        if lingering:
            LOGGER.error("[SHUTDOWN] pollers still running: %s", ", ".join(lingering))
            self.statusBar().showMessage(f"Arrêt en cours : {', '.join(lingering)} encore actif(s).")
            event.ignore()
            return
        LOGGER.info("[SHUTDOWN] complete")
        event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
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
        )

    def _fit_control_buttons(self) -> None:
        for button in self._profile_control_buttons:
            button.setMinimumWidth(button.sizeHint().width())


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

