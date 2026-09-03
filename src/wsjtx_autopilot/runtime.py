import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.base import ControlAdapter, Endpoint
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.adaptive import AdaptiveState, AttemptOutcome, AttemptRecord, DEFAULT_BAND_PROFILES, StagnationTracker
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, UnknownDxccResolver
from wsjtx_autopilot.engine.finalization import FinalizationTracker
from wsjtx_autopilot.engine.models import ActionOutcome, Candidate, CooldownKind, DecodeEvent, IntendedAction, MessageKind, OriginalDecode, StationProfile
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.ftx1 import FTX1BandDriveController, FTX1CatController
from wsjtx_autopilot.logging_setup import current_logging_session
from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    QsoLoggedPacket,
    StatusPacket,
    TxAudioAttenuationStatePacket,
    WsjtxPacket,
)
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.bands import BandResolver

LOGGER = logging.getLogger(__name__)
_DECODE_MODE_MARKERS = {"~": "FT8", "+": "FT4"}
_AUTOMATION_MODES = frozenset({"FT8", "FT4"})
_ATTENUATION_QUERY_INTERVAL = timedelta(seconds=2)


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
    instance_id: str = ""


@dataclass(slots=True)
class PendingDialFrequency:
    instance_id: str
    frequency_hz: int
    band: str
    deadline: datetime
    previous_band: str | None


