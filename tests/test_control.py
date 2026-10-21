import socket
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.adaptive import AdaptiveState, AttemptOutcome
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, CooldownKind, DecodeEvent, IntendedAction, OriginalDecode
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.main import build_control
from wsjtx_autopilot.runtime import AutopilotRuntime, HaltTxResult
from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    HaltTxPacket,
    PacketHeader,
    QsoLoggedPacket,
    ReplyPacket,
    SetDialFrequencyPacket,
    SetTxPeriodPacket,
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


def test_rearm_clears_stale_swr_safety_notice() -> None:
    control = WsjtxUdpControl(FakeTransport(), 15, None)
    config = replace(
        AppConfig(),
        ftx1_hamlib_enabled=False,
        ftx1_alc_control_enabled=False,
        ftx1_swr_protection_enabled=False,
    )
    app = AutopilotRuntime(config, control=control)
    app.last_qso_notice = "SWR SAFETY 2.50 - AutoPilot disarmed"
    app.antenna_safety_level = "fault"
    app._hamlib_swr_fault_latched = True

    assert app.arm_control()
    assert app.last_qso_notice == ""
    assert app.antenna_safety_level == "ok"
    assert app.antenna_safety_events[-1].endswith("Protection SWR réarmée par l'opérateur")


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


def test_preserves_exact_decode_fields_in_reply(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    decode = original(schema=3)
    adapter = armed_adapter(transport, decode)
    caplog.set_level("INFO")

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
    assert reply.modifiers == 0
    assert "Reply payload kind=CQ_REPLY station=OH2ZZ message='CQ OH2ZZ KP20'" in caplog.text


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


def test_ap1_heartbeat_automatically_enables_direct_reply() -> None:
    transport = FakeTransport()
    decode = original("F4NJU ON4ABC -08", schema=3)
    adapter = WsjtxUdpControl(
        transport,
        stale_seconds=15,
        max_actions=2,
        local_callsign="F4NJU",
        direct_reply_patched=False,
    )
    adapter.observe(
        HeartbeatPacket(PacketHeader(3, 1, "WSJT-X"), 3, "3.2.0", "260818-AP1"),
        ENDPOINT,
    )
    adapter.observe(packet(decode), ENDPOINT)

    outcome = adapter.execute(action(decode, ActionKind.DIRECT_REPLY, "ON4ABC"), NOW)

    assert outcome is ActionOutcome.SENT


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


def test_halt_tx_remains_available_after_action_limit_without_consuming_action(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    decode = original()
    adapter = armed_adapter(transport, decode, max_actions=1)
    assert adapter.execute(action(decode), NOW) is ActionOutcome.SENT
    assert not adapter.armed

    assert adapter.halt_tx("WSJT-X", "QSO stalled")

    assert adapter.actions_used == 1
    assert len(transport.sent) == 2
    assert isinstance(parse_datagram(transport.sent[1][0]), HaltTxPacket)
    assert "DISARM reason=action_limit_reached source=WsjtxUdpControl.execute" in caplog.text


def test_manual_disarm_logs_nonempty_reason_and_source(caplog: pytest.LogCaptureFixture) -> None:
    adapter = WsjtxUdpControl(FakeTransport(), 15, 1)

    adapter.disarm("user_request", source="test_manual_disarm")

    assert not adapter.armed
    assert "DISARM reason=user_request source=test_manual_disarm" in caplog.text


def test_ap1_controls_require_ap1_heartbeat_and_preserve_instance_session() -> None:
    transport = FakeTransport()
    adapter = WsjtxUdpControl(transport, 15, 2)
    heartbeat = HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1")
    adapter.observe(heartbeat, ENDPOINT)

    assert adapter.set_tx_period("WSJT-X", False)
    assert isinstance(parse_datagram(transport.sent[-1][0]), SetTxPeriodPacket)
    assert adapter.set_dial_frequency("WSJT-X", 18_100_000)
    dial = parse_datagram(transport.sent[-1][0])
    assert isinstance(dial, SetDialFrequencyPacket)
    assert dial.header.instance_id == "WSJT-X"
    assert dial.frequency_hz == 18_100_000


def test_ap1_controls_are_rejected_without_ap1_heartbeat() -> None:
    transport = FakeTransport()
    adapter = WsjtxUdpControl(transport, 15, 2)
    adapter.observe(HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "r123"), ENDPOINT)

    assert not adapter.set_tx_period("WSJT-X", True)
    assert not adapter.set_dial_frequency("WSJT-X", 18_100_000)
    assert transport.sent == []


def test_adaptive_parity_flip_requires_ap1_and_safe_idle() -> None:
    transport = FakeTransport()
    config = replace(AppConfig(), candidate_collection_seconds=0, adaptive_opportunity_quiet_seconds=5)
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3))
    app.handle(HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1"), NOW, ENDPOINT)
    app.handle(runtime_status("", schema=3, tx_enabled=True, transmitting=True), NOW, ENDPOINT)
    app.current_tx_first = True
    for index in range(6):
        app.note_adaptive_failure("DL1ABC", AttemptOutcome.NO_RESPONSE, NOW + timedelta(seconds=index))

    app.handle(None, NOW + timedelta(seconds=7), ENDPOINT)
    assert app.adaptive_state is AdaptiveState.NORMAL  # TX active; failure count is not the trigger.
    assert len(transport.sent) == 2  # Initial AP1 query plus retry while state is unavailable.

    app.handle(runtime_status("", schema=3, tx_enabled=False), NOW + timedelta(seconds=8), ENDPOINT)
    assert app.adaptive_state is AdaptiveState.PARITY_TRIAL
    period = parse_datagram(transport.sent[-1][0])
    assert isinstance(period, SetTxPeriodPacket)
    assert not period.tx_first


def test_adaptive_band_hop_requires_status_confirmation() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        automatic_band_hopping_enabled=True,
        allowed_auto_hop_bands=("20m", "17m"),
        minimum_band_dwell_minutes=0,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3))
    app.handle(HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1"), NOW, ENDPOINT)
    app.handle(runtime_status("", schema=3, tx_enabled=False), NOW, ENDPOINT)
    app.adaptive_state = AdaptiveState.BAND_HOP_PENDING

    app.handle(None, NOW + timedelta(seconds=1), ENDPOINT)
    command = parse_datagram(transport.sent[-1][0])
    assert isinstance(command, SetDialFrequencyPacket)
    assert command.frequency_hz == 18_100_000
    assert app.adaptive_state is AdaptiveState.BAND_CHANGING

    app.handle(
        runtime_status("", schema=3, frequency=18_100_000, tx_enabled=False),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    )
    assert app.adaptive_state is AdaptiveState.BAND_TRIAL


def test_band_trial_can_request_another_hop_when_no_viable_cq_appears() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        automatic_band_hopping_enabled=True,
        allowed_auto_hop_bands=("20m", "17m"),
        minimum_band_dwell_minutes=0,
        adaptive_parity_enabled=False,
        adaptive_opportunity_quiet_seconds=10,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3))
    app.handle(HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1"), NOW, ENDPOINT)
    app.handle(runtime_status("", schema=3, tx_enabled=False), NOW, ENDPOINT)
    app.adaptive_state = AdaptiveState.BAND_HOP_PENDING
    app.handle(None, NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(
        runtime_status("", schema=3, frequency=18_100_000, tx_enabled=False),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    )
    assert app.adaptive_state is AdaptiveState.BAND_TRIAL
    assert app.stagnation.failed_attempts == 0

    app.handle(None, NOW + timedelta(seconds=20), ENDPOINT)
    command = parse_datagram(transport.sent[-1][0])
    assert isinstance(command, SetDialFrequencyPacket)
    assert command.frequency_hz == 14_074_000


def test_worked_and_unreachable_cqs_exhaust_current_band_without_failure_threshold() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        adaptive_parity_enabled=False,
        automatic_band_hopping_enabled=True,
        allowed_auto_hop_bands=("20m", "17m"),
        minimum_band_dwell_minutes=0,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3))
    app.handle(HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1"), NOW, ENDPOINT)
    app.handle(runtime_status("", schema=3, tx_enabled=False), NOW, ENDPOINT)
    app.opportunities.observe("DL1ABC", NOW, worked=True)
    app.opportunities.observe("XQ3SK", NOW)
    app.note_adaptive_failure("XQ3SK", AttemptOutcome.NO_RESPONSE, NOW + timedelta(seconds=1))

    assert app.stagnation.failed_attempts == 1
    app.handle(None, NOW + timedelta(seconds=2), ENDPOINT)
    command = parse_datagram(transport.sent[-1][0])
    assert isinstance(command, SetDialFrequencyPacket)
    assert command.frequency_hz == 18_100_000


def test_adaptive_snapshot_exposes_viable_count_and_quiet_countdown() -> None:
    app = control_runtime(FakeTransport())
    app.config = replace(app.config, adaptive_opportunity_quiet_seconds=180)
    app.opportunities.reset("20m", "FT8", NOW)
    app.opportunities.observe("XQ3SK", NOW + timedelta(seconds=10))
    app._last_runtime_at = NOW + timedelta(seconds=70)

    opportunities = app.adaptive_snapshot()["opportunities"]

    assert opportunities["viable_count"] == 1
    assert opportunities["quiet_elapsed_seconds"] == 60
    assert opportunities["quiet_threshold_seconds"] == 180
    assert opportunities["quiet_remaining_seconds"] == 120
    assert opportunities["exhausted"] is False


def test_inactive_unanswered_qso_counts_once_for_stagnation() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(
        runtime_status("OH2ZZ", tx_enabled=True, transmitting=True, tx_message="OH2ZZ F4NJU -08"),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )
    assert app.engine.state.session.reply_confirmed

    app.handle(runtime_status("", tx_enabled=False), NOW + timedelta(seconds=122), ENDPOINT)
    assert app.engine.state.session.state is QsoState.IDLE
    assert app.stagnation.failed_attempts == 1
    assert app.stagnation.snapshot()[0]["outcome"] == "NO_RESPONSE"
    app.handle(None, NOW + timedelta(seconds=123), ENDPOINT)
    assert app.stagnation.failed_attempts == 1


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
    schema: int = 2,
    is_new: bool = True,
    low_confidence: bool = False,
    off_air: bool = False,
    df: int = 1460,
    decode_second: int = 0,
    snr: int = -8,
) -> DecodePacket:
    return DecodePacket(
        PacketHeader(schema, 2, "WSJT-X"),
        is_new,
        time(12, 0, decode_second),
        snr,
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
    schema: int = 2,
    frequency: int = 14_074_000,
    tx_enabled: bool,
    transmitting: bool = False,
    tx_message: str = "",
    tx_df: int = 900,
    tx_watchdog: bool = False,
) -> StatusPacket:
    return StatusPacket(
        PacketHeader(schema, 1, "WSJT-X"),
        frequency,
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
        tx_watchdog,
        "",
        False,
        0,
        0xFFFFFFFF,
        15,
        "Default",
        tx_message,
    )


