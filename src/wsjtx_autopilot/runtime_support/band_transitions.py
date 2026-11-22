"""Pending dial/band confirmation and FTX1 band-drive settlement."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from wsjtx_autopilot.engine.adaptive import AdaptiveState
from wsjtx_autopilot.worked.bands import BandResolver
from wsjtx_autopilot.wsjtx.models import StatusPacket

from .constants import _tr_period_seconds  # noqa: F401 - shared helper
from .models import PendingDialFrequency

LOGGER = logging.getLogger(__name__)


class BandTransitionMixin:
    def _confirm_dial_frequency(self, packet: StatusPacket, now: datetime) -> None:
        pending = self._pending_dial_frequency
        if pending is None or packet.header.instance_id != pending.instance_id:
            return
        if packet.dial_frequency != pending.frequency_hz:
            return
        self._pending_dial_frequency = None
        if pending.source == "forced":
            LOGGER.info("[BAND-HOP] Frequency confirmed: %s / %.3f MHz", pending.band, pending.frequency_hz / 1_000_000)
            if self.ftx1_band_drive is None or not self.config.ftx1_auto_apply_band_profiles:
                self._block_for_unready_band_drive(pending, "automatic per-band drive profiles are unavailable")
                return
        if self._requires_band_drive_restore(pending):
            self._pending_ftx1_band_change = pending
            self._complete_pending_ftx1_band_change(now)
            return
        self._finish_band_change(pending, _tr_period_seconds(packet.tr_period), now)

    def _observe_manual_band_change(self, packet: StatusPacket, now: datetime) -> None:
        band = BandResolver().resolve(packet.dial_frequency)
        previous_band = self._last_reported_band
        self._last_reported_band = band
        if (
            band is None
            or previous_band is None
            or band == previous_band
            or self._pending_dial_frequency is not None
            or self._pending_ftx1_band_change is not None
            or self._pending_ftx1_profile_apply is not None
            or self.status.transmitting
            or self.ftx1_band_drive is None
            or not self.config.ftx1_auto_apply_band_profiles
        ):
            return
        if band.lower() not in self.ftx1_band_drive.profiles:
            LOGGER.info("[DRIVE] No saved drive profile for manually selected %s", band)
            return
        LOGGER.info("[DRIVE] Manual band change detected: restoring saved drive profile for %s", band)
        self._pending_ftx1_band_change = PendingDialFrequency(
            packet.header.instance_id,
            packet.dial_frequency,
            band,
            now + timedelta(seconds=self.config.dial_change_confirmation_timeout_seconds),
            previous_band,
            "manual",
        )
        self._complete_pending_ftx1_band_change(now)

    def _complete_pending_ftx1_band_change(self, now: datetime) -> None:
        pending = self._pending_ftx1_band_change
        if pending is None or self.status.transmitting:
            return
        assert self.ftx1_band_drive is not None
        if pending.band.lower() not in self.ftx1_band_drive.profiles:
            self._pending_ftx1_band_change = None
            if pending.source == "forced":
                self._block_for_unready_band_drive(pending, "no saved destination drive profile")
                return
            self.ftx1_band_drive.apply_profile(pending.band, self.current_tx_audio_attenuation)
            self._finish_band_change(pending, self.status.period, now)
            return
        if self.current_tx_audio_attenuation is None:
            self._query_tx_audio_attenuation_if_needed(pending.instance_id, now)
            return
        self._pending_ftx1_band_change = None
        if pending.source == "forced":
            LOGGER.info("[DRIVE] Restoring saved drive profile for %s", pending.band)
            profile = self.ftx1_band_drive.profiles[pending.band.lower()]
            LOGGER.info("[DRIVE] Requesting TX attenuation: %d", profile.tx_audio_attenuation)
        applied = self.ftx1_band_drive.apply_profile(
            pending.band,
            self.current_tx_audio_attenuation,
            force_attenuation=pending.source == "forced",
        )
        if not applied:
            if pending.source == "forced":
                self._block_for_unready_band_drive(pending, "destination drive profile could not be applied")
                return
            self._finish_band_change(pending, self.status.period, now)
            return
        if self.pending_tx_audio_attenuation is not None:
            self._pending_ftx1_profile_apply = pending
            if pending.source == "forced":
                LOGGER.info("[DRIVE] Waiting for attenuation confirmation")
            return
        self._finish_band_change(pending, self.status.period, now)

    def _finish_pending_ftx1_profile_apply(self, now: datetime) -> None:
        pending = self._pending_ftx1_profile_apply
        if pending is None or self.pending_tx_audio_attenuation is not None:
            return
        self._pending_ftx1_profile_apply = None
        if pending.source == "forced":
            LOGGER.info("[BAND-HOP] Drive state ready")
        self._finish_band_change(pending, self.status.period, now)

    def _finish_band_change(self, pending: PendingDialFrequency, period_seconds: int | None, now: datetime) -> None:
        self.adaptive_state = AdaptiveState.BAND_TRIAL
        self.stagnation.reset()
        self.opportunities.reset(pending.band, self.status.mode or "unknown", now)
        self.last_band_change_at = now
        if pending.source == "swr_recovery":
            if self._swr_recovery is not None:
                self._swr_recovery = None
                self._hamlib_swr_fault_latched = False
                self._hamlib_high_swr_samples = 0
                self.antenna_safety_level = "warning"
                self._antenna_event(
                    now,
                    f"Bande {pending.band} confirmée, reprise sans nouvel accord automatique",
                )
                self.last_qso_notice = f"SWR BAND EXCLUDED - resumed on {pending.band} without tune"
            self._band_resume_at = now + timedelta(seconds=period_seconds or 15)
            self._band_resume_band = pending.band
            LOGGER.info("[BAND] SWR recovery confirmed=%s without retune", pending.band)
            return
        if self._begin_atu100_tune_after_band_change(pending, now):
            return
        if pending.source == "forced":
            self._start_forced_band_hop_dwell(pending.band, now)
            settle_seconds = self.config.forced_band_hop_settle_seconds
            self._band_resume_at = now + timedelta(seconds=settle_seconds)
            self._band_resume_band = pending.band
            LOGGER.info("[BAND-HOP] Confirmed on %s", pending.band)
            LOGGER.info("[BAND-HOP] Settling for %g s", settle_seconds)
        else:
            self._band_resume_at = now + timedelta(seconds=period_seconds or 15)
            self._band_resume_band = None
        LOGGER.info("[BAND] confirmed=%s", pending.band)


    def _requires_band_drive_restore(self, pending: PendingDialFrequency) -> bool:
        # A safety recovery must never wait on a WSJT-X audio profile: the
        # tuner and RF power are controlled and verified directly via Hamlib.
        if pending.source == "swr_recovery":
            return False
        return self.ftx1_band_drive is not None and self.config.ftx1_auto_apply_band_profiles

    def _block_for_unready_band_drive(self, pending: PendingDialFrequency, reason: str) -> None:
        self._blocked_band_drive_change = pending
        LOGGER.warning("[BAND-HOP] Drive state for %s is not ready: %s; automatic TX remains blocked", pending.band, reason)

    def _expire_dial_frequency_request(self, now: datetime) -> None:
        pending = self._pending_dial_frequency
        if pending is None or now <= pending.deadline:
            return
        if pending.source == "swr_recovery":
            self._pending_dial_frequency = None
            self._finish_swr_recovery_fault(now, f"confirmation de la bande {pending.band} expirée")
            return
        if pending.source == "forced":
            LOGGER.warning("[BAND-HOP] Request to %s / %.3f MHz timed out waiting for Status", pending.band, pending.frequency_hz / 1_000_000)
            self._forced_band_hop_pending_band = None
            current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            if current_band is not None:
                self._start_forced_band_hop_dwell(current_band, now)
        else:
            LOGGER.error("[BAND] requested=%d confirmation timeout", pending.frequency_hz)
        self._pending_dial_frequency = None
        self.adaptive_state = AdaptiveState.NORMAL
