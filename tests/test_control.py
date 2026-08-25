import socket
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, DecodeEvent, IntendedAction, OriginalDecode
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.main import build_control
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HaltTxPacket,
    PacketHeader,
    QsoLoggedPacket,
    ReplyPacket,
    SetTxDfPacket,
    StatusPacket,
)
from wsjtx_autopilot.wsjtx.protocol import parse_datagram
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.store import WorkedQsoStore

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
ENDPOINT = ("127.0.0.1", 2237)


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        if self.fail:
            raise OSError("simulated send failure")
        self.sent.append((data, endpoint))
        return len(data)


def original(
    message: str = "CQ OH2ZZ KP20",
    *,
    schema: int = 2,
    instance_id: str = "WSJT-X",
    endpoint: tuple[str, int] | None = ENDPOINT,
    low_confidence: bool = False,
    is_new: bool = True,
    clear_epoch: int = 0,
    off_air: bool = False,
) -> OriginalDecode:
    return OriginalDecode(
        instance_id,
        schema,
        time(12, 34, 56, 789000),
        -8,
        0.2,
        1460,
        "~",
        message,
        low_confidence,
        is_new,
        endpoint,
        clear_epoch,
        off_air,
    )


def action(
    decode: OriginalDecode | None = None,
    kind: ActionKind = ActionKind.CQ_REPLY,
    station: str = "OH2ZZ",
) -> IntendedAction:
    return IntendedAction(kind, station, "CQ candidate", decode or original(), NOW)


def packet(decode: OriginalDecode) -> DecodePacket:
    return DecodePacket(
        PacketHeader(decode.schema, 2, decode.instance_id),
        decode.is_new,
        decode.decode_time,
        decode.snr,
        decode.delta_time,
        decode.delta_frequency,
        decode.mode,
        decode.message,
        decode.low_confidence,
        decode.off_air,
    )


def armed_adapter(
    transport: FakeTransport,
    decode: OriginalDecode,
    *,
    max_actions: int = 2,
    direct_reply_patched: bool = False,
) -> WsjtxUdpControl:
    adapter = WsjtxUdpControl(
        transport,
        stale_seconds=15,
        max_actions=max_actions,
        local_callsign="F4NJU",
        direct_reply_patched=direct_reply_patched,
    )
    assert decode.source_endpoint is not None
    adapter.observe(packet(decode), decode.source_endpoint)
    return adapter


def test_preserves_exact_decode_fields_in_reply() -> None:
    transport = FakeTransport()
    decode = original(schema=3)
    adapter = armed_adapter(transport, decode)

    assert adapter.execute(action(decode), NOW) is ActionOutcome.SENT
    reply = parse_datagram(transport.sent[0][0])

    assert isinstance(reply, ReplyPacket)
    assert reply.header.schema == decode.schema
    assert reply.header.instance_id == decode.instance_id
    assert reply.decode_time == decode.decode_time
    assert reply.snr == decode.snr
    assert reply.delta_time == decode.delta_time
    assert reply.delta_frequency == decode.delta_frequency
    assert reply.mode == decode.mode
    assert reply.message == decode.message
    assert reply.low_confidence == decode.low_confidence


def test_qrz_can_send_reply_when_armed() -> None:
    transport = FakeTransport()
    decode = original("QRZ OH2ZZ KP20")
    adapter = armed_adapter(transport, decode)

    assert adapter.execute(action(decode), NOW) is ActionOutcome.SENT
    assert len(transport.sent) == 1


def test_stock_mode_rejects_direct_reply() -> None:
    transport = FakeTransport()
    decode = original("F4NJU ON4ABC -08")
    adapter = armed_adapter(transport, decode)

    outcome = adapter.execute(action(decode, ActionKind.DIRECT_REPLY, "ON4ABC"), NOW)

    assert outcome is ActionOutcome.REJECTED_LOCAL
    assert transport.sent == []