def qso_logged(station: str, instance_id: str = "WSJT-X", schema: int = 2) -> QsoLoggedPacket:
    return QsoLoggedPacket(
        PacketHeader(schema, 5, instance_id),
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
    stale_seconds: float | None = None,
) -> AutopilotRuntime:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=max_actions,
        wsjtx_direct_reply_patched=direct_reply_patched,
        **({"stale_decode_seconds": stale_seconds} if stale_seconds is not None else {}),
    )
    control = WsjtxUdpControl(
        transport,
        config.stale_decode_seconds,
        max_actions,
        local_callsign="F4NJU",
        direct_reply_patched=direct_reply_patched,
    )
    return AutopilotRuntime(config, control=control)


def waiting_final_tx(app: AutopilotRuntime, station: str = "RC9WC") -> None:
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode(f"CQ {station} NO15", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode(f"F4NJU {station} -08", ENDPOINT, decode_second=1), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode(f"F4NJU {station} RR73", ENDPOINT, decode_second=2), NOW + timedelta(seconds=2), ENDPOINT)
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX


def complete_waiting_qso(app: AutopilotRuntime, station: str = "RC9WC", *, at: datetime) -> IntendedAction | None:
    assert app.handle(
        runtime_status(station, tx_enabled=True, transmitting=True, tx_message=f"{station} F4NJU 73"),
        at,
        ENDPOINT,
    ) is None
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    assert app.finalization.state is not None
    assert app.finalization.state.tx_confirmed
    assert not app.finalization.state.tx_completed
    assert app.handle(qso_logged(station), at + timedelta(seconds=1), ENDPOINT) is None
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    assert app.handle(
        runtime_status(station, tx_enabled=False),
        at + timedelta(seconds=14),
        ENDPOINT,
    ) is None
    assert app.engine.state.session.state is QsoState.IDLE
    return app.handle(None, at + timedelta(seconds=15), ENDPOINT)


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


