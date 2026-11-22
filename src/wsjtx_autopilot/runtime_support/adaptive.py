"""Adaptive strategy feedback, snapshots and session diagnostics."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.engine.adaptive import AdaptiveState, AttemptOutcome, AttemptRecord
from wsjtx_autopilot.engine.models import EngineEvent, EngineEventKind
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.logging_setup import current_logging_session
from wsjtx_autopilot.worked.bands import BandResolver
from wsjtx_autopilot.wsjtx.models import StatusPacket

from .constants import _AUTOMATION_MODES, _DECODE_MODE_MARKERS
from .models import PendingDialFrequency

LOGGER = logging.getLogger(__name__)


class AdaptiveMixin:
    def note_adaptive_failure(
        self,
        callsign: str | None,
        outcome: AttemptOutcome,
        now: datetime,
    ) -> None:
        if not self.config.adaptive_operation_enabled or not callsign:
            return
        band = BandResolver().resolve(self.status.dial_frequency) or "unknown" if self.status.dial_frequency else "unknown"
        self.stagnation.record(
            AttemptRecord(callsign.upper(), band, self.status.mode or "unknown", now, self.current_tx_first, outcome)
        )
        LOGGER.info(
            "[ADAPT] attempts=%d failed=%d unique=%d",
            len(self.stagnation.snapshot()), self.stagnation.failed_attempts, self.stagnation.unique_calls,
        )
        self.opportunities.mark_unavailable(
            callsign, now, max(self.config.stalled_qso_cooldown_seconds, self.opportunities.observation.total_seconds())
        )

    def _adaptive_success(self) -> None:
        self.stagnation.reset()
        self.adaptive_state = AdaptiveState.NORMAL
        self._adaptive_pending_origin = None
        LOGGER.info("[ADAPT] QSO success")
        LOGGER.info("[ADAPT] stagnation reset")

    def reset_adaptive_strategy(self) -> None:
        self.stagnation.reset()
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        self.opportunities.reset(band, self.status.mode, self._last_runtime_at or datetime.now(timezone.utc))
        if self._pending_dial_frequency is None:
            self.adaptive_state = AdaptiveState.NORMAL
            self._adaptive_pending_origin = None
        LOGGER.info("[ADAPT] strategy reset")

    def adaptive_snapshot(self) -> dict[str, object]:
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        observed_at = self._last_runtime_at or datetime.now(timezone.utc)
        opportunities = self.opportunities.snapshot(observed_at)
        quiet_threshold = max(1.0, float(self.config.adaptive_opportunity_quiet_seconds))
        quiet_anchor = self.opportunities.last_viable_at or self.opportunities.started_at
        quiet_elapsed = max(0.0, (observed_at - quiet_anchor).total_seconds()) if quiet_anchor is not None else 0.0
        opportunities.update(
            {
                "viable_count": len(opportunities["available"]),
                "quiet_elapsed_seconds": min(quiet_threshold, quiet_elapsed),
                "quiet_threshold_seconds": quiet_threshold,
                "quiet_remaining_seconds": max(0.0, quiet_threshold - quiet_elapsed),
                "exhausted": self.opportunities.exhausted(observed_at),
            }
        )
        snapshot = {
            "ap1_available": self.ap1_controls_available,
            "state": self.adaptive_state.name,
            "current_tx_first": self.current_tx_first,
            "requested_tx_period": self.requested_tx_period,
            "current_band": current_band,
            "next_band": self._next_auto_hop_band(),
            "next_frequency_hz": (
                self._next_auto_hop_destination()[1]
                if self._next_auto_hop_destination() is not None
                else None
            ),
            "band_changing_to": self._pending_dial_frequency.band if self._pending_dial_frequency else None,
            "opportunities": opportunities,
            "antenna_safety": {
                "level": self.antenna_safety_level,
                "events": tuple(self.antenna_safety_events),
                "excluded_bands": tuple(sorted(self._swr_excluded_bands)),
                "recovering": self._swr_recovery is not None,
                "phase": self._swr_recovery.phase if self._swr_recovery is not None else None,
            },
        }
        if self.ftx1_band_drive is not None:
            ftx1 = self.ftx1_band_drive.snapshot(current_band, self.current_tx_audio_attenuation)
            ftx1.update(
                {
                    "hamlib_enabled": self.config.ftx1_hamlib_enabled,
                    "radio_model": getattr(
                        self.ftx1_band_drive.cat,
                        "detected_radio_model",
                        "Yaesu FTX-1" if not self.config.ftx1_hamlib_enabled else "Hamlib generic",
                    ),
                    "alc": self.last_hamlib_alc,
                    "swr": self.last_hamlib_swr,
                    "swr_protection": self.last_hamlib_swr_protection,
                    "power_watts": self.last_hamlib_power_watts,
                    "requested_power_watts": self.last_hamlib_requested_power_watts,
                    "swr_fault": self._hamlib_swr_fault_latched,
                }
            )
            snapshot["ftx1"] = ftx1
        return snapshot




    def _request_band_change(self, target: str, frequency: int, now: datetime, source: str) -> bool:
        cat = self.ftx1_band_drive.cat if self.ftx1_band_drive is not None else None
        set_frequency = getattr(cat, "set_frequency", None)
        read_frequency = getattr(cat, "read_frequency", None)
        if callable(set_frequency) and callable(read_frequency):
            if not set_frequency(frequency):
                LOGGER.error("[HAMLIB] frequency change rejected target=%d", frequency)
                return False
            observed = read_frequency()
            if observed is None or abs(observed - frequency) > 5:
                LOGGER.error("[HAMLIB] frequency confirmation failed requested=%d observed=%s", frequency, observed)
                return False
            LOGGER.info("[HAMLIB] frequency confirmed=%d", observed)
        elif not self.control.set_dial_frequency(self.status.instance_id, frequency):
            # Compatibility path for dry-run/tests and installations where
            # Hamlib radio control is explicitly unavailable.
            return False
        self.engine.clear_candidates()
        self.engine.invalidate_pending_context(self.status.instance_id, target, self.status.mode or "unknown")
        self._pending_dial_frequency = PendingDialFrequency(
            self.status.instance_id,
            frequency,
            target,
            now + timedelta(seconds=self.config.dial_change_confirmation_timeout_seconds),
            BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None,
            source,
        )
        self.adaptive_state = AdaptiveState.BAND_CHANGING
        if source == "forced":
            LOGGER.info("[BAND-HOP] Requesting %s / %.3f MHz", target, frequency / 1_000_000)
        else:
            LOGGER.info("[BAND] current=%s", BandResolver().resolve(self.status.dial_frequency) or "unknown")
            LOGGER.info("[BAND] next=%s", target)
            LOGGER.info("[BAND] waiting confirmation")
        return True

    def _run_adaptive(self, now: datetime) -> None:
        if self._swr_recovery is not None:
            return
        if self.adaptive_state in {AdaptiveState.NORMAL, AdaptiveState.PARITY_TRIAL, AdaptiveState.BAND_TRIAL} and self._adaptive_safe(now):
            exhausted = self.opportunities.exhausted(now)
            quiet = self.opportunities.quiet(now, self.config.adaptive_opportunity_quiet_seconds)
            if exhausted or quiet:
                previous = self.adaptive_state
                self._adaptive_pending_origin = previous
                self.adaptive_state = (
                    AdaptiveState.PARITY_CHANGE_PENDING
                    if previous is AdaptiveState.NORMAL and self.config.adaptive_parity_enabled and self.ap1_controls_available
                    else AdaptiveState.BAND_HOP_PENDING
                )
                LOGGER.info(
                    "[ADAPT] opportunities=%s reason=%s state=%s -> %s",
                    self.opportunities.snapshot(now),
                    "exhausted" if exhausted else "no new viable CQ",
                    previous.name,
                    self.adaptive_state.name,
                )
        if self.adaptive_state is AdaptiveState.PARITY_CHANGE_PENDING:
            if not self.config.adaptive_parity_enabled or not self._adaptive_safe(now):
                return
            if self.current_tx_first is None:
                self.adaptive_state = AdaptiveState.BAND_HOP_PENDING
                LOGGER.warning("[ADAPT] TX period unknown; skipping parity trial and requesting band hop")
            else:
                requested = not self.current_tx_first
                if self.control.set_tx_period(self.status.instance_id, requested):
                    self.requested_tx_period = requested
                    self.adaptive_state = AdaptiveState.PARITY_TRIAL
                    self._adaptive_pending_origin = None
                    self.stagnation.reset()
                    band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
                    self.opportunities.reset(band, self.status.mode or "unknown", now)
                    LOGGER.info("[ADAPT] action=TX_PERIOD_FLIP")
                    LOGGER.info("[ADAPT] %s -> %s", "FIRST" if self.current_tx_first else "SECOND", "FIRST" if requested else "SECOND")
                return
        if self.adaptive_state is AdaptiveState.BAND_HOP_PENDING:
            if not self.config.automatic_band_hopping_enabled or not self._adaptive_safe(now):
                return
            if self.last_band_change_at is not None:
                dwell = now - self.last_band_change_at
                if dwell < timedelta(minutes=self.config.minimum_band_dwell_minutes):
                    return
            destination = self._next_auto_hop_destination()
            if destination is None:
                return
            target, frequency = destination
            self._request_band_change(target, frequency, now, "adaptive")

    def _adaptive_safe(self, now: datetime) -> bool:
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        has_pending_direct = any(
            entry["band"] == current_band for entry in self.engine.pending_direct_snapshot(now)
        )
        return bool(
            self.config.adaptive_operation_enabled
            and getattr(self.control, "armed", False)
            and self.status.instance_id
            and not self.status.transmitting
            and self.engine.state.session.state is QsoState.IDLE
            and not self.finalization.active
            and self._pending_watchdog_halt is None
            and self._pending_dial_frequency is None
            and self.pending_tx_audio_attenuation is None
            and self._blocked_band_drive_change is None
            and not has_pending_direct
        )


    def clear_finalization(self, reason: str) -> None:
        self.finalization.close(reason)
        self.last_qso_notice = ""

    def _automation_mode(self, decode_mode: str) -> str | None:
        if decode_mode in _AUTOMATION_MODES:
            return decode_mode
        marker_mode = _DECODE_MODE_MARKERS.get(decode_mode)
        if marker_mode is not None:
            return marker_mode
        if self.status.mode in _AUTOMATION_MODES:
            return self.status.mode
        return None

    def _status_confirms_reply(self, packet: StatusPacket) -> bool:
        session = self.engine.state.session
        if session.state not in {QsoState.DIRECT_REPLY_SENT, QsoState.CALLING_STATION}:
            return False
        if session.remote_callsign is None or session.reply_confirmed:
            return False
        remote = session.remote_callsign.upper()
        dx_matches = packet.dx_call.upper() == remote
        tx_message_matches = remote in packet.tx_message.upper().split()
        return dx_matches and (packet.transmitting or (packet.tx_enabled and tx_message_matches))


    def note_engine_event(self, event: EngineEvent) -> None:
        """Record a decision outcome without changing the decision itself."""
        if event.kind is EngineEventKind.CANDIDATE_SELECTED:
            self.session_diagnostics.candidates_selected += 1
            self.session_diagnostics.note(f"Sélection : {event.station}")
            return
        if event.kind is not EngineEventKind.CANDIDATE_REFUSED:
            return
        reason = event.reason.strip()
        if reason.lower() in {"exchange not actionable", "duplicate decode", "stale"}:
            return
        self.session_diagnostics.candidates_refused += 1
        if "working " in reason.lower():
            self.session_diagnostics.stations_busy += 1
            self.session_diagnostics.note(f"Écartée, déjà occupée : {event.station} ({reason})")

    def note_qso_completed(self, station: str) -> None:
        self.session_diagnostics.qso_completed += 1
        self.session_diagnostics.note(f"QSO terminé : {station}")

    def session_diagnostic_snapshot(self) -> dict[str, object]:
        return self.session_diagnostics.snapshot()

    def _update_diagnostic_context(self, now: datetime) -> None:
        session = current_logging_session()
        if session is not None:
            session.context.pending_direct_calls = self.engine.pending_direct_snapshot(now)
            session.context.adaptive = {
                "enabled": self.config.adaptive_operation_enabled,
                "state": self.adaptive_state.name,
                "recent_attempts": self.stagnation.snapshot(),
                "failed_attempts": self.stagnation.failed_attempts,
                "unique_calls": self.stagnation.unique_calls,
                "current_tx_period": self.current_tx_first,
                "requested_tx_period": self.requested_tx_period,
                "current_band": BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None,
                "allowed_bands": list(self.config.allowed_auto_hop_bands),
                "last_band_change": self.last_band_change_at.isoformat() if self.last_band_change_at else None,
                "minimum_band_dwell_minutes": self.config.minimum_band_dwell_minutes,
                "next_band": self._next_auto_hop_band(),
                "next_frequency_hz": (
                    self._next_auto_hop_destination()[1]
                    if self._next_auto_hop_destination() is not None
                    else None
                ),
                "opportunities": self.opportunities.snapshot(now),
            }
