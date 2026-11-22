"""Rare-reply gating, preemption checks and action observation helpers."""

from __future__ import annotations

import logging
from datetime import datetime

from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.models import (
    ActionKind,
    ActionOutcome,
    DecodeEvent,
    IntendedAction,
    MessageKind,
)
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.progress import QsoProgressStage
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.wsjtx.models import DecodePacket

LOGGER = logging.getLogger(__name__)


class DecisionMixin:
    def _try_rare_reply_from_decode(self, event: DecodeEvent, now: datetime) -> bool:
        """Use WSJT-X's Reply command as a rare-station double click.

        This is intentionally restricted to the explicit Reply rare-tracking
        mode, a fresh local Decode, idle QSO state, and AP1/direct-Reply
        capable WSJT-X.  The original Decode is forwarded unchanged.
        """
        parsed = event.parsed
        if not (
            self._remote_hunt_enabled
            and self._rare_target_accepts(parsed.sender)
            and parsed.kind in {MessageKind.DIRECTED, MessageKind.REPORT, MessageKind.R_REPORT}
            and not parsed.is_addressed_to(self.config.local_callsign)
            and self.engine.state.session.state is QsoState.IDLE
            and not self.finalization.active
            and event.original is not None
        ):
            return False
        action = IntendedAction(ActionKind.RARE_REPLY, parsed.sender, "rare station local Decode", event.original, now)
        outcome = self.control.execute(action, now)
        if outcome is ActionOutcome.SENT:
            self.engine.clear_candidates()
            self.engine.state.start_station(event, now)
            self.last_qso_notice = f"Rare station Reply sent: {parsed.sender}"
            LOGGER.warning("[REPLY] rare double-click sent station=%s message=%s", parsed.sender, parsed.raw)
        elif outcome is ActionOutcome.REJECTED_LOCAL:
            LOGGER.info("[REPLY] rare double-click not available station=%s", parsed.sender)
        return outcome is not ActionOutcome.REJECTED_LOCAL

    def _can_preempt_unanswered_cq_attempt(self, event: DecodeEvent) -> bool:
        blocker = self._direct_preemption_blocker(event)
        if blocker is not None:
            LOGGER.info(
                "[DIRECT] preemption unavailable station=%s blocker=%s",
                event.parsed.sender,
                blocker,
            )
            return False
        return True

    def _direct_preemption_blocker(self, event: DecodeEvent) -> str | None:
        session = self.engine.state.session
        checks = (
            (
                not (self.ap1_controls_available or self.config.wsjtx_direct_reply_patched),
                "AP1_DIRECT_REPLY_UNAVAILABLE",
            ),
            (not isinstance(self.control, WsjtxUdpControl), "CONTROL_NOT_UDP"),
            (not getattr(self.control, "armed", False), "CONTROL_DISARMED"),
            (not self.status.tx_enabled, "TX_DISABLED"),
            (self.status.transmitting, "CURRENTLY_TRANSMITTING"),
            (self.finalization.active, "FINALIZATION_ACTIVE"),
            (self._pending_dial_frequency is not None, "DIAL_CHANGE_PENDING"),
            (self._pending_ftx1_band_change is not None, "FTX1_BAND_CHANGE_PENDING"),
            (self._pending_ftx1_profile_apply is not None, "FTX1_PROFILE_PENDING"),
            (session.state is not QsoState.CALLING_STATION, f"QSO_STATE_{session.state.name}"),
            (session.progress_stage is not QsoProgressStage.CALL_OR_GRID, "QSO_ALREADY_PROGRESSED"),
            (event.original is None, "MISSING_ORIGINAL_DECODE"),
            (event.parsed.sender == session.remote_callsign, "CURRENT_REMOTE"),
        )
        for blocked, reason in checks:
            if blocked:
                return reason
        assert event.original is not None
        if session.instance_id != event.original.instance_id:
            return "DECODE_INSTANCE_MISMATCH"
        if self.status.instance_id != session.instance_id:
            return "STATUS_INSTANCE_MISMATCH"
        if session.frequency != self.status.dial_frequency:
            return "FREQUENCY_MISMATCH"
        if session.mode != event.mode:
            return "MODE_MISMATCH"
        if self.status.dx_call.upper() != session.remote_callsign:
            return "WSJTX_TARGET_MISMATCH"
        previous = parse_ft8_message(session.last_message or "")
        if previous is None or previous.kind not in {MessageKind.CQ, MessageKind.QRZ}:
            return "CURRENT_QSO_HAS_REMOTE_EXCHANGE"
        maximum = self.control.max_actions
        if maximum is not None and self.control.actions_used >= maximum:
            return "ACTION_LIMIT_REACHED"
        return None

    def _log_rejected_direct_packet(self, packet: DecodePacket, blocker: str) -> None:
        parsed = parse_ft8_message(packet.message)
        if parsed is None or not parsed.is_addressed_to(self.config.local_callsign):
            return
        LOGGER.info("[DIRECT] received station=%s df=%d", parsed.sender, packet.delta_frequency)
        LOGGER.info("[DIRECT] hard_blockers=[%s]", blocker)
        LOGGER.info("[DIRECT] soft_blockers=[]")
        LOGGER.info("[DIRECT] rejected station=%s blocker=%s", parsed.sender, blocker)

    def _execute_action(self, action: IntendedAction, now: datetime) -> None:
        if not self.engine.preflight_action(action):
            self.last_qso_notice = f"Reply skipped - {action.station} is already working another station"
            return
        outcome = self.control.execute(action, now)
        LOGGER.info("[RUNTIME] control outcome=%s station=%s", outcome.name, action.station)
        self.engine.record_action_outcome(action, outcome, now)
        if outcome is ActionOutcome.SENT:
            self.session_diagnostics.replies_sent += 1
            self.session_diagnostics.note(f"Reply envoyé : {action.station}")
            self.last_qso_notice = ""
            self._watchdog_halt_failed = False
        elif outcome is ActionOutcome.REJECTED_LOCAL:
            self.session_diagnostics.local_rejections += 1
            self.session_diagnostics.note(f"Reply refusé par WSJT-X : {action.station}")
        elif outcome is ActionOutcome.FAILED:
            self.session_diagnostics.control_failures += 1
            self.session_diagnostics.note(f"Échec d'envoi Reply : {action.station}")