def test_patched_mode_sends_exact_direct_reply() -> None:
    transport = FakeTransport()
    decode = original("F4NJU ON4ABC -08")
    adapter = armed_adapter(transport, decode, direct_reply_patched=True)

    outcome = adapter.execute(action(decode, ActionKind.DIRECT_REPLY, "ON4ABC"), NOW)

    assert outcome is ActionOutcome.SENT
    reply = parse_datagram(transport.sent[0][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.message == "F4NJU ON4ABC -08"


def test_patched_mode_rejects_direct_reply_addressed_elsewhere() -> None:
    transport = FakeTransport()
    decode = original("F4ABC ON4ABC -08")
    adapter = armed_adapter(transport, decode, direct_reply_patched=True)

    outcome = adapter.execute(action(decode, ActionKind.DIRECT_REPLY, "ON4ABC"), NOW)

    assert outcome is ActionOutcome.REJECTED_LOCAL
    assert transport.sent == []


def test_cq_reply_rejects_exchange_direct_call_and_ambiguous_text() -> None:
    for message in ("EA1C SQ5ANT R-13", "F4NJU ON4ABC -08", "CQ HELLO THERE"):
        transport = FakeTransport()
        decode = original(message)
        adapter = armed_adapter(transport, decode)

        assert adapter.execute(action(decode), NOW) is ActionOutcome.REJECTED_LOCAL
        assert transport.sent == []


def test_low_confidence_replay_and_stale_decodes_are_rejected() -> None:
    cases = (
        (original(low_confidence=True), NOW),
        (original(is_new=False), NOW),
        (original(), NOW + timedelta(seconds=16)),
        (original(off_air=True), NOW),
    )
    for decode, now in cases:
        transport = FakeTransport()
        adapter = armed_adapter(transport, decode)

        assert adapter.execute(action(decode), now) is ActionOutcome.REJECTED_LOCAL
        assert transport.sent == []


def test_clear_invalidates_decode() -> None:
    transport = FakeTransport()
    decode = original()
    adapter = armed_adapter(transport, decode)
    adapter.observe(ClearPacket(PacketHeader(2, 3, "WSJT-X"), None), ENDPOINT)

    assert adapter.execute(action(decode), NOW) is ActionOutcome.REJECTED_LOCAL
    assert transport.sent == []


def test_wrong_instance_or_endpoint_is_rejected() -> None:
    for decode in (
        original(instance_id="OTHER"),
        original(endpoint=("127.0.0.1", 9999)),
    ):
        transport = FakeTransport()
        adapter = WsjtxUdpControl(transport, stale_seconds=15, max_actions=2)
        adapter.observe(packet(original()), ENDPOINT)

        assert adapter.execute(action(decode), NOW) is ActionOutcome.REJECTED_LOCAL
        assert transport.sent == []


def test_control_flag_without_second_arm_stays_dry_run() -> None:
    transport = FakeTransport()
    control = build_control(transport, replace(AppConfig(), control_enabled=True), None)

    assert isinstance(control, DryRunControl)
    assert control.execute(action(), NOW) is ActionOutcome.PROPOSED_ONLY
    assert transport.sent == []


def test_default_mode_stays_dry_run_and_sends_nothing() -> None:
    transport = FakeTransport()
    control = build_control(transport, AppConfig(), None)

    assert isinstance(control, DryRunControl)
    assert control.execute(action(), NOW) is ActionOutcome.PROPOSED_ONLY
    assert transport.sent == []


def test_same_decode_cannot_be_sent_twice() -> None:
    transport = FakeTransport()
    decode = original()
    adapter = armed_adapter(transport, decode, max_actions=2)

    assert adapter.execute(action(decode), NOW) is ActionOutcome.SENT
    assert adapter.execute(action(decode), NOW) is ActionOutcome.REJECTED_LOCAL
    assert len(transport.sent) == 1


def test_halt_tx_remains_available_after_action_limit_without_consuming_action() -> None:
    transport = FakeTransport()
    decode = original()
    adapter = armed_adapter(transport, decode, max_actions=1)
    assert adapter.execute(action(decode), NOW) is ActionOutcome.SENT
    assert not adapter.armed

    assert adapter.halt_tx("WSJT-X", "QSO stalled")

    assert adapter.actions_used == 1
    assert len(transport.sent) == 2
    assert isinstance(parse_datagram(transport.sent[1][0]), HaltTxPacket)


def test_kill_switch_file_disarms_adapter(tmp_path: Path) -> None:
    kill_switch = tmp_path / "DISARM"
    kill_switch.write_text("stop", encoding="ascii")
    transport = FakeTransport()
    adapter = WsjtxUdpControl(transport, 15, 1, kill_switch)
    decode = original()
    adapter.observe(packet(decode), ENDPOINT)

    assert not adapter.armed
    assert adapter.execute(action(decode), NOW) is ActionOutcome.REJECTED_LOCAL
    assert transport.sent == []


def test_local_udp_reply_and_one_shot_disarm() -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.2)
        endpoint = receiver.getsockname()
        decode = original(endpoint=(str(endpoint[0]), int(endpoint[1])))
        adapter = WsjtxUdpControl(sender, 15, 1)
        adapter.observe(packet(decode), decode.source_endpoint)

        assert adapter.execute(action(decode), NOW) is ActionOutcome.SENT
        data, _ = receiver.recvfrom(65_535)

        assert isinstance(parse_datagram(data), ReplyPacket)
        assert not adapter.armed
    finally:
        sender.close()
        receiver.close()


def runtime_decode(
    message: str,
    endpoint: tuple[str, int],
    *,
    is_new: bool = True,
    low_confidence: bool = False,
    off_air: bool = False,
    df: int = 1460,
    decode_second: int = 0,
) -> DecodePacket:
    return DecodePacket(
        PacketHeader(2, 2, "WSJT-X"),
        is_new,
        time(12, 0, decode_second),
        -8,
        0.2,
        df,
        "~",
        message,
        low_confidence,
        off_air,
    )


def runtime_status(
    dx_call: str,
    *,
    tx_enabled: bool,
    transmitting: bool = False,
    tx_message: str = "",
    tx_df: int = 900,
) -> StatusPacket:
    return StatusPacket(
        PacketHeader(2, 1, "WSJT-X"),
        14_074_000,
        "FT8",
        dx_call,
        "-08",
        "FT8",
        tx_enabled,
        transmitting,
        False,
        900,
        tx_df,
        "F4NJU",
        "JN18",
        "",
        False,
        "",
        False,
        0,
        0xFFFFFFFF,
        15,
        "Default",
        tx_message,
    )


def qso_logged(station: str, instance_id: str = "WSJT-X") -> QsoLoggedPacket:
    return QsoLoggedPacket(
        PacketHeader(2, 5, instance_id),
        NOW,
        station,
        "JO20",
        14_074_000,
        "FT8",
        "-08",
        "-10",
        "50",
        "",
        "",
        NOW,
        "F4NJU",
        "F4NJU",
        "JN18",
        "",
        "",
        "",
    )


def control_runtime(
    transport: FakeTransport,
    max_actions: int = 2,
    *,
    direct_reply_patched: bool = False,
) -> AutopilotRuntime:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=max_actions,
        wsjtx_direct_reply_patched=direct_reply_patched,
    )
    control = WsjtxUdpControl(
        transport,
        15,
        max_actions,
        local_callsign="F4NJU",
        direct_reply_patched=direct_reply_patched,
    )
    return AutopilotRuntime(config, control=control)