def test_cq_reply_is_sent_directly_without_set_tx_df() -> None:
    transport = FakeTransport()
    app = AutopilotRuntime(
        replace(AppConfig(), candidate_collection_seconds=0),
        control=WsjtxUdpControl(transport, 15, 2),
    )

    action = app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT, df=895), NOW, ENDPOINT)

    assert action is not None
    assert len(transport.sent) == 1
    assert isinstance(parse_datagram(transport.sent[0][0]), ReplyPacket)


def test_direct_reply_is_sent_directly_without_set_tx_df() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, direct_reply_patched=True)

    action = app.handle(runtime_decode("F4NJU ON4ABC -08", ENDPOINT, df=2062), NOW, ENDPOINT)

    assert action is not None and action.kind is ActionKind.DIRECT_REPLY
    assert len(transport.sent) == 1
    assert isinstance(parse_datagram(transport.sent[0][0]), ReplyPacket)


def test_tx_df_changes_do_not_disarm_control() -> None:
    transport = FakeTransport()
    app = control_runtime(transport)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)

    app.handle(runtime_status("OH2ZZ", tx_enabled=True, tx_df=895), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_status("OH2ZZ", tx_enabled=True, tx_df=1700), NOW + timedelta(seconds=2), ENDPOINT)

    assert app.control.armed  # type: ignore[attr-defined]


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


