import logging
from copy import deepcopy

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.config import SettingsStore, UserSettings

# Kept here because SettingsTabsMixin resolves this symbol at call time, so
# existing monkeypatches of ``main_window.list_supported_radios`` still apply.
from wsjtx_autopilot.hamlib import list_supported_radios  # noqa: F401
from wsjtx_autopilot.logging_setup import build_metadata, set_file_log_level
from wsjtx_autopilot.updates import UpdateRelease, version_key

from .activity_panel import ActivityPanelMixin
from .adaptive_panel import AdaptivePanelMixin
from .hunt import HuntPanel, PskReporterMqttPoller, SpaceWeatherPoller, SpecialCallPoller, WavelogLotwPoller
from .integrations import IntegrationPanelMixin
from .session_map import SessionQsoMapDialog
from .settings_dialog import SettingsDialog
from .style import apply_main_window_style, fit_control_buttons
from .update_panel import UpdatePanelMixin
from .updates import UpdateCheckPoller, UpdateDownloadPoller
from .viewmodels import SessionQsoMapPoint
from .worker import BackendWorker

LOGGER = logging.getLogger(__name__)




class MainWindow(
    ActivityPanelMixin,
    AdaptivePanelMixin,
    UpdatePanelMixin,
    IntegrationPanelMixin,
    QMainWindow,
):
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
        apply_main_window_style(self)

    def _fit_control_buttons(self) -> None:
        fit_control_buttons(self._profile_control_buttons)

