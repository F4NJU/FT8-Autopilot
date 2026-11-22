"""Adaptive strategy and FTX1 status rendering."""

from __future__ import annotations


class AdaptivePanelMixin:
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