def test_low_confidence_direct_is_hard_blocked_but_same_window_cq_is_sent() -> None:
    transport = FakeTransport()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0.25,
        allow_dupes=True,
        wsjtx_direct_reply_patched=True,
    )
    control = WsjtxUdpControl(
        transport,
        15,
        2,
        local_callsign="F4NJU",
        direct_reply_patched=True,
    )
    app = AutopilotRuntime(config, control=control)
    app.handle(
        runtime_decode("F4NJU YO2LFP -13", ENDPOINT, low_confidence=True, df=1200),
        NOW,
        ENDPOINT,
    )
    app.handle(runtime_decode("CQ ES3BH KO28", ENDPOINT, df=1400), NOW, ENDPOINT)

    action = app.handle(None, NOW + timedelta(milliseconds=250), ENDPOINT)

    assert action is not None and action.station == "ES3BH"
    reply = parse_datagram(transport.sent[0][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.message == "CQ ES3BH KO28"


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
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    app.handle(
        runtime_status("OH2ZZ", tx_enabled=True, transmitting=True, tx_message="OH2ZZ F4NJU 73"),
        NOW + timedelta(seconds=11),
        ENDPOINT,
    )
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    assert app.finalization.state is not None
    assert app.finalization.state.tx_confirmed
    assert not app.finalization.state.tx_completed
    app.handle(runtime_status("OH2ZZ", tx_enabled=False), NOW + timedelta(seconds=13), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert app.control.actions_used == 1


def test_normal_qso_completion_rescans_queued_cq_once(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, stale_seconds=60)
    caplog.set_level("INFO")
    waiting_final_tx(app)
    sent_before = len(transport.sent)

    assert app.handle(runtime_decode("CQ RY6F LN05", ENDPOINT, df=1200), NOW + timedelta(seconds=3), ENDPOINT) is None
    assert len(transport.sent) == sent_before

    action = complete_waiting_qso(app, at=NOW + timedelta(seconds=4))

    assert action is not None and action.station == "RY6F"
    assert action.kind is ActionKind.CQ_REPLY
    assert len(transport.sent) == sent_before + 1
    assert app.engine.state.session.state is QsoState.CALLING_STATION
    assert "[ENGINE] QSO complete; rescanning queued candidates count=1" in caplog.text
    assert "[ENGINE] post-QSO candidate selected station=RY6F" in caplog.text


def test_post_qso_rescan_selects_highest_scored_queued_candidate() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, stale_seconds=60)
    waiting_final_tx(app)
    app.handle(runtime_decode("CQ A6AAA LL75", ENDPOINT, df=1200, snr=-20), NOW + timedelta(seconds=3), ENDPOINT)
    app.handle(runtime_decode("CQ B7BBB OL63", ENDPOINT, df=1201, snr=10), NOW + timedelta(seconds=4), ENDPOINT)

    action = complete_waiting_qso(app, at=NOW + timedelta(seconds=5))

    assert action is not None and action.station == "B7BBB"
    assert action.kind is ActionKind.CQ_REPLY
    assert len(transport.sent) == 2


def test_post_qso_rescan_rejects_stale_queued_candidate() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, stale_seconds=5)
    waiting_final_tx(app)
    app.handle(runtime_decode("CQ RY6F LN05", ENDPOINT, df=1200), NOW + timedelta(seconds=3), ENDPOINT)

    action = complete_waiting_qso(app, at=NOW + timedelta(seconds=10))

    assert action is None
    assert len(transport.sent) == 1
    assert app.engine.state.session.state is QsoState.IDLE


def test_post_qso_rescan_rejects_candidate_that_enters_cooldown() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, stale_seconds=60)
    waiting_final_tx(app)
    app.handle(runtime_decode("CQ RY6F LN05", ENDPOINT, df=1200), NOW + timedelta(seconds=3), ENDPOINT)
    app.engine._set_station_cooldown(
        "RY6F",
        NOW + timedelta(seconds=3),
        60,
        CooldownKind.STATION_RETRY,
        "test cooldown",
    )

    action = complete_waiting_qso(app, at=NOW + timedelta(seconds=4))

    assert action is None
    assert len(transport.sent) == 1
    assert app.engine.state.session.state is QsoState.IDLE


def test_post_qso_rescan_does_not_duplicate_action_when_direct_call_arrives() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True, stale_seconds=60)
    waiting_final_tx(app)
    app.handle(runtime_decode("CQ RY6F LN05", ENDPOINT, df=1200), NOW + timedelta(seconds=3), ENDPOINT)

    action = complete_waiting_qso(app, at=NOW + timedelta(seconds=4))
    racing_action = app.handle(
        runtime_decode("F4NJU OZ2LC -10", ENDPOINT, df=1300, decode_second=5),
        NOW + timedelta(seconds=20),
        ENDPOINT,
    )

    assert action is not None and action.station == "RY6F"
    assert racing_action is None
    assert len(transport.sent) == 2
    assert app.engine.state.session.remote_callsign == "RY6F"


def test_normal_qso_completion_without_queued_candidates_is_unchanged() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    waiting_final_tx(app)

    action = complete_waiting_qso(app, at=NOW + timedelta(seconds=3))

    assert action is None
    assert len(transport.sent) == 1
    assert app.engine.state.session.state is QsoState.IDLE


def test_qso_logged_during_waiting_final_tx_blocks_cq_and_queues_direct_call() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_decode("CQ A6IOK LL75", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU A6IOK R-05", ENDPOINT), NOW + timedelta(seconds=1), ENDPOINT)
    assert app.engine.state.session.state is QsoState.QSO_ACTIVE

    app.handle(runtime_decode("F4NJU A6IOK RR73", ENDPOINT), NOW + timedelta(seconds=2), ENDPOINT)
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    app.handle(qso_logged("A6IOK"), NOW + timedelta(seconds=3), ENDPOINT)
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    assert app.finalization.state is not None and app.finalization.state.qso_logged

    sent_before = len(transport.sent)
    assert app.handle(runtime_decode("CQ SP9MOC JO90", ENDPOINT, df=1300), NOW + timedelta(seconds=4), ENDPOINT) is None
    assert app.handle(runtime_decode("F4NJU S51DD +01", ENDPOINT, df=1400), NOW + timedelta(seconds=5), ENDPOINT) is None
    assert len(transport.sent) == sent_before
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=5))) == 1
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX


def test_stale_pending_direct_waits_for_refresh_before_cq() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ A6IOK LL75", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU A6IOK R-05", ENDPOINT), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode("F4NJU A6IOK RR73", ENDPOINT), NOW + timedelta(seconds=2), ENDPOINT)
    app.handle(qso_logged("A6IOK"), NOW + timedelta(seconds=3), ENDPOINT)
    app.handle(runtime_decode("F4NJU S51DD +01", ENDPOINT, df=1400), NOW + timedelta(seconds=4), ENDPOINT)
    app.handle(runtime_decode("CQ SP9MOC JO90", ENDPOINT, df=1300), NOW + timedelta(seconds=5), ENDPOINT)

    app.handle(
        runtime_status("A6IOK", tx_enabled=True, transmitting=True, tx_message="A6IOK F4NJU 73"),
        NOW + timedelta(seconds=6),
        ENDPOINT,
    )
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    app.handle(runtime_status("A6IOK", tx_enabled=False), NOW + timedelta(seconds=9), ENDPOINT)
    assert app.engine.state.session.state is QsoState.IDLE

    assert app.handle(None, NOW + timedelta(seconds=21), ENDPOINT) is None
    pending = app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=21))
    assert len(pending) == 1
    assert pending[0].waiting_for_refresh

    action = app.handle(
        runtime_decode("F4NJU S51DD +01", ENDPOINT, df=1450, decode_second=15),
        NOW + timedelta(seconds=22),
        ENDPOINT,
    )
    assert action is not None
    assert action.station == "S51DD"
    assert action.kind is ActionKind.DIRECT_REPLY
    reply = parse_datagram(transport.sent[-1][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.delta_frequency == 1450


def test_direct_finalization_refresh_uses_exact_fresh_reply_payload(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    caplog.set_level("INFO")
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ DK8QH JN49", ENDPOINT, df=900), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU DK8QH R-05", ENDPOINT, df=1000), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode("F4NJU DK8QH RR73", ENDPOINT, df=1100), NOW + timedelta(seconds=2), ENDPOINT)
    assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
    app.handle(qso_logged("DK8QH"), NOW + timedelta(seconds=3), ENDPOINT)
    app.handle(runtime_decode("F4NJU OZ2LC -08", ENDPOINT, df=556), NOW + timedelta(seconds=4), ENDPOINT)
    app.handle(runtime_decode("CQ DL6NDN JO42", ENDPOINT, df=1600), NOW + timedelta(seconds=5), ENDPOINT)

    app.handle(
        runtime_status("DK8QH", tx_enabled=True, transmitting=True, tx_message="DK8QH F4NJU 73"),
        NOW + timedelta(seconds=6),
        ENDPOINT,
    )
    app.handle(runtime_status("DK8QH", tx_enabled=False), NOW + timedelta(seconds=9), ENDPOINT)
    assert app.engine.state.session.state is QsoState.IDLE

    assert app.handle(None, NOW + timedelta(seconds=21), ENDPOINT) is None
    assert "[DIRECT] WAITING_DIRECT_REFRESH station=OZ2LC" in caplog.text
    assert app.handle(runtime_decode("CQ DL6NDN JO42", ENDPOINT, df=1601, decode_second=15), NOW + timedelta(seconds=22), ENDPOINT) is None

    action = app.handle(
        runtime_decode("F4NJU OZ2LC -08", ENDPOINT, df=556, decode_second=15),
        NOW + timedelta(seconds=23),
        ENDPOINT,
    )
    assert action is not None
    assert action.station == "OZ2LC"
    assert action.kind is ActionKind.DIRECT_REPLY
    reply = parse_datagram(transport.sent[-1][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.decode_time == time(12, 0, 15)
    assert reply.snr == -8
    assert reply.delta_time == 0.2
    assert reply.delta_frequency == 556
    assert reply.mode == "~"
    assert reply.message == "F4NJU OZ2LC -08"
    assert not reply.low_confidence
    assert reply.modifiers == 0
    assert "Reply payload kind=DIRECT_REPLY station=OZ2LC message='F4NJU OZ2LC -08'" in caplog.text


def test_direct_reply_timeout_keeps_direct_priority_until_refresh() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_decode("F4NJU OZ2LC -08", ENDPOINT, df=556), NOW, ENDPOINT)
    assert app.engine.state.session.state is QsoState.DIRECT_REPLY_SENT

    app.handle(None, NOW + timedelta(seconds=21), ENDPOINT)
    assert app.engine.state.session.state is QsoState.IDLE
    pending = app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=21))
    assert len(pending) == 1
    assert pending[0].station == "OZ2LC"
    assert pending[0].waiting_for_refresh

    assert app.handle(runtime_decode("CQ DL6NDN JO42", ENDPOINT, df=1600), NOW + timedelta(seconds=22), ENDPOINT) is None
    action = app.handle(
        runtime_decode("F4NJU OZ2LC -08", ENDPOINT, df=556, decode_second=15),
        NOW + timedelta(seconds=23),
        ENDPOINT,
    )
    assert action is not None and action.station == "OZ2LC"


def test_expired_pending_direct_allows_normal_cq_selection() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ DK8QH JN49", ENDPOINT, df=900), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU DK8QH R-05", ENDPOINT, df=1000), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode("F4NJU OZ2LC -08", ENDPOINT, df=556), NOW + timedelta(seconds=2), ENDPOINT)
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=2))) == 1

    app.engine.abort_qso("test completion")
    assert app.handle(None, NOW + timedelta(seconds=18), ENDPOINT) is None
    assert app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=33)) == ()

    action = app.handle(runtime_decode("CQ DL6NDN JO42", ENDPOINT, df=1600), NOW + timedelta(seconds=34), ENDPOINT)
    assert action is not None
    assert action.station == "DL6NDN"


def test_final_tx_timeout_preserves_single_worked_qso_after_qso_logged(tmp_path: Path) -> None:
    transport = FakeTransport()
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        config = replace(AppConfig(), candidate_collection_seconds=0, max_initiation_attempts=3)
        app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3), worked_service=service)
        app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
        app.handle(runtime_decode("CQ A6IOK LL75", ENDPOINT), NOW, ENDPOINT)
        app.handle(runtime_decode("F4NJU A6IOK R-05", ENDPOINT), NOW + timedelta(seconds=1), ENDPOINT)
        app.handle(runtime_decode("F4NJU A6IOK RR73", ENDPOINT), NOW + timedelta(seconds=2), ENDPOINT)
        app.handle(qso_logged("A6IOK"), NOW + timedelta(seconds=3), ENDPOINT)
        app.handle(qso_logged("A6IOK"), NOW + timedelta(seconds=4), ENDPOINT)
        assert app.engine.state.session.state is QsoState.WAITING_FINAL_TX
        assert service.count(NOW.date()) == 1

        app.handle(None, NOW + timedelta(seconds=32), ENDPOINT)

        assert not app.finalization.active
        assert app.engine.state.session.state is QsoState.IDLE
        assert service.count(NOW.date()) == 1


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