def test_armed_cq_send_success_engages_qso_and_blocks_another() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)

    first = app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    second = app.handle(runtime_decode("CQ RA3Y KO73", ENDPOINT, df=1200), NOW, ENDPOINT)

    assert first is not None
    assert second is None
    assert len(transport.sent) == 1
    assert app.engine.state.session.state is QsoState.CALLING_STATION
    assert app.engine.state.session.remote_callsign == "OH2ZZ"


def test_send_failure_leaves_state_idle() -> None:
    transport = FakeTransport(fail=True)
    app = control_runtime(transport)

    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None


def test_direct_send_failure_leaves_state_idle() -> None:
    transport = FakeTransport(fail=True)
    app = control_runtime(transport, direct_reply_patched=True)

    app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT), NOW, ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None


def test_smart_tx_waits_for_status_before_reply_and_locks_df() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=2,
        wsjtx_set_tx_df_patched=True,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 2))
    app.handle(runtime_decode("EA1C SQ5ANT R-13", ENDPOINT, df=500), NOW, ENDPOINT)

    action = app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT, df=895), NOW + timedelta(seconds=1), ENDPOINT)

    assert action is not None and action.selected_tx_df != 895
    requested = action.selected_tx_df
    assert requested is not None
    assert len(transport.sent) == 1
    assert isinstance(parse_datagram(transport.sent[0][0]), SetTxDfPacket)
    assert app.engine.state.session.state is QsoState.IDLE

    app.handle(runtime_status("", tx_enabled=False, tx_df=requested), NOW + timedelta(seconds=2), ENDPOINT)

    assert isinstance(parse_datagram(transport.sent[1][0]), ReplyPacket)
    assert app.engine.state.session.chosen_tx_df == requested
    app.handle(runtime_decode("F4NJU OH2ZZ -08", ENDPOINT, df=1400), NOW + timedelta(seconds=3), ENDPOINT)
    assert app.engine.state.session.chosen_tx_df == requested


