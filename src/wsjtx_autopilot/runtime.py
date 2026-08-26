import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.base import ControlAdapter, Endpoint
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, UnknownDxccResolver
from wsjtx_autopilot.engine.finalization import FinalizationTracker
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, Candidate, CandidateKind, CooldownKind, DecodeEvent, IntendedAction, MessageKind, OriginalDecode, StationProfile
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.engine.tx_frequency import SpectrumOccupancyTracker, TxFrequencyDecision, TxFrequencyPlanner
from wsjtx_autopilot.logging_setup import current_logging_session
from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    QsoLoggedPacket,
    StatusPacket,
    WsjtxPacket,
)
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.bands import BandResolver

LOGGER = logging.getLogger(__name__)
_DECODE_MODE_MARKERS = {"~": "FT8", "+": "FT4"}
_AUTOMATION_MODES = frozenset({"FT8", "FT4"})


@dataclass(slots=True)
class WsjtxStatus:
    dial_frequency: int | None = None
    mode: str | None = None
    dx_call: str = ""
    tx_enabled: bool = False
    transmitting: bool = False
    tx_df: int | None = None
    tx_message: str = ""
    period: int | None = None


@dataclass(slots=True)
class PendingTxDfAction:
    action: IntendedAction
    requested_df: int
    remote_df: int
    deadline: datetime