def test_wsjtx_runaway_watchdog_aborts_immediately_and_uses_waiting_candidate(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ R3KLE KO91", ENDPOINT, df=1202), NOW + timedelta(seconds=1), ENDPOINT)

    action = app.handle(
        runtime_status("OH2ZZ", tx_enabled=False, tx_watchdog=True),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    )

    assert action is not None
    assert action.station == "R3KLE"
    assert app.engine.state.session.remote_callsign == "R3KLE"
    cooldown = app.engine.cooldown_for("OH2ZZ", NOW + timedelta(seconds=2))
    assert cooldown is not None
    assert cooldown.kind is CooldownKind.STALLED_QSO
    assert "WSJT-X Runaway TX watchdog remote=OH2ZZ" in caplog.text


def test_wsjtx_watchdog_waits_for_rx_confirmation_before_disarming(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    monkeypatch.setattr(app, "_request_halt_tx", lambda *_args: HaltTxResult(False, True, None))

    action = app.handle(
        runtime_status(
            "OH2ZZ",
            tx_enabled=True,
            transmitting=True,
            tx_message="OH2ZZ F4NJU -08",
            tx_watchdog=True,
        ),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )

    assert action is None
    assert app.control.armed
    assert app.engine.state.session.state is QsoState.IDLE
    assert app._pending_watchdog_halt is not None

    app.handle(
        runtime_status("", tx_enabled=False, transmitting=False),
        NOW + timedelta(seconds=1, milliseconds=100),
        ENDPOINT,
    )

    assert app.control.armed
    assert app._pending_watchdog_halt is None
    assert not app._watchdog_halt_failed


def test_wsjtx_watchdog_disarms_if_tx_remains_active_after_confirmation_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    monkeypatch.setattr(app, "_request_halt_tx", lambda *_args: HaltTxResult(False, True, None))
    app.handle(
        runtime_status(
            "OH2ZZ",
            tx_enabled=True,
            transmitting=True,
            tx_message="OH2ZZ F4NJU -08",
            tx_watchdog=True,
        ),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )

    app.handle(None, NOW + timedelta(seconds=3, milliseconds=100), ENDPOINT)

    assert not app.control.armed
    assert app._pending_watchdog_halt is None
    assert app._watchdog_halt_failed
    assert "TX still active" in app.last_qso_notice


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


def test_remote_busy_waits_for_wsjtx_rx_when_hamlib_stop_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(
        runtime_status(
            "OH2ZZ",
            tx_enabled=True,
            transmitting=True,
            tx_message="OH2ZZ F4NJU -08",
        ),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )
    monkeypatch.setattr(app, "_request_halt_tx", lambda *_args: HaltTxResult(False, True, None))

    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=2), ENDPOINT)

    assert app.control.armed
    assert app.engine.state.session.state is QsoState.IDLE
    assert app._pending_watchdog_halt is not None

    app.handle(
        runtime_status("", tx_enabled=False, transmitting=False),
        NOW + timedelta(seconds=2, milliseconds=200),
        ENDPOINT,
    )

    assert app.control.armed
    assert app._pending_watchdog_halt is None
    assert not app._watchdog_halt_failed


def test_remote_busy_disarms_only_if_tx_stays_active_after_confirmation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3)
    app.handle(runtime_decode("CQ OH2ZZ KP20", ENDPOINT), NOW, ENDPOINT)
    app.handle(
        runtime_status(
            "OH2ZZ",
            tx_enabled=True,
            transmitting=True,
            tx_message="OH2ZZ F4NJU -08",
        ),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )
    monkeypatch.setattr(app, "_request_halt_tx", lambda *_args: HaltTxResult(False, True, None))
    app.handle(runtime_decode("UT2UB OH2ZZ +13", ENDPOINT, df=1201), NOW + timedelta(seconds=2), ENDPOINT)

    app.handle(None, NOW + timedelta(seconds=4, milliseconds=100), ENDPOINT)

    assert not app.control.armed
    assert app._pending_watchdog_halt is None
    assert app._watchdog_halt_failed
    assert "TX still active" in app.last_qso_notice


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
    assert not app.finalization.state.tx_completed
    app.handle(
        runtime_status("UI6O", tx_enabled=False),
        NOW + timedelta(seconds=3, milliseconds=500),
        ENDPOINT,
    )
    assert app.finalization.state is not None and app.finalization.state.tx_completed


