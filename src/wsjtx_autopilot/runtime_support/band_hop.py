"""Forced and timed band-hopping scheduler."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.worked.bands import BandResolver

LOGGER = logging.getLogger(__name__)


class BandHopMixin:
    def _forced_band_hop_bands(self) -> tuple[str, ...]:
        configured = (
            self.config.forced_band_hop_bands
            if self.config.forced_band_hopping_enabled
            else self.config.allowed_auto_hop_bands
        )
        return tuple(
            dict.fromkeys(
                band.strip().lower()
                for band in configured
                if isinstance(band, str)
                and band.strip()
                and band.strip().lower() not in self._swr_excluded_bands
                and self._band_frequency(band.strip(), "FT8") is not None
            )
        )

    def _timed_band_hopping_enabled(self) -> bool:
        return bool(
            self.config.forced_band_hopping_enabled
            or (
                self.config.automatic_band_hopping_enabled
                and self.config.maximum_band_dwell_minutes > 0
            )
        )

    def _timed_band_hop_interval_minutes(self) -> float:
        if self.config.forced_band_hopping_enabled:
            return self.config.forced_band_hop_interval_minutes
        return self.config.maximum_band_dwell_minutes

    def _observe_forced_band_hop_status(self, now: datetime) -> None:
        if not self._timed_band_hopping_enabled() or self._pending_dial_frequency is not None:
            return
        bands = self._forced_band_hop_bands()
        if not bands:
            self._warn_forced_band_hop_configuration()
            return
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if current_band in bands and self._forced_band_hop_dwell_band != current_band:
            self._forced_band_hop_pending_band = None
            self._start_forced_band_hop_dwell(current_band, now)
        elif current_band not in bands and self._forced_band_hop_pending_band is None:
            self._forced_band_hop_pending_band = bands[0]
            LOGGER.info("[BAND-HOP] Current band is outside the configured sequence; pending %s", bands[0])

    def _run_forced_band_hop(self, now: datetime) -> None:
        if self._swr_recovery is not None or not self._timed_band_hopping_enabled():
            return
        bands = self._forced_band_hop_bands()
        if not bands:
            self._warn_forced_band_hop_configuration()
            return
        if self._forced_band_hop_pending_band is None:
            started_at = self._forced_band_hop_dwell_started_at
            current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            if started_at is None or current_band not in bands:
                return
            if now - started_at < timedelta(minutes=self._timed_band_hop_interval_minutes()):
                return
            target = self._next_forced_band_hop_band()
            if target is None:
                LOGGER.warning("[BAND-HOP] No alternate configured band is available; restarting dwell")
                self._start_forced_band_hop_dwell(current_band, now)
                return
            self._forced_band_hop_pending_band = target
            LOGGER.info("[BAND-HOP] Dwell expired on %s", current_band)
            if self.engine.state.session.state is not QsoState.IDLE or self.finalization.active:
                LOGGER.info("[BAND-HOP] QSO active, deferring hop to %s", target)
        target = self._forced_band_hop_pending_band
        if target is None or not self._forced_band_hop_safe():
            return
        frequency = self._band_frequency(target, self.status.mode or "FT8")
        if frequency is None:
            LOGGER.warning("[BAND-HOP] No frequency is configured for %s in %s; remaining on the current band", target, self.status.mode or "unknown")
            self._forced_band_hop_pending_band = None
            return
        if self._request_band_change(target, frequency, now, "forced"):
            self._forced_band_hop_pending_band = None

    def _start_forced_band_hop_dwell(self, band: str, now: datetime) -> None:
        self._forced_band_hop_dwell_band = band
        self._forced_band_hop_dwell_started_at = now
        LOGGER.info(
            "[BAND-HOP] Started dwell on %s for %g min",
            band,
            self._timed_band_hop_interval_minutes(),
        )

    def _next_forced_band_hop_band(self) -> str | None:
        bands = self._forced_band_hop_bands()
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if current_band not in bands or len(bands) < 2:
            return None
        start = bands.index(current_band)
        for offset in range(1, len(bands)):
            candidate = bands[(start + offset) % len(bands)]
            if self._band_frequency(candidate, self.status.mode or "FT8") is not None:
                return candidate
        return None

    def _forced_band_hop_safe(self) -> bool:
        return bool(
            getattr(self.control, "armed", False)
            and self.status.instance_id
            and not self.status.transmitting
            and self.engine.state.session.state is QsoState.IDLE
            and not self.finalization.active
            and self._pending_watchdog_halt is None
            and not self._band_transition_pending()
            and not self.engine.pending_direct_snapshot(self._last_runtime_at or datetime.now(timezone.utc))
        )

    def _warn_forced_band_hop_configuration(self) -> None:
        if not self._forced_band_hop_config_warning_logged:
            LOGGER.warning("[BAND-HOP] Forced hopping is enabled but no configured bands are available")
            self._forced_band_hop_config_warning_logged = True