def test_smart_tx_direct_call_uses_remote_df_when_occupancy_is_empty() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        wsjtx_direct_reply_patched=True,
        wsjtx_set_tx_df_patched=True,
    )
    control = WsjtxUdpControl(transport, 15, 2, local_callsign="F4NJU", direct_reply_patched=True)
    app = AutopilotRuntime(config, control=control)

    action = app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT, df=2062), NOW, ENDPOINT)

    assert action is not None and action.selected_tx_df == 2062
    set_packet = parse_datagram(transport.sent[0][0])
    assert isinstance(set_packet, SetTxDfPacket) and set_packet.tx_df == 2062
    app.handle(runtime_status("", tx_enabled=False, tx_df=2062), NOW + timedelta(seconds=1), ENDPOINT)
    assert isinstance(parse_datagram(transport.sent[1][0]), ReplyPacket)
    assert app.engine.state.session.remote_df == 2062
    assert app.engine.state.session.chosen_tx_df == 2062


def test_smart_tx_off_sets_remote_df() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        smart_tx_frequency=False,
        wsjtx_set_tx_df_patched=True,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 2))
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT, df=895), NOW, ENDPOINT)
    packet = parse_datagram(transport.sent[0][0])
    assert isinstance(packet, SetTxDfPacket)
    assert packet.tx_df == 895


def test_smart_tx_confirmation_timeout_falls_back_to_remote_df() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        wsjtx_set_tx_df_patched=True,
        tx_df_confirmation_timeout_seconds=1,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 2))
    app.handle(runtime_decode("EA1C SQ5ANT R-13", ENDPOINT, df=500), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT, df=895), NOW + timedelta(seconds=1), ENDPOINT)

    app.handle(None, NOW + timedelta(seconds=3), ENDPOINT)

    fallback = parse_datagram(transport.sent[1][0])
    assert isinstance(fallback, SetTxDfPacket)
    assert fallback.tx_df == 895
    assert app.engine.state.session.state is QsoState.IDLE


def test_direct_call_preempts_cq_while_set_tx_df_is_unconfirmed() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0.25,
        allow_dupes=True,
        wsjtx_direct_reply_patched=True,
        wsjtx_set_tx_df_patched=True,
    )
    control = WsjtxUdpControl(
        transport,
        15,
        2,
        local_callsign="F4NJU",
        direct_reply_patched=True,
    )
    app = AutopilotRuntime(config, control=control)

    assert app.handle(runtime_decode("CQ TA1SW KN41", ENDPOINT, df=900), NOW, ENDPOINT) is None
    cq_action = app.handle(None, NOW + timedelta(milliseconds=250), ENDPOINT)
    assert cq_action is not None and cq_action.station == "TA1SW"
    assert isinstance(parse_datagram(transport.sent[0][0]), SetTxDfPacket)

    assert app.handle(
        runtime_decode("F4NJU RW1CW KO59", ENDPOINT, df=1200),
        NOW + timedelta(milliseconds=260),
        ENDPOINT,
    ) is None
    assert len(transport.sent) == 1

    direct_action = app.handle(None, NOW + timedelta(milliseconds=510), ENDPOINT)
    assert direct_action is not None and direct_action.station == "RW1CW"
    direct_set = parse_datagram(transport.sent[1][0])
    assert isinstance(direct_set, SetTxDfPacket)
    app.handle(
        runtime_status("", tx_enabled=False, tx_df=direct_set.tx_df),
        NOW + timedelta(milliseconds=520),
        ENDPOINT,
    )
    reply = parse_datagram(transport.sent[2][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.message == "F4NJU RW1CW KO59"


def test_direct_reply_without_status_confirmation_times_out_to_idle() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, direct_reply_patched=True)

    app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT), NOW, ENDPOINT)
    assert app.engine.state.session.state is QsoState.DIRECT_REPLY_SENT

    app.handle(None, NOW + timedelta(seconds=21), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None


def test_cq_reply_without_status_or_remote_confirmation_times_out_to_idle() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    assert app.engine.state.session.state is QsoState.CALLING_STATION

    app.handle(None, NOW + timedelta(seconds=21), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None


def test_direct_reply_requires_coherent_status_confirmation() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, direct_reply_patched=True)
    app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT), NOW, ENDPOINT)

    app.handle(runtime_status("ON4ABC", tx_enabled=False), NOW + timedelta(seconds=1), ENDPOINT)
    assert app.engine.state.session.state is QsoState.DIRECT_REPLY_SENT

    app.handle(
        runtime_status("ON4ABC", tx_enabled=True, tx_message="ON4ABC F4NJU R-08"),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    )

    assert app.engine.state.session.state is QsoState.CALLING_STATION
    assert app.engine.state.session.remote_callsign == "ON4ABC"


