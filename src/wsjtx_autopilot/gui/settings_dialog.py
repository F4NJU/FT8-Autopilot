"""Application settings dialog."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.config import ActivityPolicy, DirectCallPolicy, UserSettings

from .settings_tabs import SettingsTabsMixin
from .settings_widgets import _csv_set


class SettingsDialog(QDialog, SettingsTabsMixin):
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
        # The Reply menu couples these two flags: rare tracking supplies the
        # special-call list, while the dialog only exposes the scoring toggle.
        # Keep one rule in both places so the menu and the dialog cannot drift.
        rare_tracking = (
            self.special_call_priority.isChecked()
            or self.rare_stations_only.isChecked()
            or self.unconfirmed_dxcc_only.isChecked()
        )
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
            exclude_previously_worked_calls=self.exclude_worked_history.isChecked(),
            minimum_snr=minimum_snr,
            favor_strong_signals=self.strong_signals.isChecked(),
            pota_policy=ActivityPolicy(self.pota_policy.currentData()),
            sota_policy=ActivityPolicy(self.sota_policy.currentData()),
            qrp_policy=ActivityPolicy(self.qrp_policy.currentData()),
            pskreporter_mqtt_enabled=rare_tracking,
            prioritize_special_calls_in_reply=self.special_call_priority.isChecked(),
            rare_stations_only=self.rare_stations_only.isChecked(),
            prioritize_favorable_path_in_reply=self.favorable_path_priority.isChecked(),
            respond_to_cq_dx=self.respond_cq_dx.isChecked(),
            blacklist=_csv_set(self.blacklist.text()),
            ignore_minutes=self.ignore_minutes.value(),
            cty_dat_path=self.cty_path.text().strip() or None,
            wsjtx_log_path=self.adif_path.text().strip() or None,
            launch_wsjtx_with_autopilot=self.manage_wsjtx.isChecked(),
            wsjtx_executable_path=self.wsjtx_executable_path.text().strip(),
            sync_adif_on_startup=self.sync_adif.isChecked(),
            direct_reply_patched=False,
            max_actions=self.max_actions.value(),
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
            ftx1_auto_apply_band_profiles=self.ftx1_auto_apply_band_profiles.isChecked(),
            ftx1_band_profiles=self._settings.ftx1_band_profiles,
            discord_presence_enabled=self.discord_presence_enabled.isChecked(),
            discord_application_id=self.discord_application_id.text().strip(),
            special_calls_api_key=self.special_calls_api_key.text().strip(),
            special_calls_refresh_minutes=self.special_calls_refresh.value(),
            special_calls_lookback_minutes=self.special_calls_lookback.value(),
            wavelog_api_url=self.wavelog_api_url.text().strip(),
            wavelog_unconfirmed_dxcc_only=self.unconfirmed_dxcc_only.isChecked(),
            wavelog_api_token=self.wavelog_api_token.text().strip(),
            wavelog_lotw_refresh_minutes=self.wavelog_lotw_refresh.value(),
            logging_level=self.logging_level.currentData(),
        )

        # Tab builders live in SettingsTabsMixin.

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