class AutopilotRuntime:
    """Connect parsed WSJT-X packets to the dry-run decision engine."""

    def __init__(
        self,
        config: AppConfig,
        engine: DecisionEngine | None = None,
        control: ControlAdapter | None = None,
        worked_service: WorkedTodayService | None = None,
        ftx1_band_drive: FTX1BandDriveController | None = None,
        ftx1_band_profiles_changed: Callable[[dict[str, dict[str, int]]], None] | None = None,
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
        self._last_runtime_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        self.current_tx_audio_attenuation: int | None = None
        self.pending_tx_audio_attenuation: int | None = None
        self._pending_ftx1_profile_save_band: str | None = None
        self.tx_audio_attenuation_last_update: datetime | None = None
        self._tx_audio_attenuation_deadline: datetime | None = None
        self._attenuation_query_instance_id = ""
        self._last_attenuation_query_at: datetime | None = None
        self.last_qso_notice = ""
        self._watchdog_halt_failed = False
        self.finalization = FinalizationTracker(
            config.finalization_hold_periods,
            config.max_final_retries,
            config.final_tx_timeout_periods,
        )
        self._clear_epochs: dict[str, int] = {}
        self._pending_dial_frequency: PendingDialFrequency | None = None
        self._pending_ftx1_band_change: PendingDialFrequency | None = None
        self._pending_ftx1_profile_apply: PendingDialFrequency | None = None
        self.ftx1_band_drive = ftx1_band_drive
        if self.ftx1_band_drive is None and config.ftx1_cat2_enabled and config.ftx1_cat2_confirmed_ftx1:
            self.ftx1_band_drive = FTX1BandDriveController(
                FTX1CatController(
                    config.ftx1_cat2_port,
                    config.ftx1_cat2_baudrate,
                    config.ftx1_cat2_timeout_seconds,
                ),
                config.ftx1_band_profiles,
                ftx1_band_profiles_changed,
                self._request_ftx1_profile_attenuation,
                lambda: self.status.transmitting,
            )
        elif self.ftx1_band_drive is not None:
            # Runtime owns the RX gate and AP1 confirmation path for injected test/custom controllers too.
            self.ftx1_band_drive._set_tx_audio_attenuation = self._request_ftx1_profile_attenuation
            self.ftx1_band_drive._is_transmitting = lambda: self.status.transmitting
        self.ap1_controls_available = False
        self.wsjtx_revision = ""
        self.current_tx_first: bool | None = None
        self.requested_tx_period: bool | None = None
        self.adaptive_state = AdaptiveState.NORMAL
        self.stagnation = StagnationTracker(
            config.stagnation_attempt_window,
            config.stagnation_min_failed_attempts,
            config.stagnation_max_unique_calls,
        )
        self.last_band_change_at: datetime | None = None
        self._band_resume_at: datetime | None = None
        if isinstance(self.control, WsjtxUdpControl):
            self.control.set_disarm_context(self._disarm_context)
        session = current_logging_session()
        if session is not None:
            session.context.feature_flags = {
                "autocall": config.autocall_enabled,
                "direct_reply_patch": config.wsjtx_direct_reply_patched,
            }

    def initialize_ftx1_cat2(self) -> bool:
        """Open and identify the configured CAT-2 port during backend startup."""
        LOGGER.info(
            "[FTX1] CAT-2 config enabled=%s port=%s baud=%d",
            self.config.ftx1_cat2_enabled,
            self.config.ftx1_cat2_port,
            self.config.ftx1_cat2_baudrate,
        )
        if not self.config.ftx1_cat2_enabled or not self.config.ftx1_cat2_confirmed_ftx1:
            return False
        if self.ftx1_band_drive is None:
            LOGGER.error("[FTX1] CAT-2 open failed: controller is unavailable")
            return False
        if not self.ftx1_band_drive.cat.identify():
            return False
        LOGGER.info("[FTX1] CAT-2 ready")
        return True

    def arm_control(self) -> bool:
        """Arm the live UDP control adapter on an explicit GUI request."""
        LOGGER.info("[RUNTIME] ARM request received")
        if not isinstance(self.control, WsjtxUdpControl):
            LOGGER.error("[RUNTIME] ARM request rejected: control is not WsjtxUdpControl")
            return False
        return self.control.arm()

    def _disarm_context(self) -> dict[str, object]:
        heartbeat_age_seconds: float | None = None
        if self._last_runtime_at is not None and self._last_heartbeat_at is not None:
            heartbeat_age_seconds = round((self._last_runtime_at - self._last_heartbeat_at).total_seconds(), 1)
        return {
            "wsjtx_online": bool(self.status.instance_id),
            "heartbeat_age_seconds": heartbeat_age_seconds if heartbeat_age_seconds is not None else "unknown",
            "transmitting": self.status.transmitting,
            "learning_state": "DISABLED",
            "exception": "none",
        }

    def handle(
        self,
        packet: WsjtxPacket | None,
        now: datetime,
        endpoint: Endpoint | None = None,
    ) -> IntendedAction | None:
        self._last_runtime_at = now
        self.control.poll()
        if packet is not None:
            self.control.observe(packet, endpoint)
        handled_action: IntendedAction | None = None
        if isinstance(packet, HeartbeatPacket):
            self._last_heartbeat_at = now
            source = f"{endpoint[0]}:{endpoint[1]}" if endpoint is not None else "unknown"
            LOGGER.info(
                "[WSJTX] heartbeat instance=%s schema=%d version=%s endpoint=%s",
                packet.header.instance_id,
                packet.header.schema,
                packet.version,
                source,
            )
            self.wsjtx_revision = packet.revision
            self.ap1_controls_available = "AP1" in packet.revision.upper()
            LOGGER.info("[WSJTX] revision=%s", packet.revision)
            LOGGER.info("[WSJTX] AP1 controls available=%s", str(self.ap1_controls_available).lower())
            if self._attenuation_query_instance_id and self._attenuation_query_instance_id != packet.header.instance_id:
                self.invalidate_tx_audio_attenuation_state("WSJT-X instance changed")
            if self.ap1_controls_available:
                self._query_tx_audio_attenuation_if_needed(packet.header.instance_id, now)
            else:
                self.invalidate_tx_audio_attenuation_state("AP1 is unavailable")
            session = current_logging_session()
            if session is not None:
                session.context.wsjtx_version = packet.version
                session.context.wsjtx_schema = packet.header.schema
                session.context.wsjtx_instance = packet.header.instance_id
                session.context.wsjtx_endpoint = source
                session.context.wsjtx_revision = packet.revision
                session.context.ap1_controls_available = self.ap1_controls_available
        elif isinstance(packet, TxAudioAttenuationStatePacket):
            self._observe_tx_audio_attenuation(packet.attenuation, now)
            self._finish_pending_ftx1_profile_apply(now)
            self._complete_pending_ftx1_band_change(now)
        elif isinstance(packet, StatusPacket):
            was_transmitting = self.status.transmitting
            period = packet.tr_period if packet.tr_period != 0xFFFFFFFF else None
            self.status = WsjtxStatus(
                instance_id=packet.header.instance_id,
                dial_frequency=packet.dial_frequency,
                mode=packet.mode,
                dx_call=packet.dx_call,
                tx_enabled=packet.tx_enabled,
                transmitting=packet.transmitting,
                tx_df=packet.tx_df,
                tx_message=packet.tx_message,
                period=period,
            )
            band = BandResolver().resolve(packet.dial_frequency) or "unknown"
            self.engine.invalidate_pending_context(packet.header.instance_id, band, packet.mode or "unknown")
            session = current_logging_session()
            if session is not None:
                session.context.band = band
                session.context.mode = packet.mode or "unknown"
            self._observe_finalization_status(packet, now)
            self._observe_adaptive_status(packet, now)
            self._confirm_dial_frequency(packet, now)
            if was_transmitting != packet.transmitting:
                LOGGER.info("[WSJTX] transmitting=%s -> %s", was_transmitting, packet.transmitting)
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
        elif isinstance(packet, DecodePacket):
            self._handle_decode(packet, now, endpoint)
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
            if self.engine.state.matches_remote(packet.dx_call, packet.header.instance_id):
                self._adaptive_success()
            if self.finalization.note_qso_logged(packet.dx_call, packet.header.instance_id):
                LOGGER.info(
                    "[FINALIZE] QSOLogged retained remote=%s waiting local TX",
                    packet.dx_call,
                )
            elif self.engine.complete_qso(packet.dx_call, packet.header.instance_id, "QSOLogged received"):
                self._log_ready_for_next_qso()

        remote_before_expiry = self.engine.state.session.remote_callsign
        if self.engine.state.expire_direct_reply(
            now,
            self.config.direct_reply_confirmation_timeout_seconds,
        ):
            self.note_adaptive_failure(remote_before_expiry, AttemptOutcome.NO_RESPONSE, now)
        remote_before_expiry = self.engine.state.session.remote_callsign
        if self.engine.state.expire_reply_confirmation(
            now,
            self.config.direct_reply_confirmation_timeout_seconds,
        ):
            self.note_adaptive_failure(remote_before_expiry, AttemptOutcome.NO_RESPONSE, now)
        self._maintain_qso_lifecycle(now)
        final_state = self.finalization.state
        if self.finalization.expire(now) and final_state is not None and not final_state.tx_confirmed:
            LOGGER.error(
                "[FINALIZE] local terminal TX missing remote=%s wsjtx_tx_enabled=%s engine_state=%s qso_logged=%s",
                final_state.remote_callsign,
                self.status.tx_enabled,
                self.engine.state.session.state.name,
                final_state.qso_logged,
            )
            if self.engine.complete_qso(
                final_state.remote_callsign,
                final_state.instance_id,
                "final TX timeout",
            ):
                self._log_ready_for_next_qso()
        self._expire_dial_frequency_request(now)
        self._expire_tx_audio_attenuation_request(now)
        self._query_tx_audio_attenuation_if_needed(self._attenuation_query_instance_id, now)
        self._complete_pending_ftx1_band_change(now)
        if self._pending_dial_frequency is not None or self._pending_ftx1_band_change is not None or self._pending_ftx1_profile_apply is not None:
            action = None
        elif self.finalization.active and self.engine.state.session.state is QsoState.IDLE:
            LOGGER.debug("[FINALIZE] candidate selection held during RX grace")
            action = None
        else:
            self._run_adaptive(now)
            action = None if self._band_resume_at is not None and now < self._band_resume_at else self.engine.decide(now)
        if action is not None:
            LOGGER.info(
                "[RUNTIME] requesting control action station=%s kind=%s implementation=%s armed=%s",
                action.station,
                action.kind.name,
                type(self.control).__name__,
                getattr(self.control, "armed", False),
            )
            self._execute_action(action, now)
        self._update_diagnostic_context(now)
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
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency is not None else None
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
                    self._disarm_control("halt_tx_failed_remote_cq", "AutopilotRuntime._handle_decode")
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
                    self._disarm_control("halt_tx_failed_remote_busy", "AutopilotRuntime._handle_decode")
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
        direct_to_local = parsed.is_addressed_to(self.config.local_callsign) and parsed.kind in {
            MessageKind.DIRECTED,
            MessageKind.REPORT,
            MessageKind.R_REPORT,
        }
        if self.finalization.active and direct_to_local:
            self.engine.queue_pending_direct(event, now, "finalization hold")
            return None
        return self.engine.observe(event, now)

    def _log_rejected_direct_packet(self, packet: DecodePacket, blocker: str) -> None:
        parsed = parse_ft8_message(packet.message)
        if parsed is None or not parsed.is_addressed_to(self.config.local_callsign):
            return
        LOGGER.info("[DIRECT] received station=%s df=%d", parsed.sender, packet.delta_frequency)
        LOGGER.info("[DIRECT] hard_blockers=[%s]", blocker)
        LOGGER.info("[DIRECT] soft_blockers=[]")
        LOGGER.info("[DIRECT] rejected station=%s blocker=%s", parsed.sender, blocker)

    def _execute_action(self, action: IntendedAction, now: datetime) -> None:
        outcome = self.control.execute(action, now)
        LOGGER.info("[RUNTIME] control outcome=%s station=%s", outcome.name, action.station)
        self.engine.record_action_outcome(action, outcome, now)
        if outcome is ActionOutcome.SENT:
            self.last_qso_notice = ""
            self._watchdog_halt_failed = False

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
        if terminal_tx and packet.transmitting:
            self.finalization.confirm_final_tx(now)
            if self.engine.state.matches_remote(state.remote_callsign):
                self.engine.state.mark_complete("final 73 transmitted")

    def _observe_adaptive_status(self, packet: StatusPacket, now: datetime) -> None:
        if not packet.transmitting:
            return
        phase = now.second % 30 + now.microsecond / 1_000_000
        observed = True if phase <= 3 else False if 12 <= phase <= 18 else None
        if observed is None:
            return
        self.current_tx_first = observed
        if self.requested_tx_period is not None and observed != self.requested_tx_period:
            LOGGER.error("[CONTROL] SetTxPeriod validation failed requested=%s observed=%s", self.requested_tx_period, observed)
            self.requested_tx_period = None

    def _confirm_dial_frequency(self, packet: StatusPacket, now: datetime) -> None:
        pending = self._pending_dial_frequency
        if pending is None or packet.header.instance_id != pending.instance_id:
            return
        if packet.dial_frequency != pending.frequency_hz:
            return
        self._pending_dial_frequency = None
        if self.ftx1_band_drive is not None and self.config.ftx1_auto_apply_band_profiles:
            self._pending_ftx1_band_change = pending
            self._complete_pending_ftx1_band_change(now)
            return
        self._finish_band_change(pending, packet.tr_period, now)

    def _complete_pending_ftx1_band_change(self, now: datetime) -> None:
        pending = self._pending_ftx1_band_change
        if pending is None or self.status.transmitting:
            return
        assert self.ftx1_band_drive is not None
        if pending.band.lower() not in self.ftx1_band_drive.profiles:
            self._pending_ftx1_band_change = None
            self.ftx1_band_drive.apply_profile(pending.band, self.current_tx_audio_attenuation)
            self._finish_band_change(pending, self.status.period, now)
            return
        if self.current_tx_audio_attenuation is None:
            self._query_tx_audio_attenuation_if_needed(pending.instance_id, now)
            return
        self._pending_ftx1_band_change = None
        self.ftx1_band_drive.apply_profile(pending.band, self.current_tx_audio_attenuation)
        if self.pending_tx_audio_attenuation is not None:
            self._pending_ftx1_profile_apply = pending
            return
        self._finish_band_change(pending, self.status.period, now)

    def _finish_pending_ftx1_profile_apply(self, now: datetime) -> None:
        pending = self._pending_ftx1_profile_apply
        if pending is None or self.pending_tx_audio_attenuation is not None:
            return
        self._pending_ftx1_profile_apply = None
        self._finish_band_change(pending, self.status.period, now)

    def _finish_band_change(self, pending: PendingDialFrequency, period_seconds: int | None, now: datetime) -> None:
        self.adaptive_state = AdaptiveState.BAND_TRIAL
        self.last_band_change_at = now
        self._band_resume_at = now + timedelta(seconds=period_seconds or 15)
        LOGGER.info("[BAND] confirmed=%s", pending.band)

    def _expire_dial_frequency_request(self, now: datetime) -> None:
        pending = self._pending_dial_frequency
        if pending is None or now <= pending.deadline:
            return
        LOGGER.error("[BAND] requested=%d confirmation timeout", pending.frequency_hz)
        self._pending_dial_frequency = None
        self.adaptive_state = AdaptiveState.NORMAL

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
        if self.stagnation.is_stagnating():
            LOGGER.info("[ADAPT] stagnation=true")
            if self.adaptive_state is AdaptiveState.NORMAL:
                self.adaptive_state = (
                    AdaptiveState.PARITY_CHANGE_PENDING
                    if self.config.adaptive_parity_enabled
                    else AdaptiveState.BAND_HOP_PENDING
                )
                LOGGER.info("[ADAPT] state NORMAL -> %s", self.adaptive_state.name)
            elif (
                self.adaptive_state is AdaptiveState.PARITY_TRIAL
                and self.stagnation.failed_attempts >= self.config.parity_trial_failed_attempts
            ):
                self.adaptive_state = AdaptiveState.BAND_HOP_PENDING
                LOGGER.info("[ADAPT] state PARITY_TRIAL -> BAND_HOP_PENDING")

    def _adaptive_success(self) -> None:
        self.stagnation.reset()
        self.adaptive_state = AdaptiveState.NORMAL
        LOGGER.info("[ADAPT] QSO success")
        LOGGER.info("[ADAPT] stagnation reset")

    def reset_adaptive_strategy(self) -> None:
        self.stagnation.reset()
        if self._pending_dial_frequency is None:
            self.adaptive_state = AdaptiveState.NORMAL
        LOGGER.info("[ADAPT] strategy reset")

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
        elif previous is not None and previous != attenuation:
            LOGGER.info("[WSJTX] Manual TX attenuation change %d -> %d", previous, attenuation)
        else:
            LOGGER.info("[WSJTX] TX attenuation state=%d", attenuation)
        pending_save = self._pending_ftx1_profile_save_band
        if pending_save is not None and self.ftx1_band_drive is not None:
            self._pending_ftx1_profile_save_band = None
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

    def _expire_tx_audio_attenuation_request(self, now: datetime) -> None:
        if self.pending_tx_audio_attenuation is not None and self._tx_audio_attenuation_deadline is not None and now > self._tx_audio_attenuation_deadline:
            LOGGER.error("[WSJTX] TX attenuation confirmation timeout requested=%d current=%s", self.pending_tx_audio_attenuation, self.current_tx_audio_attenuation)
            self.pending_tx_audio_attenuation = None
            self._tx_audio_attenuation_deadline = None
            self._finish_pending_ftx1_profile_apply(now)

    def save_ftx1_current_band_profile(self) -> bool:
        controller = self.ftx1_band_drive
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if controller is None or band is None:
            LOGGER.error("[FTX1] Save manual band profile failed: CAT-2 or current band unavailable")
            return False
        if self.status.transmitting:
            LOGGER.error("[FTX1] Save manual band profile failed: WSJT-X is transmitting")
            return False
        if self.current_tx_audio_attenuation is None:
            if not self.ap1_controls_available or not isinstance(self.control, WsjtxUdpControl):
                LOGGER.error("[FTX1] Save manual band profile failed: WSJT-X attenuation state unavailable")
                return False
            self._pending_ftx1_profile_save_band = band
            if self.control.query_tx_audio_attenuation(self.status.instance_id):
                LOGGER.info("[FTX1] Save manual band profile awaiting AP1 attenuation confirmation")
                return True
            self._pending_ftx1_profile_save_band = None
            LOGGER.error("[FTX1] Save manual band profile failed: attenuation query could not be sent")
            return False
        return controller.save_current_profile(band, self.current_tx_audio_attenuation)

    def delete_ftx1_current_band_profile(self) -> bool:
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        return bool(self.ftx1_band_drive is not None and band is not None and self.ftx1_band_drive.delete_profile(band))

    def reset_all_ftx1_band_profiles(self) -> None:
        if self.ftx1_band_drive is not None:
            self.ftx1_band_drive.reset_profiles()

    def _disarm_control(self, reason: str, source: str) -> None:
        if isinstance(self.control, WsjtxUdpControl):
            self.control.disarm(reason, source=source)
        else:
            self.control.disarm(reason)

    def adaptive_snapshot(self) -> dict[str, object]:
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        snapshot = {
            "ap1_available": self.ap1_controls_available,
            "state": self.adaptive_state.name,
            "current_tx_first": self.current_tx_first,
            "requested_tx_period": self.requested_tx_period,
            "current_band": current_band,
            "next_band": self._next_auto_hop_band(),
            "band_changing_to": self._pending_dial_frequency.band if self._pending_dial_frequency else None,
        }
        if self.ftx1_band_drive is not None:
            snapshot["ftx1"] = self.ftx1_band_drive.snapshot(current_band, self.current_tx_audio_attenuation)
        return snapshot

    def _run_adaptive(self, now: datetime) -> None:
        if self.adaptive_state is AdaptiveState.PARITY_CHANGE_PENDING:
            if not self.config.adaptive_parity_enabled or not self._adaptive_safe(now):
                return
            if self.current_tx_first is None:
                return
            requested = not self.current_tx_first
            if self.control.set_tx_period(self.status.instance_id, requested):
                self.requested_tx_period = requested
                self.adaptive_state = AdaptiveState.PARITY_TRIAL
                self.stagnation.reset()
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
            target = self._next_auto_hop_band()
            if target is None:
                return
            frequency = self._band_frequency(target, self.status.mode or "")
            if frequency is None:
                return
            self.engine.clear_candidates()
            self.engine.invalidate_pending_context(self.status.instance_id, target, self.status.mode or "unknown")
            if not self.control.set_dial_frequency(self.status.instance_id, frequency):
                return
            self._pending_dial_frequency = PendingDialFrequency(
                self.status.instance_id,
                frequency,
                target,
                now + timedelta(seconds=self.config.dial_change_confirmation_timeout_seconds),
                BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None,
            )
            self.adaptive_state = AdaptiveState.BAND_CHANGING
            LOGGER.info("[BAND] current=%s", BandResolver().resolve(self.status.dial_frequency) or "unknown")
            LOGGER.info("[BAND] next=%s", target)
            LOGGER.info("[BAND] waiting confirmation")

    def _adaptive_safe(self, now: datetime) -> bool:
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        has_pending_direct = any(
            entry["band"] == current_band for entry in self.engine.pending_direct_snapshot(now)
        )
        return bool(
            self.config.adaptive_operation_enabled
            and self.ap1_controls_available
            and getattr(self.control, "armed", False)
            and self.status.instance_id
            and not self.status.transmitting
            and self.engine.state.session.state is QsoState.IDLE
            and not self.finalization.active
            and self._pending_dial_frequency is None
            and not has_pending_direct
        )

    def _next_auto_hop_band(self) -> str | None:
        allowed = tuple(dict.fromkeys(band.lower() for band in self.config.allowed_auto_hop_bands))
        if not allowed or self.status.dial_frequency is None:
            return None
        current = (BandResolver().resolve(self.status.dial_frequency) or "").lower()
        if current not in allowed:
            return next((band for band in allowed if self._band_frequency(band, self.status.mode or "") is not None), None)
        start = allowed.index(current)
        for offset in range(1, len(allowed)):
            candidate = allowed[(start + offset) % len(allowed)]
            if self._band_frequency(candidate, self.status.mode or "") is not None:
                return candidate
        return None

    def _band_frequency(self, band: str, mode: str) -> int | None:
        override = self.config.auto_hop_band_frequencies.get(band.lower(), {}).get(mode.upper())
        if override is not None:
            return override
        profile = next((item for item in DEFAULT_BAND_PROFILES if item.band.lower() == band.lower()), None)
        return profile.frequency_for(mode) if profile is not None else None

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
            halted = self.control.halt_tx(session.instance_id, "QSO stalled")
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
            }
