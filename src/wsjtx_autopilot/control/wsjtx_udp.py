import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from wsjtx_autopilot.control.base import DatagramTransport, Endpoint
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, IntendedAction, MessageKind, OriginalDecode
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.wsjtx.models import ClearPacket, HeartbeatPacket, WsjtxPacket
from wsjtx_autopilot.wsjtx.protocol import (
    SUPPORTED_SCHEMAS,
    ProtocolError,
    serialize_halt_tx,
    serialize_reply,
    serialize_set_tx_period,
    serialize_set_dial_frequency,
)

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WsjtxSession:
    schema: int
    endpoint: Endpoint
    clear_epoch: int = 0
    ap1_controls_available: bool = False


class WsjtxUdpControl:
    """Armed adapter that initiates WSJT-X QSOs from exact CQ/QRZ Decodes."""

    def __init__(
        self,
        transport: DatagramTransport,
        stale_seconds: float,
        max_actions: int | None,
        kill_switch_file: Path | None = None,
        local_callsign: str = "",
        direct_reply_patched: bool = False,
        armed: bool = True,
    ) -> None:
        if max_actions is not None and max_actions < 1:
            raise ValueError("max_actions must be at least 1")
        self._transport = transport
        self._stale = timedelta(seconds=stale_seconds)
        self._max_actions = max_actions
        self._kill_switch_file = kill_switch_file
        self._local_callsign = local_callsign.upper()
        self._direct_reply_patched = direct_reply_patched
        self._armed = armed
        self._actions_used = 0
        self._sessions: dict[str, WsjtxSession] = {}
        self._used_decodes: set[tuple[object, ...]] = set()
        self._used_final_decodes: set[tuple[object, ...]] = set()
        self._disarm_context: Callable[[], Mapping[str, object]] | None = None
        self.poll()

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def actions_used(self) -> int:
        return self._actions_used

    @property
    def max_actions(self) -> int | None:
        return self._max_actions

    def observe(self, packet: WsjtxPacket, endpoint: Endpoint | None) -> None:
        self.poll()
        if endpoint is None:
            return
        instance_id = packet.header.instance_id
        session = self._sessions.get(instance_id)
        if session is None:
            self._sessions[instance_id] = WsjtxSession(packet.header.schema, endpoint)
            session = self._sessions[instance_id]
        elif session.endpoint != endpoint:
            LOGGER.warning(
                "[CONTROL] ignored packet instance=%s from unexpected endpoint=%s expected=%s",
                instance_id,
                endpoint,
                session.endpoint,
            )
            return
        else:
            session.schema = packet.header.schema
        if isinstance(packet, HeartbeatPacket):
            session.ap1_controls_available = "AP1" in packet.revision.upper()
        if isinstance(packet, ClearPacket):
            session.clear_epoch += 1
            LOGGER.info("[CONTROL] Clear observed instance=%s epoch=%d", instance_id, session.clear_epoch)

    def execute(self, action: IntendedAction, now: datetime) -> ActionOutcome:
        self.poll()
        LOGGER.info(
            "[CONTROL] implementation=%s armed=%s station=%s",
            type(self).__name__,
            self._armed,
            action.station,
        )
        if action.kind is ActionKind.DIRECT_REPLY:
            LOGGER.info("[CONTROL] direct Reply requested station=%s", action.station)
        rejection = self._validate(action, now)
        if rejection is not None:
            LOGGER.warning("[CONTROL] Reply rejected station=%s reason=%s", action.station, rejection)
            LOGGER.warning("[CONTROL] outcome=%s station=%s", ActionOutcome.REJECTED_LOCAL.name, action.station)
            return ActionOutcome.REJECTED_LOCAL

        decode = action.original_decode
        assert decode is not None and decode.source_endpoint is not None
        key = self._decode_key(decode)
        self._used_decodes.add(key)
        LOGGER.info(
            "[CONTROL] target instance=%s schema=%d endpoint=%s:%d",
            decode.instance_id,
            decode.schema,
            decode.source_endpoint[0],
            decode.source_endpoint[1],
        )
        LOGGER.info("[CONTROL] send_reply called station=%s", action.station)
        try:
            datagram = serialize_reply(
                decode.schema,
                decode.instance_id,
                decode.decode_time,
                decode.snr,
                decode.delta_time,
                decode.delta_frequency,
                decode.mode,
                decode.message,
                decode.low_confidence,
                0,
            )
            sent = self._transport.sendto(datagram, decode.source_endpoint)
            LOGGER.info(
                "[UDP] command=Reply endpoint=%s:%d bytes=%d expected=%d outcome=%s",
                decode.source_endpoint[0],
                decode.source_endpoint[1],
                sent,
                len(datagram),
                "sent" if sent == len(datagram) else "partial",
            )
            if sent != len(datagram):
                raise OSError(f"partial UDP send ({sent}/{len(datagram)} bytes)")
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[CONTROL] Reply failed station=%s error=%s", action.station, exc)
            LOGGER.error("[CONTROL] outcome=%s station=%s", ActionOutcome.FAILED.name, action.station)
            return ActionOutcome.FAILED

        self._actions_used += 1
        LOGGER.warning(
            "[CONTROL] %s sent station=%s instance=%s schema=%d endpoint=%s",
            "Direct Reply" if action.kind is ActionKind.DIRECT_REPLY else "Reply",
            action.station,
            decode.instance_id,
            decode.schema,
            decode.source_endpoint,
        )
        if self._max_actions is not None and self._actions_used >= self._max_actions:
            self._disarm("action_limit_reached", "WsjtxUdpControl.execute")
        elif self._max_actions is not None:
            LOGGER.info("[CONTROL] actions=%d/%d", self._actions_used, self._max_actions)
        LOGGER.info("[CONTROL] outcome=%s station=%s", ActionOutcome.SENT.name, action.station)
        return ActionOutcome.SENT

    def poll(self) -> None:
        if self._armed and self._kill_switch_file is not None and self._kill_switch_file.exists():
            self._disarm(f"kill_switch_file:{self._kill_switch_file}", "WsjtxUdpControl.poll")

    def disarm(self, reason: str = "software_kill_switch", *, source: str = "external") -> None:
        self._disarm(reason, source)

    def set_disarm_context(self, provider: Callable[[], Mapping[str, object]]) -> None:
        self._disarm_context = provider

    def _disarm(self, reason: str, source: str) -> None:
        if self._armed:
            self._armed = False
            context: Mapping[str, object] = {}
            if self._disarm_context is not None:
                try:
                    context = self._disarm_context()
                except Exception as exc:
                    context = {"context_error": type(exc).__name__}
            fields = " ".join(f"{key}={value}" for key, value in context.items())
            LOGGER.warning(
                "[CONTROL] DISARM reason=%s source=%s armed_previous_state=true%s",
                reason,
                source,
                f" {fields}" if fields else "",
            )

    def arm(self) -> bool:
        LOGGER.info("[CONTROL] arm() called previous=%s", self._armed)
        if self._kill_switch_file is not None and self._kill_switch_file.exists():
            LOGGER.error("[CONTROL] ARM rejected kill switch file exists: %s", self._kill_switch_file)
            self._disarm("kill_switch_file_present", "WsjtxUdpControl.arm")
            return False
        self._armed = True
        LOGGER.warning("[CONTROL] ARMED implementation=%s max_actions=%s", type(self).__name__, self._max_actions)
        return True

    def halt_tx(self, instance_id: str | None, reason: str) -> bool:
        """Send immediate Halt Tx without consuming an initiation action."""
        if instance_id is None and len(self._sessions) == 1:
            instance_id = next(iter(self._sessions))
        session = self._sessions.get(instance_id or "")
        if session is None:
            LOGGER.error("[CONTROL] HaltTx rejected reason=unknown instance id=%s", instance_id or "-")
            return False
        try:
            datagram = serialize_halt_tx(session.schema, instance_id or "", auto_tx_only=False)
            sent = self._transport.sendto(datagram, session.endpoint)
            if sent != len(datagram):
                raise OSError(f"partial UDP send ({sent}/{len(datagram)} bytes)")
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[CONTROL] HaltTx failed reason=%s error=%s", reason, exc)
            return False
        LOGGER.warning(
            "[UDP] command=HaltTx endpoint=%s:%d instance=%s outcome=sent reason=%s",
            session.endpoint[0],
            session.endpoint[1],
            instance_id,
            reason,
        )
        return True

    def set_tx_period(self, instance_id: str, tx_first: bool) -> bool:
        session = self._ap1_session(instance_id)
        if session is None:
            return False
        try:
            datagram = serialize_set_tx_period(session.schema, instance_id, tx_first)
            sent = self._transport.sendto(datagram, session.endpoint)
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[CONTROL] SetTxPeriod failed error=%s", exc)
            return False
        if sent != len(datagram):
            LOGGER.error("[CONTROL] SetTxPeriod failed partial send")
            return False
        LOGGER.info("[CONTROL] SetTxPeriod requested=%s", "FIRST" if tx_first else "SECOND")
        return True

    def set_dial_frequency(self, instance_id: str, frequency_hz: int) -> bool:
        session = self._ap1_session(instance_id)
        if session is None:
            return False
        try:
            datagram = serialize_set_dial_frequency(session.schema, instance_id, frequency_hz)
            sent = self._transport.sendto(datagram, session.endpoint)
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[CONTROL] SetDialFrequency failed error=%s", exc)
            return False
        if sent != len(datagram):
            LOGGER.error("[CONTROL] SetDialFrequency failed partial send")
            return False
        LOGGER.info("[CONTROL] SetDialFrequency frequency=%d", frequency_hz)
        return True

    def set_tx_audio_attenuation(self, instance_id: str, attenuation: int) -> bool:
        session = self._ap1_session(instance_id)
        if session is None:
            return False
        try:
            from wsjtx_autopilot.wsjtx.protocol import serialize_set_tx_audio_attenuation
            datagram = serialize_set_tx_audio_attenuation(session.schema, instance_id, attenuation)
            sent = self._transport.sendto(datagram, session.endpoint)
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[WSJTX] SetTxAudioAttenuation failed error=%s", exc)
            return False
        if sent != len(datagram):
            LOGGER.error("[WSJTX] SetTxAudioAttenuation failed partial send")
            return False
        LOGGER.info("[WSJTX] SetTxAudioAttenuation sent value=%d", attenuation)
        return True

    def query_tx_audio_attenuation(self, instance_id: str) -> bool:
        # This is a read-only state request and must work before ARM CONTROL.
        session = self._ap1_session(instance_id, require_armed=False)
        if session is None:
            return False
        try:
            from wsjtx_autopilot.wsjtx.protocol import serialize_query_tx_audio_attenuation
            datagram = serialize_query_tx_audio_attenuation(session.schema, instance_id)
            LOGGER.info("[WSJTX] Sending attenuation query id=%s schema=%d", instance_id, session.schema)
            return self._transport.sendto(datagram, session.endpoint) == len(datagram)
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[WSJTX] QueryTxAudioAttenuation failed error=%s", exc)
            return False

    def _ap1_session(self, instance_id: str, *, require_armed: bool = True) -> WsjtxSession | None:
        session = self._sessions.get(instance_id)
        if session is None or not session.ap1_controls_available or (require_armed and not self._armed):
            LOGGER.warning("[CONTROL] AP1 command rejected instance=%s", instance_id)
            return None
        return session

    def retry_final(self, decode: OriginalDecode, reason: str) -> bool:
        """Retry a terminal response from its exact Decode without consuming an action."""
        self.poll()
        continuation_allowed = (
            self._max_actions is not None and self._actions_used >= self._max_actions
        )
        if (not self._armed and not continuation_allowed) or not self._direct_reply_patched:
            LOGGER.warning("[FINALIZE] retry rejected armed=%s direct_patch=%s", self._armed, self._direct_reply_patched)
            return False
        if not decode.is_new or decode.low_confidence or decode.off_air or decode.source_endpoint is None:
            LOGGER.warning("[FINALIZE] retry rejected unsafe Decode")
            return False
        parsed = parse_ft8_message(decode.message)
        if (
            parsed is None
            or parsed.kind not in {MessageKind.RRR, MessageKind.RR73}
            or not parsed.is_addressed_to(self._local_callsign)
        ):
            LOGGER.warning("[FINALIZE] retry rejected non-terminal Decode message=%s", decode.message)
            return False
        session = self._sessions.get(decode.instance_id)
        key = self._decode_key(decode)
        if (
            session is None
            or session.schema != decode.schema
            or session.endpoint != decode.source_endpoint
            or session.clear_epoch != decode.clear_epoch
            or key in self._used_final_decodes
        ):
            LOGGER.warning("[FINALIZE] retry rejected session mismatch or reused Decode")
            return False
        try:
            datagram = serialize_reply(
                decode.schema,
                decode.instance_id,
                decode.decode_time,
                decode.snr,
                decode.delta_time,
                decode.delta_frequency,
                decode.mode,
                decode.message,
                decode.low_confidence,
                0,
            )
            sent = self._transport.sendto(datagram, decode.source_endpoint)
            if sent != len(datagram):
                raise OSError(f"partial UDP send ({sent}/{len(datagram)} bytes)")
        except (OSError, ProtocolError) as exc:
            LOGGER.error("[FINALIZE] retry failed remote=%s error=%s", parsed.sender, exc)
            return False
        self._used_final_decodes.add(key)
        LOGGER.warning("[FINALIZE] terminal Reply sent remote=%s reason=%s", parsed.sender, reason)
        return True

    def _validate(self, action: IntendedAction, now: datetime) -> str | None:
        if not self._armed:
            return "control is disarmed"
        if self._max_actions is not None and self._actions_used >= self._max_actions:
            return "maximum initiations reached"
        if action.kind not in {ActionKind.CQ_REPLY, ActionKind.DIRECT_REPLY} or action.original_decode is None:
            return "missing exact source Decode"
        decode = action.original_decode
        if not decode.is_new:
            return "Decode New=false"
        if decode.low_confidence:
            return "low-confidence Decode"
        if decode.off_air:
            return "off-air Decode"
        if action.observed_at is None or now - action.observed_at > self._stale:
            return "stale Decode"
        if decode.source_endpoint is None:
            return "missing source endpoint"
        parsed = parse_ft8_message(decode.message)
        if parsed is None:
            return "ambiguous source Decode"
        if action.kind is ActionKind.CQ_REPLY:
            if parsed.kind not in {MessageKind.CQ, MessageKind.QRZ}:
                return "CQ Reply is restricted to CQ/QRZ Decodes"
        else:
            if not self._direct_reply_patched:
                return "connected WSJT-X is not declared Direct Reply patched"
            if parsed.kind in {MessageKind.CQ, MessageKind.QRZ} or not parsed.is_addressed_to(self._local_callsign):
                return "Direct Reply requires a Decode addressed to the local callsign"
        if parsed.sender != action.station:
            return "selected station does not match source Decode"
        if decode.schema not in SUPPORTED_SCHEMAS:
            return "unsupported schema"
        session = self._sessions.get(decode.instance_id)
        if session is None:
            return "unknown WSJT-X instance"
        if session.schema != decode.schema:
            return "session schema mismatch"
        if session.endpoint != decode.source_endpoint:
            return "session endpoint mismatch"
        if session.clear_epoch != decode.clear_epoch:
            return "Decode invalidated by Clear"
        if self._decode_key(decode) in self._used_decodes:
            return "Decode already actioned"
        return None

    @staticmethod
    def _decode_key(decode: OriginalDecode) -> tuple[object, ...]:
        return (
            decode.instance_id,
            decode.schema,
            decode.decode_time,
            decode.snr,
            decode.delta_time,
            decode.delta_frequency,
            decode.mode,
            decode.message,
            decode.source_endpoint,
            decode.clear_epoch,
        )
