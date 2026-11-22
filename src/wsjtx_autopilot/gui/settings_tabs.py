"""Settings tab builders extracted verbatim from :mod:`main_window`.

The methods live in a mixin so every widget remains an attribute of the
``SettingsDialog`` instance: public names used by callers and tests therefore
do not change.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.config import AppPaths, DirectCallPolicy
from wsjtx_autopilot.logging_setup import current_logging_session, export_diagnostic

from .settings_widgets import _activity_policy_combo, _path_picker

LOGGER = logging.getLogger(__name__)


def _supported_radio_models(rigctld_path: str):
    """Late-bound lookup so patching ``main_window.list_supported_radios`` works."""

    from . import main_window  # noqa: PLC0415

    return main_window.list_supported_radios(rigctld_path)


class SettingsTabsMixin:
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
        for model_number, manufacturer, model_name in _supported_radio_models(self._settings.ftx1_hamlib_rigctld_path):
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
            "Expérimental, désactivé par défaut. Demande une porteuse Tune à AP1 "
            "après une bascule de bande confirmée. Nécessite un AP1 avec les "
            "messages privés 24/25 (SetTune/TuneState), absent de l'AP1 intégré "
            "à la v1.0.4 : sans accusé, AutoPilot reprend après un cycle de "
            "sécurité. N'active pas une mesure SWR si le poste ne la fournit pas."
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