def test_runtime_never_actions_direct_low_confidence_or_old_decode() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)

    app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT), NOW, ENDPOINT)
    app.engine.state.reset()
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT, low_confidence=True, df=1461), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ RA3Y KO73", ENDPOINT, is_new=False, df=1462), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ F5ABC JN18", ENDPOINT, off_air=True, df=1463), NOW, ENDPOINT)

    assert transport.sent == []


def test_runtime_clear_removes_pending_cq() -> None:
    transport = FakeTransport()
    config = replace(AppConfig(), candidate_collection_seconds=1, max_initiation_attempts=2)
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 2))
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(ClearPacket(PacketHeader(2, 3, "WSJT-X"), None), NOW, ENDPOINT)
    app.handle(None, NOW + timedelta(seconds=1), ENDPOINT)

    assert transport.sent == []
    assert app.engine.state.session.state is QsoState.IDLE


def test_qso_logged_for_active_remote_completes_and_cleans_session() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(qso_logged("OH2ZZ"), NOW + timedelta(seconds=30), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert app.control.actions_used == 1


def test_qso_logged_for_other_station_does_not_complete_active_qso() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(qso_logged("DL1XYZ"), NOW + timedelta(seconds=10), ENDPOINT)

    assert app.engine.state.session.state is QsoState.CALLING_STATION
    assert app.engine.state.session.remote_callsign == "OH2ZZ"


def test_two_qsos_use_two_actions_and_third_initiation_is_blocked() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=2)

    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    assert app.control.actions_used == 1
    app.handle(qso_logged("OH2ZZ"), NOW + timedelta(seconds=20), ENDPOINT)
    assert app.engine.state.session.state is QsoState.IDLE

    app.handle(runtime_decode("CQ DL1XYZ JO40", ENDPOINT, df=1200), NOW + timedelta(seconds=30), ENDPOINT)
    assert app.control.actions_used == 2
    assert app.engine.state.session.remote_callsign == "DL1XYZ"
    assert not app.control.armed  # type: ignore[attr-defined]

    # Auto Seq traffic still advances the already-started second QSO.
    app.handle(runtime_decode("F4NJU DL1XYZ -05", ENDPOINT, df=1201), NOW + timedelta(seconds=40), ENDPOINT)
    assert app.engine.state.session.state is QsoState.QSO_ACTIVE
    assert app.control.actions_used == 2
    app.handle(qso_logged("DL1XYZ"), NOW + timedelta(seconds=50), ENDPOINT)
    assert app.engine.state.session.state is QsoState.IDLE

    app.handle(runtime_decode("CQ F5ABC JN18", ENDPOINT, df=1300), NOW + timedelta(seconds=60), ENDPOINT)
    assert len(transport.sent) == 2
    assert app.control.actions_used == 2
    assert app.engine.state.session.state is QsoState.IDLE


