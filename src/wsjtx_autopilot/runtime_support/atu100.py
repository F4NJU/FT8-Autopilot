"""Bounded AP1 Tune carrier used for the optional external ATU-100."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from wsjtx_autopilot.wsjtx.models import TuneStatePacket

from .models import PendingAtu100Tune, PendingDialFrequency

LOGGER = logging.getLogger(__name__)
_ATU100_TUNE_ACK_TIMEOUT = timedelta(seconds=3)
_ATU100_TUNE_DURATION = timedelta(seconds=5)


class Atu100Mixin:
    def _begin_atu100_tune_after_band_change(self, pending: PendingDialFrequency, now: datetime) -> bool:
        """Request a short, acknowledged AP1 Tune carrier for an external ATU-100.

        This deliberately controls only the WSJT-X Tune generator: the tuner is
        external and receives no CAT command from AutoPilot.
        """
        if not self.config.atu100_tune_on_band_change or not self.ap1_controls_available:
            return False
        if self.status.transmitting or not pending.instance_id:
            LOGGER.warning("[ATU-100] Tune skipped band=%s transmitting=%s", pending.band, self.status.transmitting)
            return False
        if not self.control.set_tune(pending.instance_id, True):
            self.last_qso_notice = f"ATU-100 Tune refused on {pending.band}"
            LOGGER.warning("[ATU-100] Tune request rejected band=%s", pending.band)
            return False
        self._pending_atu100_tune = PendingAtu100Tune(
            pending.instance_id,
            pending.band,
            now + _ATU100_TUNE_ACK_TIMEOUT,
        )
        # Keep QSO selection paused until the short carrier has finished.
        self._band_resume_at = now + _ATU100_TUNE_ACK_TIMEOUT + _ATU100_TUNE_DURATION
        self._band_resume_band = pending.band
        self.last_qso_notice = f"ATU-100 Tune requested on {pending.band}"
        LOGGER.info("[ATU-100] Tune requested after confirmed band change band=%s", pending.band)
        return True

    def _observe_atu100_tune_state(self, packet: TuneStatePacket, now: datetime) -> None:
        pending = self._pending_atu100_tune
        if pending is None or packet.header.instance_id != pending.instance_id:
            return
        if packet.active:
            pending.active_confirmed = True
            pending.stop_at = now + _ATU100_TUNE_DURATION
            self.last_qso_notice = f"ATU-100 Tune active on {pending.band}"
            LOGGER.info("[ATU-100] Tune active confirmed band=%s", pending.band)
            return
        if pending.active_confirmed:
            self._finish_atu100_tune(now, "stopped by AP1")

    def _run_pending_atu100_tune(self, now: datetime) -> None:
        pending = self._pending_atu100_tune
        if pending is None:
            return
        if not pending.active_confirmed:
            if now > pending.ack_deadline:
                self._pending_atu100_tune = None
                self.last_qso_notice = f"ATU-100 Tune unconfirmed on {pending.band}; protection SWR uncertain"
                self._band_resume_at = now + timedelta(seconds=self.status.period or 15)
                self._band_resume_band = pending.band
                LOGGER.warning("[ATU-100] Tune confirmation timeout band=%s", pending.band)
            return
        if pending.stop_at is not None and now >= pending.stop_at:
            if not self.control.set_tune(pending.instance_id, False):
                self.last_qso_notice = f"ATU-100 Tune stop refused on {pending.band}; AutoPilot paused"
                LOGGER.error("[ATU-100] Tune stop request rejected band=%s", pending.band)
                return
            self._finish_atu100_tune(now, "carrier completed")

    def _finish_atu100_tune(self, now: datetime, detail: str) -> None:
        pending = self._pending_atu100_tune
        if pending is None:
            return
        self._pending_atu100_tune = None
        self._band_resume_at = now + timedelta(seconds=self.status.period or 15)
        self._band_resume_band = pending.band
        self.last_qso_notice = f"ATU-100 Tune complete on {pending.band} ({detail})"
        LOGGER.info("[ATU-100] Tune complete band=%s detail=%s", pending.band, detail)
