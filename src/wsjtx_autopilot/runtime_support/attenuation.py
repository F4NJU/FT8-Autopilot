"""Confirmed TX audio attenuation and band-profile application."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl

LOGGER = logging.getLogger(__name__)
_ATTENUATION_QUERY_INTERVAL = timedelta(seconds=2)


class AttenuationMixin:
    def set_tx_audio_attenuation_confirmed(self, attenuation: int, timeout_seconds: float = 2.0) -> bool:
        if not 0 <= attenuation <= 450:
            LOGGER.info("[DRIVE] action skipped reason=OUT_OF_BOUNDS")
            return False
        if self.status.transmitting:
            LOGGER.info("[DRIVE] action skipped reason=TX_ACTIVE")
            return False
        if not self.status.instance_id:
            LOGGER.info("[DRIVE] action skipped reason=ATTENUATION_UNKNOWN")
            return False
        if self.pending_tx_audio_attenuation is not None:
            LOGGER.info("[DRIVE] action skipped reason=PENDING_CONFIRMATION")
            return False
        if not isinstance(self.control, WsjtxUdpControl) or not self.control.set_tx_audio_attenuation(self.status.instance_id, attenuation):
            LOGGER.info("[DRIVE] action skipped reason=AP1_SEND_FAILED")
            return False
        LOGGER.info("[DRIVE] applying attenuation %s -> %d", self.current_tx_audio_attenuation, attenuation)
        self.pending_tx_audio_attenuation = attenuation
        self._tx_audio_attenuation_deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        LOGGER.info("[WSJTX] Request TX attenuation=%d", attenuation)
        return True

    def _request_ftx1_profile_attenuation(self, attenuation: int) -> bool:
        """Apply a user-saved attenuation only while WSJT-X is confirmed in RX."""
        return not self.status.transmitting and self.set_tx_audio_attenuation_confirmed(attenuation)

    def _observe_tx_audio_attenuation(self, attenuation: int, now: datetime) -> None:
        previous = self.current_tx_audio_attenuation
        requested = self.pending_tx_audio_attenuation
        LOGGER.info("[WSJTX] AP1 attenuation state packet received value=%d", attenuation)
        self.current_tx_audio_attenuation = attenuation
        self.tx_audio_attenuation_last_update = now
        LOGGER.info("[WSJTX] current_tx_audio_attenuation=%d", self.current_tx_audio_attenuation)
        if requested == attenuation:
            self.pending_tx_audio_attenuation = None
            self._tx_audio_attenuation_deadline = None
            LOGGER.info("[WSJTX] TX attenuation confirmed=%d", attenuation)
            if self._pending_alc_profile_band is not None and self.ftx1_band_drive is not None:
                self.ftx1_band_drive.update_profile_attenuation(self._pending_alc_profile_band, attenuation)
                self._pending_alc_profile_band = None
        elif previous is not None and previous != attenuation:
            LOGGER.info("[WSJTX] Manual TX attenuation change %d -> %d", previous, attenuation)
        else:
            LOGGER.info("[WSJTX] TX attenuation state=%d", attenuation)
        pending_save = self._pending_ftx1_profile_save_band
        if pending_save is not None and self.ftx1_band_drive is not None:
            self._pending_ftx1_profile_save_band = None
            if self._pending_dial_frequency is not None or self._pending_ftx1_band_change is not None or self._pending_ftx1_profile_apply is not None:
                LOGGER.error("[BAND-DRIVE] Save current band profile refused: band changed while attenuation was queried")
                return
            self.ftx1_band_drive.save_current_profile(pending_save, attenuation)

    def _query_tx_audio_attenuation_if_needed(self, instance_id: str, now: datetime) -> None:
        if (
            not instance_id
            or not self.ap1_controls_available
            or self.current_tx_audio_attenuation is not None
            or not isinstance(self.control, WsjtxUdpControl)
        ):
            return
        if (
            self._attenuation_query_instance_id == instance_id
            and self._last_attenuation_query_at is not None
            and now - self._last_attenuation_query_at < _ATTENUATION_QUERY_INTERVAL
        ):
            return
        self._attenuation_query_instance_id = instance_id
        self._last_attenuation_query_at = now
        if self.control.query_tx_audio_attenuation(instance_id):
            LOGGER.info("[WSJTX] AP1 attenuation query sent")

    def invalidate_tx_audio_attenuation_state(self, reason: str) -> None:
        if self.current_tx_audio_attenuation is not None:
            LOGGER.info("[WSJTX] current_tx_audio_attenuation invalidated: %s", reason)
        self.current_tx_audio_attenuation = None
        self.tx_audio_attenuation_last_update = None
        self.pending_tx_audio_attenuation = None
        self._tx_audio_attenuation_deadline = None
        self._attenuation_query_instance_id = ""
        self._last_attenuation_query_at = None
        self._pending_alc_profile_band = None