def test_terminal_decodes_only_complete_the_matching_local_qso() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(runtime_decode("EA1C SQ5ANT RR73", ENDPOINT, df=1100), NOW + timedelta(seconds=5), ENDPOINT)
    assert app.engine.state.session.state is QsoState.CALLING_STATION

    app.handle(runtime_decode("F4NJU OH2ZZ RR73", ENDPOINT, df=1101), NOW + timedelta(seconds=10), ENDPOINT)
    assert app.engine.state.session.state is QsoState.COMPLETE
    app.handle(None, NOW + timedelta(seconds=13), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert app.control.actions_used == 1


def test_qso_timeout_ignores_persistent_dx_call_and_returns_idle() -> None:
    transport = FakeTransport()
    config = replace(AppConfig(), candidate_collection_seconds=0, qso_timeout_seconds=10)
    control = WsjtxUdpControl(transport, 15, 2)
    app = AutopilotRuntime(config, control=control)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_status("OH2ZZ", tx_enabled=False), NOW + timedelta(seconds=1), ENDPOINT)

    app.handle(None, NOW + timedelta(seconds=11), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None


def test_direct_reply_uses_same_qso_logged_completion_cycle() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, direct_reply_patched=True)
    app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT), NOW, ENDPOINT)
    app.handle(
        runtime_status("ON4ABC", tx_enabled=True, tx_message="ON4ABC F4NJU R-08"),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )

    app.handle(qso_logged("ON4ABC"), NOW + timedelta(seconds=30), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert app.control.actions_used == 1


def test_remote_reply_to_local_keeps_qso_active() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(runtime_decode("F4NJU OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)

    assert app.engine.state.session.state is QsoState.QSO_ACTIVE
    assert app.engine.state.session.remote_callsign == "OH2ZZ"


def test_remote_reply_to_third_party_aborts_immediately_and_only_cools_remote() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.control.armed  # type: ignore[attr-defined]
    cooldown = app.engine.cooldown_for("OH2ZZ", NOW + timedelta(seconds=1))
    assert cooldown is not None
    assert cooldown.kind.name == "REMOTE_BUSY_OTHER_QSO"

    action = app.handle(runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1202), NOW + timedelta(seconds=2), ENDPOINT)
    assert action is not None
    assert action.station == "R3KLE"


def test_bracketed_remote_reply_to_third_party_aborts_immediately() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_decode("F4NJU <II7MGXX> +07", ENDPOINT), NOW, ENDPOINT)
    assert app.engine.state.session.remote_callsign == "II7MGXX"

    app.handle(
        runtime_decode("UT2UB <II7MGXX> +13", ENDPOINT, df=1201),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.cooldown_for("II7MGXX", NOW + timedelta(seconds=1)) is not None


def test_candidate_received_during_qso_is_selected_in_same_window_after_remote_busy() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    assert app.handle(
        runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1201),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    ) is None

    action = app.handle(
        runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1202),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    )

    assert action is not None
    assert action.station == "R3KLE"
    assert app.engine.state.session.remote_callsign == "R3KLE"


def test_persistent_old_dx_call_does_not_block_new_candidate_after_remote_busy() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_status("OH2ZZ", tx_enabled=False), NOW + timedelta(seconds=2), ENDPOINT)

    action = app.handle(runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1202), NOW + timedelta(seconds=3), ENDPOINT)

    assert action is not None
    assert action.station == "R3KLE"


def test_remote_busy_halt_tx_does_not_disarm_control() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(
        runtime_status("OH2ZZ", tx_enabled=True, tx_message="OH2ZZ F4NJU -08"),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )

    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=2), ENDPOINT)

    assert isinstance(parse_datagram(transport.sent[-1][0]), HaltTxPacket)
    assert app.control.armed  # type: ignore[attr-defined]
    assert app.control.actions_used == 1


def test_failed_remote_busy_halt_retains_active_qso_and_disarms() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(
        runtime_status("OH2ZZ", tx_enabled=True, tx_message="OH2ZZ F4NJU -08"),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )
    transport.fail = True

    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=2), ENDPOINT)

    assert app.engine.state.session.remote_callsign == "OH2ZZ"
    assert app.engine.state.session.state is not QsoState.IDLE
    assert not app.control.armed  # type: ignore[attr-defined]


def test_remote_busy_cooldown_expires_without_global_delay() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=4,
        remote_busy_cooldown_seconds=30,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 4))
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)

    assert app.handle(
        runtime_decode("CQ OH2ZZ KP20", ENDPOINT, df=1202),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    ) is None
    assert app.handle(
        runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1203),
        NOW + timedelta(seconds=3),
        ENDPOINT,
    ) is not None
    app.handle(qso_logged("R3KLE"), NOW + timedelta(seconds=10), ENDPOINT)

    action = app.handle(
        runtime_decode("CQ OH2ZZ KP20", ENDPOINT, df=1204),
        NOW + timedelta(seconds=32),
        ENDPOINT,
    )
    assert action is not None
    assert action.station == "OH2ZZ"


def test_remote_busy_abort_does_not_create_worked_qso(tmp_path: Path) -> None:
    transport = FakeTransport()
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        config = replace(AppConfig(), candidate_collection_seconds=0, max_initiation_attempts=3)
        app = AutopilotRuntime(
            config,
            control=WsjtxUdpControl(transport, 15, 3),
            worked_service=service,
        )
        app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
        app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

        app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)

        assert service.count(NOW.date()) == 0
        assert app.engine.state.session.state is QsoState.IDLE


