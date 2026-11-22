"""QSO lifecycle and runaway-TX watchdog handling."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.engine.adaptive import AttemptOutcome
from wsjtx_autopilot.engine.state import QsoState

from .models import PendingWatchdogHalt

LOGGER = logging.getLogger(__name__)
_WATCHDOG_HALT_CONFIRM_TIMEOUT = timedelta(seconds=2)


class WatchdogMixin:
    def _maintain_qso_lifecycle(self, now: datetime) -> None:
        session = self.engine.state.session
        if session.state is QsoState.IDLE or session.last_activity is None:
            return

        remote = (session.remote_callsign or "").upper()
        tx_message_matches = remote and remote in self.status.tx_message.upper().split()
        status_engaged = self.status.dx_call.upper() == remote and tx_message_matches
        inactive = not self.status.transmitting and (not self.status.tx_enabled or not status_engaged)

        if session.state is QsoState.COMPLETE:
            completion_age = (now - session.last_activity).total_seconds()
            if inactive and completion_age >= self.config.qso_completion_grace_seconds:
                if self.engine.finalize_observed_completion("terminal Decode and inactive WSJT-X Status"):
                    self.note_qso_completed(session.remote_callsign or "station inconnue")
                    self._log_ready_for_next_qso()
            return

        if session.state is QsoState.WAITING_FINAL_TX:
            return

        if session.state is QsoState.DIRECT_REPLY_SENT:
            return
        if session.no_progress_periods >= self.config.max_no_progress_periods:
            if self._watchdog_halt_failed:
                return
            stage = session.progress_stage.name if session.progress_stage is not None else "UNKNOWN"
            LOGGER.error(
                "[WATCHDOG] QSO stalled remote=%s stage=%s no_progress=%d/%d",
                remote,
                stage,
                session.no_progress_periods,
                self.config.max_no_progress_periods,
            )
            self.session_diagnostics.autopilot_watchdogs += 1
            self.session_diagnostics.note(f"Watchdog AutoPilot : {remote or 'station inconnue'}")
            halted = self._halt_tx(session.instance_id, "QSO stalled")
            if not halted and not isinstance(self.control, DryRunControl):
                self._disarm_control("halt_tx_failed_stalled_qso", "AutopilotRuntime._maintain_qso_lifecycle")
                self._watchdog_halt_failed = True
                self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({remote})"
                LOGGER.critical(
                    "[WATCHDOG] HaltTx failed; retaining active QSO and blocking new initiations remote=%s",
                    remote,
                )
                return
            self.last_qso_notice = f"QSO abandoned - no progress ({remote}, {stage})"
            if self.engine.abort_qso(
                "QSO stalled",
                now,
                self.config.stalled_qso_cooldown_seconds,
            ):
                self.note_adaptive_failure(remote, AttemptOutcome.STALLED, now)
                self._log_ready_for_next_qso()
            return
        inactivity = (now - session.last_activity).total_seconds()
        if inactive and inactivity > self.config.qso_timeout_seconds:
            outcome = (
                AttemptOutcome.NO_RESPONSE
                if session.state is QsoState.CALLING_STATION
                else AttemptOutcome.STALLED
            )
            if self.engine.abort_qso(
                "QSO inactivity timeout while WSJT-X is not engaged",
                now,
                self.config.stalled_qso_cooldown_seconds,
            ):
                self.note_adaptive_failure(remote, outcome, now)
                self._log_ready_for_next_qso()

    def _handle_wsjtx_tx_watchdog(self, packet: StatusPacket, now: datetime) -> None:
        """Release an active QSO as soon as WSJT-X reports its TX watchdog."""
        if not packet.tx_watchdog:
            return
        if self._pending_watchdog_halt is not None:
            return
        session = self.engine.state.session
        if session.state is QsoState.IDLE:
            return
        remote = (session.remote_callsign or "").upper()
        stage = session.progress_stage.name if session.progress_stage is not None else "UNKNOWN"
        LOGGER.warning(
            "[WATCHDOG] WSJT-X Runaway TX watchdog remote=%s stage=%s; abandoning immediately",
            remote or "unknown",
            stage,
        )
        self.session_diagnostics.wsjtx_watchdogs += 1
        self.session_diagnostics.note(f"Watchdog WSJT-X : {remote or 'station inconnue'}")
        if packet.transmitting or packet.tx_enabled:
            halt_result = self._request_halt_tx(packet.header.instance_id, "WSJT-X Runaway TX watchdog")
            halted = halt_result.radio_ok and halt_result.wsjtx_ok
            if not halted and halt_result.wsjtx_ok and not packet.transmitting:
                halted = True
                LOGGER.warning(
                    "[WATCHDOG] Hamlib PTT OFF confirmation unavailable, but WSJT-X is already in RX"
                )
            elif not halted and halt_result.wsjtx_ok and not isinstance(self.control, DryRunControl):
                self._pending_watchdog_halt = PendingWatchdogHalt(
                    packet.header.instance_id,
                    remote,
                    now + _WATCHDOG_HALT_CONFIRM_TIMEOUT,
                )
                LOGGER.warning(
                    "[WATCHDOG] HaltTx sent; waiting %.1f s for WSJT-X RX confirmation remote=%s",
                    _WATCHDOG_HALT_CONFIRM_TIMEOUT.total_seconds(),
                    remote or "unknown",
                )
            elif not halted and not isinstance(self.control, DryRunControl):
                self._disarm_control("halt_tx_failed_wsjtx_watchdog", "AutopilotRuntime._handle_wsjtx_tx_watchdog")
                self._watchdog_halt_failed = True
                self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({remote})"
                LOGGER.critical(
                    "[WATCHDOG] HaltTx failed after WSJT-X watchdog; retaining active QSO remote=%s",
                    remote or "unknown",
                )
                return
        state = session.state
        if self.engine.abort_qso(
            "WSJT-X Runaway TX watchdog",
            now,
            self.config.stalled_qso_cooldown_seconds,
            preserve_candidates=True,
        ):
            outcome = AttemptOutcome.NO_RESPONSE if state is QsoState.CALLING_STATION else AttemptOutcome.STALLED
            self.note_adaptive_failure(remote, outcome, now)
            self.last_qso_notice = f"QSO abandoned - WSJT-X TX watchdog ({remote})"
            self._log_ready_for_next_qso()

    def _poll_watchdog_halt_confirmation(self, now: datetime) -> None:
        pending = self._pending_watchdog_halt
        if pending is None:
            return
        if not self.status.transmitting:
            self._pending_watchdog_halt = None
            self._watchdog_halt_failed = False
            LOGGER.warning(
                "[%s] WSJT-X RX confirmed after HaltTx; AutoPilot remains armed remote=%s",
                pending.context,
                pending.remote or "unknown",
            )
            return
        if now < pending.deadline:
            return
        self._pending_watchdog_halt = None
        self._disarm_control(pending.disarm_reason, pending.source)
        self._watchdog_halt_failed = True
        self.last_qso_notice = f"SAFETY FAULT - TX still active after HaltTx ({pending.remote})"
        LOGGER.critical(
            "[%s] WSJT-X still transmitting after HaltTx confirmation timeout; AutoPilot disarmed remote=%s",
            pending.context,
            pending.remote or "unknown",
        )

    def _log_ready_for_next_qso(self) -> None:
        maximum = self.control.max_actions
        if maximum is not None and self.control.actions_used < maximum:
            LOGGER.info(
                "[ENGINE] ready for next QSO actions=%d/%d",
                self.control.actions_used,
                maximum,
            )
