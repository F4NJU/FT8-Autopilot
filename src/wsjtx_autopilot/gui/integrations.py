"""Optional Wavelog/LoTW, special-call, space-weather and PSK Reporter panels."""

from __future__ import annotations

from wsjtx_autopilot.config import AppPaths

from .hunt import PskReporterMqttPoller, SpaceWeatherPoller, SpecialCallPoller, WavelogLotwPoller


class IntegrationPanelMixin:
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