def test_one_remote_cq_is_tolerated_and_progress_resets_counter() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT), NOW, ENDPOINT)

    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)

    assert app.engine.state.session.state is QsoState.CALLING_STATION
    assert app.engine.state.session.remote_cq_count == 1
    assert app.control.actions_used == 1

    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT, df=1250), NOW + timedelta(seconds=1), ENDPOINT)
    assert app.engine.state.session.remote_cq_count == 1

    app.handle(runtime_decode("F4NJU UT6O -14", ENDPOINT, df=1202), NOW + timedelta(seconds=2), ENDPOINT)
    assert app.engine.state.session.state is QsoState.QSO_ACTIVE
    assert app.engine.state.session.remote_cq_count == 0


def test_two_remote_cqs_abort_only_remote_and_allow_other_station() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)

    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT, df=1202, decode_second=15), NOW + timedelta(seconds=2), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.control.actions_used == 1
    assert app.engine.cooldown_for("UT6O", NOW + timedelta(seconds=2)) is not None
    assert app.handle(
        runtime_decode("CQ UT6O KN87", ENDPOINT, df=1203),
        NOW + timedelta(seconds=3),
        ENDPOINT,
    ) is None
    action = app.handle(
        runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1204),
        NOW + timedelta(seconds=4),
        ENDPOINT,
    )
    assert action is not None and action.station == "R3KLE"


def test_remote_returned_to_cq_cooldown_expires() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=3,
        remote_returned_to_cq_cooldown_seconds=5,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3))
    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode("CQ UT6O KN87", ENDPOINT, df=1202, decode_second=15), NOW + timedelta(seconds=2), ENDPOINT)
    assert app.handle(
        runtime_decode("CQ UT6O KN87", ENDPOINT, df=1203),
        NOW + timedelta(seconds=3),
        ENDPOINT,
    ) is None

    action = app.handle(
        runtime_decode("CQ UT6O KN87", ENDPOINT, df=1204),
        NOW + timedelta(seconds=8),
        ENDPOINT,
    )
    assert action is not None and action.station == "UT6O"


def start_finalization(app: AutopilotRuntime) -> None:
    app.handle(runtime_decode("CQ UI6O KN85", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU UI6O -10", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1202), NOW + timedelta(seconds=2), ENDPOINT)
    assert app.finalization.active
    app.handle(
        runtime_status("UI6O", tx_enabled=True, transmitting=True, tx_message="UI6O F4NJU 73"),
        NOW + timedelta(seconds=3),
        ENDPOINT,
    )
    assert app.finalization.state is not None and app.finalization.state.tx_confirmed


def test_terminal_exchange_creates_finalization_and_qso_logged_preserves_it() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)

    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.finalization.active
    assert app.finalization.state is not None
    assert app.finalization.state.remote_callsign == "UI6O"


def test_finalization_hold_collects_candidate_then_releases_after_one_rx_period() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)

    assert app.handle(
        runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1203),
        NOW + timedelta(seconds=5),
        ENDPOINT,
    ) is None
    assert app.handle(None, NOW + timedelta(seconds=17), ENDPOINT) is None

    action = app.handle(None, NOW + timedelta(seconds=18), ENDPOINT)
    assert action is not None and action.station == "R3KLE"


def test_repeated_rrr_retries_terminal_without_new_action() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
    actions_before = app.control.actions_used
    sent_before = len(transport.sent)

    app.handle(
        runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1203, decode_second=15),
        NOW + timedelta(seconds=5),
        ENDPOINT,
    )

    assert len(transport.sent) == sent_before + 1
    assert isinstance(parse_datagram(transport.sent[-1][0]), ReplyPacket)
    assert app.control.actions_used == actions_before
    assert app.engine.state.session.state is QsoState.IDLE
    assert app.finalization.state is not None and app.finalization.state.retry_count == 1


def test_same_period_terminal_duplicate_does_not_retry() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
    sent_before = len(transport.sent)

    app.handle(runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1700), NOW + timedelta(seconds=5), ENDPOINT)

    assert len(transport.sent) == sent_before
    assert app.finalization.state is not None and app.finalization.state.retry_count == 0