class AutopilotRuntime:
    """Connect parsed WSJT-X packets to the dry-run decision engine."""

    def __init__(
        self,
        config: AppConfig,
        engine: DecisionEngine | None = None,
        control: ControlAdapter | None = None,
        worked_service: WorkedTodayService | None = None,
    ) -> None:
        self.config = config
        self.worked_service = worked_service
        if engine is None:
            resolver = CtyDatResolver(config.cty_dat_path) if config.cty_dat_path else UnknownDxccResolver()
            local_profile = StationProfile(config.local_callsign, resolver.resolve(config.local_callsign))
            engine = DecisionEngine(
                config,
                worked_service.check if worked_service is not None else None,
                cq_eligibility=CqEligibility(local_profile, resolver, config.respond_to_cq_dx),
            )
        self.engine = engine
        self.control = control or DryRunControl()
        self.status = WsjtxStatus()
        self.last_qso_notice = ""
        self._watchdog_halt_failed = False
        self.finalization = FinalizationTracker(
            config.finalization_hold_periods,
            config.max_final_retries,
        )
        self._clear_epochs: dict[str, int] = {}
        self.occupancy = SpectrumOccupancyTracker(
            config.occupancy_history_seconds,
            config.occupied_guard_hz,
        )
        self.tx_frequency_planner = TxFrequencyPlanner()
        self.last_tx_frequency_decision: TxFrequencyDecision | None = None
        self._pending_tx_df: PendingTxDfAction | None = None
        session = current_logging_session()
        if session is not None:
            session.context.feature_flags = {
                "autocall": config.autocall_enabled,
                "direct_reply_patch": config.wsjtx_direct_reply_patched,
                "set_tx_df_patch": config.wsjtx_set_tx_df_patched,
                "smart_tx": config.smart_tx_frequency,
            }

    def handle(
        self,
        packet: WsjtxPacket | None,
        now: datetime,
        endpoint: Endpoint | None = None,
    ) -> IntendedAction | None:
        self.control.poll()
        if packet is not None:
            self.control.observe(packet, endpoint)
        handled_action: IntendedAction | None = None
        if isinstance(packet, HeartbeatPacket):
            source = f"{endpoint[0]}:{endpoint[1]}" if endpoint is not None else "unknown"
            LOGGER.info(
                "[WSJTX] heartbeat instance=%s schema=%d version=%s endpoint=%s",
                packet.header.instance_id,
                packet.header.schema,
                packet.version,
                source,
            )
            session = current_logging_session()
            if session is not None:
                session.context.wsjtx_version = packet.version
                session.context.wsjtx_schema = packet.header.schema
                session.context.wsjtx_instance = packet.header.instance_id
                session.context.wsjtx_endpoint = source
        elif isinstance(packet, StatusPacket):
            period = packet.tr_period if packet.tr_period != 0xFFFFFFFF else None
            self.status = WsjtxStatus(
                dial_frequency=packet.dial_frequency,
                mode=packet.mode,
                dx_call=packet.dx_call,
                tx_enabled=packet.tx_enabled,
                transmitting=packet.transmitting,
                tx_df=packet.tx_df,
                tx_message=packet.tx_message,
                period=period,
            )
            session = current_logging_session()
            if session is not None:
                session.context.band = BandResolver().resolve(packet.dial_frequency) or "unknown"
                session.context.mode = packet.mode or "unknown"
            self._observe_finalization_status(packet, now)
            LOGGER.info(
                "[STATUS] %d Hz %s DX=%s TX=%s transmitting=%s TX_DF=%d engine_state=%s",
                packet.dial_frequency,
                packet.mode,
                packet.dx_call or "-",
                "on" if packet.tx_enabled else "off",
                "yes" if packet.transmitting else "no",
                packet.tx_df,
                self.engine.state.session.state.name,
            )
            if self._status_confirms_reply(packet):
                if self.engine.state.session.state is QsoState.DIRECT_REPLY_SENT:
                    self.engine.state.confirm_direct_reply(now)
                else:
                    self.engine.state.confirm_reply(now)
                LOGGER.info("[ENGINE] QSO active remote=%s", self.engine.state.session.remote_callsign)
            handled_action = self._confirm_tx_df(packet, now)
            self._verify_locked_tx_df(packet)
        elif isinstance(packet, DecodePacket):
            accepted = self._handle_decode(packet, now, endpoint)
            if accepted is not None:
                self._preempt_pending_cq(accepted)
        elif isinstance(packet, ClearPacket):
            instance_id = packet.header.instance_id
            self._clear_epochs[instance_id] = self._clear_epochs.get(instance_id, 0) + 1
            self.engine.invalidate_instance_decodes(instance_id)
            LOGGER.info("[WSJTX] Clear instance=%s window=%s", instance_id, packet.window)
        elif isinstance(packet, QsoLoggedPacket):
            LOGGER.info(
                "[QSO id=%s] QSOLogged station=%s frequency=%d mode=%s instance=%s",
                self.engine.state.session.qso_id or "-",
                packet.dx_call,
                packet.tx_frequency,
                packet.mode,
                packet.header.instance_id,
            )
            if self.worked_service is not None:
                self.worked_service.record_qso_logged(packet)
            if self.engine.complete_qso(packet.dx_call, packet.header.instance_id, "QSOLogged received"):
                self._log_ready_for_next_qso()

        self.engine.state.expire_direct_reply(
            now,
            self.config.direct_reply_confirmation_timeout_seconds,
        )
        self.engine.state.expire_reply_confirmation(
            now,
            self.config.direct_reply_confirmation_timeout_seconds,
        )
        self._maintain_qso_lifecycle(now)
        self.finalization.expire(now)
        timed_out_action = self._expire_tx_df_request(now)
        if timed_out_action is not None:
            handled_action = timed_out_action
        if self._pending_tx_df is not None:
            action = None
        elif self.finalization.active and self.engine.state.session.state is QsoState.IDLE:
            LOGGER.debug("[FINALIZE] candidate selection held during RX grace")
            action = None
        else:
            action = self.engine.decide(now)
        if action is not None:
            action = self._plan_tx_frequency(action, now)
            LOGGER.info(
                "[RUNTIME] requesting control action station=%s kind=%s implementation=%s armed=%s",
                action.station,
                action.kind.name,
                type(self.control).__name__,
                getattr(self.control, "armed", False),
            )
            if self._should_set_tx_df(action):
                if not self._request_tx_df(action, now):
                    action = self._fallback_after_set_failure(action, now)
            else:
                if not isinstance(self.control, DryRunControl) and not self.config.wsjtx_set_tx_df_patched:
                    action = replace(
                        action,
                        selected_tx_df=None,
                        tx_df_reason="native Reply; Tx DF unconfirmed",
                        tx_df_gap_width=0,
                    )
                self._execute_action(action, now)
        session = self.engine.state.session
        LOGGER.debug("[ENGINE] state=%s remote=%s", session.state.name, session.remote_callsign or "-")
        return action or handled_action

    def _handle_decode(
        self,
        packet: DecodePacket,
        now: datetime,
        endpoint: Endpoint | None,
    ) -> Candidate | None:
        LOGGER.info(
            "[DECODE] %+d %+.1f %d %s",
            packet.snr,
            packet.delta_time,
            packet.delta_frequency,
            packet.message,
        )
        mode = self._automation_mode(packet.mode)
        if packet.is_new and mode is not None:
            self.engine.note_decode_arrival(
                (packet.header.instance_id, packet.decode_time, mode),
                now,
            )
        if packet.is_new and not packet.off_air and mode is not None:
            self.occupancy.add_decode(packet.delta_frequency, now)
        if not packet.is_new:
            LOGGER.info("[ENGINE] type=ignored reason=replayed decode text=%s", packet.message)
            return None
        if packet.low_confidence:
            LOGGER.info("[ENGINE] type=ignored reason=low confidence text=%s", packet.message)
            self._log_rejected_direct_packet(packet, "LOW_CONFIDENCE")
            return None
        if packet.off_air:
            LOGGER.info("[ENGINE] type=ignored reason=off-air Decode text=%s", packet.message)
            self._log_rejected_direct_packet(packet, "OFF_AIR")
            return None

        if mode is None:
            LOGGER.info(
                "[ENGINE] type=ignored reason=unsupported mode decode_mode=%r status_mode=%r text=%s",
                packet.mode,
                self.status.mode,
                packet.message,
            )
            return None
        parsed = parse_ft8_message(packet.message)
        if parsed is None:
            LOGGER.info("[ENGINE] type=ignored reason=ambiguous/free-text text=%s", packet.message)
            return None

        if parsed.kind in {MessageKind.CQ, MessageKind.QRZ}:
            detected_type = parsed.kind.name
        elif parsed.is_addressed_to(self.config.local_callsign):
            detected_type = "direct call"
        else:
            detected_type = "exchange"
        LOGGER.info(
            "[ENGINE] parsed type=%s sender=%s text=%s decode_mode=%r mode=%s",
            detected_type,
            parsed.sender,
            parsed.raw,
            packet.mode,
            mode,
        )
        identity = (
            f"{packet.header.instance_id}|{packet.decode_time.isoformat()}|"
            f"{packet.delta_frequency}|{packet.message}"
        )
        event = DecodeEvent(
            parsed=parsed,
            observed_at=now,
            mode=mode,
            snr=packet.snr,
            frequency=self.status.dial_frequency,
            period=self.status.period,
            unique_id=identity,
            original=OriginalDecode(
                instance_id=packet.header.instance_id,
                schema=packet.header.schema,
                decode_time=packet.decode_time,
                snr=packet.snr,
                delta_time=packet.delta_time,
                delta_frequency=packet.delta_frequency,
                mode=packet.mode,
                message=packet.message,
                low_confidence=packet.low_confidence,
                is_new=packet.is_new,
                source_endpoint=endpoint,
                clear_epoch=self._clear_epochs.get(packet.header.instance_id, 0),
                off_air=packet.off_air,
            ),
        )
        if parsed.kind is MessageKind.SEVENTY_THREE and self.finalization.state is not None:
            if parsed.sender == self.finalization.state.remote_callsign:
                self.finalization.close("remote 73 received")
                self.last_qso_notice = ""
        if parsed.kind in {MessageKind.RRR, MessageKind.RR73} and parsed.is_addressed_to(self.config.local_callsign):
            final_state = self.finalization.state
            session = self.engine.state.session
            terminal_progress = (
                final_state is not None
                and final_state.last_terminal_kind is MessageKind.RRR
                and parsed.kind is MessageKind.RR73
                and session.state is not QsoState.IDLE
                and session.remote_callsign == parsed.sender
            )
            if terminal_progress:
                self.finalization.advance_terminal(event, now)
            elif self.finalization.matches_retry(event):
                session = self.engine.state.session
                if self.finalization.same_terminal_period(event):
                    LOGGER.info(
                        "[FINALIZE] duplicate terminal Decode ignored remote=%s type=%s",
                        parsed.sender,
                        parsed.kind.name,
                    )
                    return None
                new_qso_active = (
                    session.state is not QsoState.IDLE
                    and (
                        session.remote_callsign not in {None, parsed.sender}
                        or (
                            final_state is not None
                            and session.reply_sent_at is not None
                            and session.reply_sent_at > final_state.started_at
                        )
                    )
                )
                if new_qso_active:
                    LOGGER.info(
                        "[FINALIZE] old terminal retry ignored remote=%s active_remote=%s state=%s",
                        parsed.sender,
                        session.remote_callsign,
                        session.state.name,
                    )
                    return None
                LOGGER.info(
                    "[FINALIZE] repeated terminal message remote=%s type=%s",
                    parsed.sender,
                    parsed.kind.name,
                )
                if not self.finalization.can_retry():
                    LOGGER.info("[FINALIZE] retry limit reached remote=%s", parsed.sender)
                    self.finalization.close("retry limit reached")
                    return
                if event.original is not None and self.control.retry_final(event.original, "repeated terminal message"):
                    count = self.finalization.record_retry(event, now)
                    self.last_qso_notice = (
                        f"{parsed.sender} repeats {parsed.kind.name}; retransmission 73 "
                        f"{count}/{self.config.max_final_retries}"
                    )
                    if count >= self.config.max_final_retries:
                        LOGGER.info("[FINALIZE] retry limit reached remote=%s", parsed.sender)
                return None
            if not terminal_progress and session.state is not QsoState.IDLE and session.remote_callsign == parsed.sender:
                if self.finalization.begin(event, now):
                    self.last_qso_notice = f"QSO complete - final confirmation {parsed.sender}"
        remote_cq = self.engine.remote_cq_during_attempt(event, now)
        if remote_cq is not None:
            session = self.engine.state.session
            if remote_cq == "tolerated":
                self.last_qso_notice = (
                    f"Remote CQ {parsed.sender}: {session.remote_cq_count}/"
                    f"{self.config.max_remote_cq_during_attempt}"
                )
                return None
            if remote_cq == "ignored":
                return None
            self.last_qso_notice = f"QSO abandoned - {parsed.sender} returned to CQ; searching candidates"
            halt_failed = False
            if self.status.tx_enabled and self.status.dx_call.upper() == parsed.sender.upper():
                halted = self.control.halt_tx(event.original.instance_id, "remote returned to CQ")
                if not halted and not isinstance(self.control, DryRunControl):
                    self.control.disarm("HaltTx failed after remote returned to CQ")
                    self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({parsed.sender})"
                    halt_failed = True
            if not halt_failed:
                self.engine.abort_qso(
                    "remote returned to CQ",
                    now,
                    self.config.remote_returned_to_cq_cooldown_seconds,
                    CooldownKind.REMOTE_RETURNED_TO_CQ,
                    preserve_candidates=True,
                )
            return None
        abandoned = self.engine.remote_engaged_other(event, now)
        if abandoned is not None:
            remote, other = abandoned
            self.last_qso_notice = f"QSO abandoned - {remote} is working {other}; searching candidates"
            halt_failed = False
            if self.status.tx_enabled and self.status.dx_call.upper() == remote.upper():
                halted = self.control.halt_tx(event.original.instance_id, "remote engaged another station")
                if not halted and not isinstance(self.control, DryRunControl):
                    self.control.disarm("HaltTx failed after remote engaged another station")
                    self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({remote})"
                    halt_failed = True
            if halt_failed:
                return None
            self.engine.abort_qso(
                f"working {other}",
                now,
                self.config.remote_busy_cooldown_seconds,
                CooldownKind.REMOTE_BUSY_OTHER_QSO,
                preserve_candidates=True,
            )
        return self.engine.observe(event, now)

    def _preempt_pending_cq(self, candidate: Candidate) -> None:
        pending = self._pending_tx_df
        if (
            pending is None
            or pending.action.kind is not ActionKind.CQ_REPLY
            or candidate.kind is not CandidateKind.DIRECT_CALLER
            or not candidate.force_priority
            or candidate.event.original is None
            or pending.action.original_decode is None
        ):
            return
        old = pending.action.original_decode
        new = candidate.event.original
        same_window = (
            old.instance_id == new.instance_id
            and old.decode_time == new.decode_time
            and candidate.event.mode == self._automation_mode(old.mode)
        )
        if not same_window:
            return
        if not self.engine.cancel_pending_action(pending.action, "higher-priority Direct Call in same window"):
            return
        self._pending_tx_df = None
        self.last_tx_frequency_decision = None
        LOGGER.info(
            "[ENGINE] candidate preempted old=%s type=CQ new=%s type=DIRECT_CALL",
            pending.action.station,
            candidate.station,
        )

    def _log_rejected_direct_packet(self, packet: DecodePacket, blocker: str) -> None:
        parsed = parse_ft8_message(packet.message)
        if parsed is None or not parsed.is_addressed_to(self.config.local_callsign):
            return
        LOGGER.info("[DIRECT] received station=%s df=%d", parsed.sender, packet.delta_frequency)
        LOGGER.info("[DIRECT] hard_blockers=[%s]", blocker)
        LOGGER.info("[DIRECT] soft_blockers=[]")
        LOGGER.info("[DIRECT] rejected station=%s blocker=%s", parsed.sender, blocker)

    def _plan_tx_frequency(self, action: IntendedAction, now: datetime) -> IntendedAction:
        decode = action.original_decode
        if decode is None:
            return action
        remote_df = decode.delta_frequency
        LOGGER.info("[TXDF] remote=%s df=%d", action.station, remote_df)
        count = self.occupancy.signal_count(now)
        LOGGER.info("[TXDF] decode occupancy=%d signals", count)
        if not self.config.smart_tx_frequency or not self.config.smart_tx_find_free:
            decision = TxFrequencyDecision(remote_df, "Smart TX disabled", fallback=True)
        elif count <= 1:
            decision = TxFrequencyDecision(remote_df, "no occupancy data", fallback=True)
        else:
            reserved = self.engine.state.session.chosen_tx_df
            decision = self.tx_frequency_planner.plan(
                remote_df,
                self.occupancy.occupied_ranges(now, reserved),
                self.status.tx_df,
                self.config.tx_df_min,
                self.config.tx_df_max,
                self.config.minimum_free_gap_hz,
            )
        self.last_tx_frequency_decision = decision
        if decision.fallback:
            LOGGER.info("[TXDF] %s", decision.reason)
            LOGGER.info("[TXDF] fallback remote=%d", remote_df)
        else:
            LOGGER.info("[TXDF] selected=%d gap=%dHz", decision.selected_df, decision.gap_width)
        selected_df = decision.selected_df
        if (
            decision.fallback
            and self.config.smart_tx_frequency
            and self.config.smart_tx_find_free
            and not self.config.smart_tx_fallback_remote
        ):
            selected_df = None
        return replace(
            action,
            remote_df=remote_df,
            selected_tx_df=selected_df,
            tx_df_reason=decision.reason,
            tx_df_gap_width=decision.gap_width,
            tx_df_fallback=decision.fallback,
        )

    def _should_set_tx_df(self, action: IntendedAction) -> bool:
        return (
            self.config.wsjtx_set_tx_df_patched
            and action.selected_tx_df is not None
            and self.config.tx_df_min <= action.selected_tx_df <= self.config.tx_df_max
            and not isinstance(self.control, DryRunControl)
        )

    def _request_tx_df(self, action: IntendedAction, now: datetime) -> bool:
        requested = action.selected_tx_df
        remote = action.remote_df
        if requested is None or remote is None or not self.control.set_tx_df(action, requested, now):
            return False
        self._pending_tx_df = PendingTxDfAction(
            action,
            requested,
            remote,
            now + timedelta(seconds=self.config.tx_df_confirmation_timeout_seconds),
        )
        return True

    def _confirm_tx_df(self, packet: StatusPacket, now: datetime) -> IntendedAction | None:
        pending = self._pending_tx_df
        if pending is None or packet.header.instance_id != pending.action.original_decode.instance_id:  # type: ignore[union-attr]
            return None
        if packet.tx_df != pending.requested_df:
            return None
        self._pending_tx_df = None
        LOGGER.info("[STATUS] Tx DF=%d confirmed", packet.tx_df)
        self._execute_action(pending.action, now)
        return pending.action

    def _expire_tx_df_request(self, now: datetime) -> IntendedAction | None:
        pending = self._pending_tx_df
        if pending is None or now <= pending.deadline:
            return None
        self._pending_tx_df = None
        LOGGER.warning("[TXDF] confirmation timeout requested=%d", pending.requested_df)
        return self._fallback_after_set_failure(pending.action, now)

    def _fallback_after_set_failure(self, action: IntendedAction, now: datetime) -> IntendedAction:
        remote = action.remote_df
        if (
            self.config.smart_tx_fallback_remote
            and remote is not None
            and action.selected_tx_df != remote
            and self.config.tx_df_min <= remote <= self.config.tx_df_max
        ):
            fallback = replace(
                action,
                selected_tx_df=remote,
                tx_df_reason="fallback remote DF",
                tx_df_gap_width=0,
                tx_df_fallback=True,
            )
            LOGGER.info("[TXDF] fallback remote DF=%d Hz", remote)
            if self._request_tx_df(fallback, now):
                return fallback
        native = replace(
            action,
            selected_tx_df=None,
            tx_df_reason="native Reply fallback; Tx DF unconfirmed",
            tx_df_gap_width=0,
            tx_df_fallback=True,
        )
        LOGGER.warning("[TXDF] SetTxDF unavailable; using native Reply behavior")
        self._execute_action(native, now)
        return native

    def _execute_action(self, action: IntendedAction, now: datetime) -> None:
        outcome = self.control.execute(action, now)
        LOGGER.info("[RUNTIME] control outcome=%s station=%s", outcome.name, action.station)
        self.engine.record_action_outcome(action, outcome, now)
        if outcome is ActionOutcome.SENT:
            self.last_qso_notice = ""
            self._watchdog_halt_failed = False

    def _verify_locked_tx_df(self, packet: StatusPacket) -> None:
        session = self.engine.state.session
        if session.state is QsoState.IDLE or session.chosen_tx_df is None:
            return
        if packet.tx_df == session.chosen_tx_df:
            return
        expected = session.chosen_tx_df
        session.chosen_tx_df = packet.tx_df
        session.tx_df_reason = "lock lost; actual WSJT-X Tx DF"
        session.tx_df_gap_width = 0
        LOGGER.error("[TXDF] lock lost expected=%d actual=%d", expected, packet.tx_df)
        self.control.disarm("WSJT-X changed locked Tx DF")

    def _observe_finalization_status(self, packet: StatusPacket, now: datetime) -> None:
        state = self.finalization.state
        if state is None:
            return
        if packet.mode != state.mode or (
            state.frequency is not None and packet.dial_frequency != state.frequency
        ):
            self.finalization.close("mode or frequency changed")
            self.last_qso_notice = ""
            return
        tokens = packet.tx_message.upper().split()
        terminal_tx = (
            bool(tokens)
            and tokens[-1] == "73"
            and state.remote_callsign.upper() in tokens
            and self.config.local_callsign.upper() in tokens
        )
        if terminal_tx and (packet.transmitting or packet.tx_enabled):
            self.finalization.confirm_final_tx(now)
            if self.engine.state.matches_remote(state.remote_callsign):
                self.engine.state.mark_complete("final 73 transmitted")

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
                    self._log_ready_for_next_qso()
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
            halted = self.control.halt_tx(session.instance_id, "QSO stalled")
            if not halted and not isinstance(self.control, DryRunControl):
                self.control.disarm("HaltTx failed for stalled QSO")
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
                self._log_ready_for_next_qso()
            return
        inactivity = (now - session.last_activity).total_seconds()
        if inactive and inactivity > self.config.qso_timeout_seconds:
            if self.engine.abort_qso("QSO inactivity timeout while WSJT-X is not engaged"):
                self._log_ready_for_next_qso()

    def _log_ready_for_next_qso(self) -> None:
        maximum = self.control.max_actions
        if maximum is not None and self.control.actions_used < maximum:
            LOGGER.info(
                "[ENGINE] ready for next QSO actions=%d/%d",
                self.control.actions_used,
                maximum,
            )