def test_qso_logged_cannot_replace_final_73_while_it_is_transmitting() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, stale_seconds=60)
    waiting_final_tx(app, "DL5WP")
    app.handle(runtime_decode("CQ F1IQH JN27", ENDPOINT, df=1079), NOW + timedelta(seconds=3), ENDPOINT)
    sent_before = len(transport.sent)

    assert app.handle(
        runtime_status("DL5WP", tx_enabled=True, transmitting=True, tx_message="DL5WP F4NJU 73", tx_df=795),
        NOW + timedelta(seconds=4),
        ENDPOINT,
    ) is None
    assert app.handle(qso_logged("DL5WP"), NOW + timedelta(seconds=5), ENDPOINT) is None
    assert app.handle(
        runtime_status("DL5WP", tx_enabled=False, transmitting=True, tx_message="DL5WP F4NJU 73", tx_df=795),
        NOW + timedelta(seconds=10),
        ENDPOINT,
    ) is None
    assert len(transport.sent) == sent_before
    assert app.engine.state.session.remote_callsign == "DL5WP"

    assert app.handle(
        runtime_status("DL5WP", tx_enabled=False, transmitting=False, tx_df=795),
        NOW + timedelta(seconds=18),
        ENDPOINT,
    ) is None
    assert len(transport.sent) == sent_before

    action = app.handle(None, NOW + timedelta(seconds=19), ENDPOINT)
    assert action is not None and action.station == "F1IQH"
    assert len(transport.sent) == sent_before + 1


def test_qso_logged_during_rr73_tx_cannot_start_queued_cq() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, stale_seconds=60)
    app.handle(runtime_decode("CQ SQ4FXT KO03", ENDPOINT, df=754), NOW, ENDPOINT)
    app.handle(
        runtime_decode("F4NJU SQ4FXT R-13", ENDPOINT, df=754, decode_second=1),
        NOW + timedelta(seconds=1), ENDPOINT,
    )
    app.handle(runtime_decode("CQ R5DT KO94", ENDPOINT, df=1307), NOW + timedelta(seconds=2), ENDPOINT)
    sent_before = len(transport.sent)
    app.handle(
        runtime_status(
            "SQ4FXT",
            tx_enabled=False,
            transmitting=True,
            tx_message="SQ4FXT F4NJU RR73",
            tx_df=754,
        ),
        NOW + timedelta(seconds=3), ENDPOINT,
    )

    assert app.handle(qso_logged("SQ4FXT"), NOW + timedelta(seconds=4), ENDPOINT) is None
    assert len(transport.sent) == sent_before
    assert app.engine.state.session.remote_callsign == "SQ4FXT"
    assert app.finalization.state is not None and app.finalization.state.qso_logged

    assert app.handle(
        runtime_status("SQ4FXT", tx_enabled=False, transmitting=False, tx_df=754),
        NOW + timedelta(seconds=15), ENDPOINT,
    ) is None
    assert len(transport.sent) == sent_before

    action = app.handle(None, NOW + timedelta(seconds=31), ENDPOINT)
    assert action is not None and action.station == "R5DT"


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


def test_pending_direct_after_active_qso_beats_cq_and_replies_with_latest_decode() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_decode("CQ R5DT KO85", ENDPOINT, schema=3, df=900), NOW, ENDPOINT)
    app.handle(
        runtime_decode("F4NJU R5DT -10", ENDPOINT, schema=3, df=1000),
        NOW + timedelta(seconds=1),
        ENDPOINT,
    )
    assert app.engine.state.session.state is QsoState.QSO_ACTIVE

    app.handle(
        runtime_decode("F4NJU S51DD +01", ENDPOINT, schema=3, df=1200),
        NOW + timedelta(seconds=2),
        ENDPOINT,
    )
    app.handle(
        runtime_decode("F4NJU S51DD +01", ENDPOINT, schema=3, df=1450),
        NOW + timedelta(seconds=3),
        ENDPOINT,
    )
    pending = app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=3))
    assert len(pending) == 1
    assert pending[0].repeat_count == 2
    assert pending[0].event.original is not None
    assert pending[0].event.original.delta_frequency == 1450
    assert app.engine.state.session.remote_callsign == "R5DT"

    action = app.handle(qso_logged("R5DT", schema=3), NOW + timedelta(seconds=4), ENDPOINT)
    assert action is not None
    assert action.station == "S51DD"
    assert action.reason == "PENDING_DIRECT_CALL"
    assert action.original_decode is not None
    assert action.original_decode.delta_frequency == 1450
    reply = parse_datagram(transport.sent[-1][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.header.schema == 3
    assert reply.message == "F4NJU S51DD +01"

    assert app.handle(runtime_decode("CQ SP9MOC JO90", ENDPOINT, df=1600), NOW + timedelta(seconds=5), ENDPOINT) is None


def test_direct_caller_preempts_unanswered_cq_attempt_before_transmission() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ R3RRC/P KO88", ENDPOINT, df=2191), NOW, ENDPOINT)
    app.handle(
        runtime_status("R3RRC/P", tx_enabled=True, transmitting=False, tx_message="R3RRC/P F4NJU -08"),
        NOW + timedelta(seconds=1), ENDPOINT,
    )

    action = app.handle(
        runtime_decode("F4NJU F6HRP +00", ENDPOINT, df=569, decode_second=2, snr=9),
        NOW + timedelta(seconds=2), ENDPOINT,
    )

    assert action is not None and action.station == "F6HRP"
    assert action.kind is ActionKind.DIRECT_REPLY
    assert [type(parse_datagram(data)).__name__ for data, _ in transport.sent] == [
        "ReplyPacket", "HaltTxPacket", "ReplyPacket",
    ]
    assert app.engine.state.session.remote_callsign == "F6HRP"
    assert app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=2)) == ()