def test_final_retry_remains_available_after_cli_action_limit_disarms_initiations() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=1, direct_reply_patched=True)
    start_finalization(app)
    assert not app.control.armed  # type: ignore[attr-defined]
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
    sent_before = len(transport.sent)

    app.handle(
        runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1701, decode_second=15),
        NOW + timedelta(seconds=5),
        ENDPOINT,
    )

    assert len(transport.sent) == sent_before + 1
    assert app.control.actions_used == 1


def test_rr73_starts_and_retries_terminal_finalization() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_decode("CQ UI6O KN85", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU UI6O RR73", ENDPOINT, df=1201), NOW + timedelta(seconds=1), ENDPOINT)
    assert app.finalization.active
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=2), ENDPOINT)

    app.handle(
        runtime_decode("F4NJU UI6O RR73", ENDPOINT, df=1202, decode_second=15),
        NOW + timedelta(seconds=3),
        ENDPOINT,
    )

    assert app.finalization.state is not None and app.finalization.state.retry_count == 1
    assert app.control.actions_used == 1


def test_remote_73_closes_finalization_and_other_station_terminal_does_not_retry() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
    sent_before = len(transport.sent)

    app.handle(runtime_decode("F4NJU DL1XYZ RRR", ENDPOINT, df=1203), NOW + timedelta(seconds=5), ENDPOINT)
    assert len(transport.sent) == sent_before
    assert app.finalization.active

    app.handle(runtime_decode("F4NJU UI6O 73", ENDPOINT, df=1204), NOW + timedelta(seconds=6), ENDPOINT)
    assert not app.finalization.active


def test_final_retry_limit_prevents_fourth_retry() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
    sent_before = len(transport.sent)
    for index in range(3):
        app.handle(
            runtime_decode(
                "F4NJU UI6O RRR",
                ENDPOINT,
                df=1300 + index,
                decode_second=15 * (index + 1),
            ),
            NOW + timedelta(seconds=5 + index),
            ENDPOINT,
        )

    assert len(transport.sent) == sent_before + 3
    assert app.finalization.active
    assert not app.finalization.can_retry()
    app.handle(
        runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1400, decode_second=55),
        NOW + timedelta(seconds=9),
        ENDPOINT,
    )
    assert len(transport.sent) == sent_before + 3


def test_old_final_retry_does_not_interrupt_new_active_qso() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
    old_finalization = app.finalization.state
    assert old_finalization is not None
    cq = parse_ft8_message("CQ R3KLE KO91")
    report = parse_ft8_message("F4NJU R3KLE -08")
    assert cq is not None and report is not None
    app.engine.state.start_station(
        DecodeEvent(cq, NOW + timedelta(seconds=5), "FT8", -5),
        NOW + timedelta(seconds=5),
    )
    app.engine.state.observe(
        DecodeEvent(report, NOW + timedelta(seconds=6), "FT8", -8),
    )
    sent_before = len(transport.sent)

    app.handle(
        runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1500, decode_second=15),
        NOW + timedelta(seconds=7),
        ENDPOINT,
    )

    assert len(transport.sent) == sent_before
    assert app.engine.state.session.remote_callsign == "R3KLE"
    assert app.engine.state.session.state is QsoState.QSO_ACTIVE


def test_final_retry_does_not_duplicate_worked_today(tmp_path: Path) -> None:
    transport = FakeTransport()
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        config = replace(
            AppConfig(),
            candidate_collection_seconds=0,
            max_initiation_attempts=3,
            wsjtx_direct_reply_patched=True,
        )
        app = AutopilotRuntime(
            config,
            control=WsjtxUdpControl(transport, 15, 3, local_callsign="F4NJU", direct_reply_patched=True),
            worked_service=service,
        )
        app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
        start_finalization(app)
        app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)
        assert service.count(NOW.date()) == 1

        app.handle(
            runtime_decode("F4NJU UI6O RRR", ENDPOINT, df=1600, decode_second=15),
            NOW + timedelta(seconds=5),
            ENDPOINT,
        )

        assert service.count(NOW.date()) == 1
        assert app.control.actions_used == 1


def test_mode_change_closes_finalization_and_new_runtime_starts_empty() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    changed = replace(runtime_status("UI6O", tx_enabled=False), mode="FT4")

    app.handle(changed, NOW + timedelta(seconds=4), ENDPOINT)

    assert not app.finalization.active
    fresh = control_runtime(FakeTransport(), max_actions=3, direct_reply_patched=True)
    assert not fresh.finalization.active