def test_dual_rr73_direct_caller_preempts_unanswered_cq_attempt() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ 9A4ZM JN86", ENDPOINT, df=2536), NOW, ENDPOINT)
    app.handle(
        runtime_status("9A4ZM", tx_enabled=True, transmitting=False, tx_message="9A4ZM F4NJU JN29"),
        NOW + timedelta(seconds=1), ENDPOINT,
    )

    action = app.handle(
        runtime_decode(
            "F6BQX RR73; F4NJU <ES0RY> -16",
            ENDPOINT,
            df=460,
            decode_second=2,
            snr=-11,
        ),
        NOW + timedelta(seconds=2), ENDPOINT,
    )

    assert action is not None and action.station == "ES0RY"
    assert action.kind is ActionKind.DIRECT_REPLY
    assert [type(parse_datagram(data)).__name__ for data, _ in transport.sent] == [
        "ReplyPacket", "HaltTxPacket", "ReplyPacket",
    ]
    reply = parse_datagram(transport.sent[-1][0])
    assert isinstance(reply, ReplyPacket)
    assert reply.message == "F6BQX RR73; F4NJU <ES0RY> -16"
    assert app.engine.state.session.remote_callsign == "ES0RY"


def test_direct_caller_does_not_interrupt_current_transmission() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ R3RRC/P KO88", ENDPOINT, df=2191), NOW, ENDPOINT)
    app.handle(
        runtime_status("R3RRC/P", tx_enabled=True, transmitting=True, tx_message="R3RRC/P F4NJU -08"),
        NOW + timedelta(seconds=1), ENDPOINT,
    )

    assert app.handle(
        runtime_decode("F4NJU F6HRP +00", ENDPOINT, df=569, decode_second=2),
        NOW + timedelta(seconds=2), ENDPOINT,
    ) is None
    assert len(transport.sent) == 1
    assert app.engine.state.session.remote_callsign == "R3RRC/P"
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=2))) == 1


def test_failed_halt_keeps_old_attempt_and_disarms_before_direct_reply() -> None:
    class HaltFailingTransport(FakeTransport):
        def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
            if isinstance(parse_datagram(data), HaltTxPacket):
                raise OSError("simulated HaltTx failure")
            return super().sendto(data, endpoint)

    transport = HaltFailingTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ R3RRC/P KO88", ENDPOINT, df=2191), NOW, ENDPOINT)
    app.handle(
        runtime_status("R3RRC/P", tx_enabled=True, transmitting=False, tx_message="R3RRC/P F4NJU -08"),
        NOW + timedelta(seconds=1), ENDPOINT,
    )

    assert app.handle(
        runtime_decode("F4NJU F6HRP +00", ENDPOINT, df=569, decode_second=2),
        NOW + timedelta(seconds=2), ENDPOINT,
    ) is None
    assert not app.control.armed
    assert app.engine.state.session.remote_callsign == "R3RRC/P"
    assert len(transport.sent) == 1


def test_direct_caller_does_not_preempt_after_remote_has_replied() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_status("", tx_enabled=False), NOW, ENDPOINT)
    app.handle(runtime_decode("CQ R3RRC/P KO88", ENDPOINT, df=2191), NOW, ENDPOINT)
    app.handle(
        runtime_decode("F4NJU R3RRC/P -10", ENDPOINT, df=2191, decode_second=1),
        NOW + timedelta(seconds=1), ENDPOINT,
    )
    assert app.engine.state.session.state is QsoState.QSO_ACTIVE

    assert app.handle(
        runtime_decode("F4NJU F6HRP +00", ENDPOINT, df=569, decode_second=2),
        NOW + timedelta(seconds=2), ENDPOINT,
    ) is None
    assert len(transport.sent) == 1
    assert app.engine.state.session.remote_callsign == "R3RRC/P"


def test_finalization_hold_queues_direct_then_selects_it_before_cq() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    start_finalization(app)
    app.handle(qso_logged("UI6O"), NOW + timedelta(seconds=4), ENDPOINT)

    assert app.handle(
        runtime_decode("F4NJU S51DD +01", ENDPOINT, df=1203),
        NOW + timedelta(seconds=5),
        ENDPOINT,
    ) is None
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=5))) == 1
    assert app.handle(
        runtime_decode("CQ SP9MOC JO90", ENDPOINT, df=1400),
        NOW + timedelta(seconds=6),
        ENDPOINT,
    ) is None
    assert app.handle(None, NOW + timedelta(seconds=17), ENDPOINT) is None

    action = app.handle(None, NOW + timedelta(seconds=18), ENDPOINT)
    assert action is not None
    assert action.station == "S51DD"
    assert action.reason == "PENDING_DIRECT_CALL"


def test_clear_and_context_change_invalidate_pending_direct_calls() -> None:
    transport = FakeTransport()
    app = control_runtime(transport, max_actions=3, direct_reply_patched=True)
    app.handle(runtime_decode("CQ R5DT KO85", ENDPOINT), NOW, ENDPOINT)
    app.handle(runtime_decode("F4NJU R5DT -10", ENDPOINT), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(runtime_decode("F4NJU S51DD +01", ENDPOINT), NOW + timedelta(seconds=2), ENDPOINT)
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=2))) == 1

    app.handle(ClearPacket(PacketHeader(2, 3, "WSJT-X"), None), NOW + timedelta(seconds=3), ENDPOINT)
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=3))) == 0

    app.handle(
        runtime_decode("F4NJU S51DD +01", ENDPOINT, decode_second=15),
        NOW + timedelta(seconds=4),
        ENDPOINT,
    )
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=4))) == 1
    changed = replace(runtime_status("R5DT", tx_enabled=False), mode="FT4")
    app.handle(changed, NOW + timedelta(seconds=5), ENDPOINT)
    assert len(app.engine.pending_direct_calls.entries(NOW + timedelta(seconds=5))) == 0


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
