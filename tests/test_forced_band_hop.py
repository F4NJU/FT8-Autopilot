from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

import pytest

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.adaptive import AdaptiveState
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.models import DecodePacket, HeartbeatPacket, PacketHeader, ReplyPacket, SetDialFrequencyPacket, StatusPacket
from wsjtx_autopilot.wsjtx.protocol import parse_datagram

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
ENDPOINT = ("127.0.0.1", 2237)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        self.sent.append((data, endpoint))
        return len(data)


def heartbeat() -> HeartbeatPacket:
    return HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1")


def status(frequency: int, *, transmitting: bool = False) -> StatusPacket:
    return StatusPacket(
        PacketHeader(3, 1, "WSJT-X"),
        frequency,
        "FT8",
        "",
        "",
        "FT8",
        False,
        transmitting,
        False,
        900,
        900,
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
        "",
    )


def runtime(
    transport: FakeTransport,
    *,
    bands: tuple[str, ...] = ("20m", "30m", "40m"),
    enabled: bool = True,
    interval_minutes: float = 10,
    settle_seconds: float = 30,
    frequency: int = 14_074_000,
) -> AutopilotRuntime:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        qso_timeout_seconds=3_600,
        forced_band_hopping_enabled=enabled,
        forced_band_hop_bands=bands,
        forced_band_hop_interval_minutes=interval_minutes,
        forced_band_hop_settle_seconds=settle_seconds,
        allowed_auto_hop_bands=bands,
    )
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, None, armed=True))
    app.handle(heartbeat(), NOW, ENDPOINT)
    app.handle(status(frequency), NOW, ENDPOINT)
    transport.sent.clear()
    return app


def dial_command(transport: FakeTransport) -> SetDialFrequencyPacket:
    command = parse_datagram(transport.sent[-1][0])
    assert isinstance(command, SetDialFrequencyPacket)
    return command


def sent_packets(transport: FakeTransport) -> list[object]:
    return [parse_datagram(data) for data, _ in transport.sent]


def test_forced_hop_requests_20m_to_30m_when_dwell_expires() -> None:
    transport = FakeTransport()
    app = runtime(transport)

    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)

    assert dial_command(transport).frequency_hz == 10_136_000


def test_forced_hop_wraps_40m_to_20m() -> None:
    transport = FakeTransport()
    app = runtime(transport, frequency=7_074_000)

    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)

    assert dial_command(transport).frequency_hz == 14_074_000


def test_forced_hop_does_not_request_before_dwell_expires() -> None:
    transport = FakeTransport()
    app = runtime(transport)

    app.handle(None, NOW + timedelta(minutes=9, seconds=59), ENDPOINT)

    assert not any(isinstance(packet, SetDialFrequencyPacket) for packet in sent_packets(transport))


def test_expired_forced_dwell_requests_immediately_while_idle() -> None:
    transport = FakeTransport()
    app = runtime(transport)

    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)

    assert app.adaptive_state is AdaptiveState.BAND_CHANGING
    assert app._pending_dial_frequency is not None


def test_expired_forced_dwell_defers_during_active_qso() -> None:
    transport = FakeTransport()
    app = runtime(transport)
    app.engine.state.session.state = QsoState.QSO_ACTIVE
    app.engine.state.session.last_activity = NOW

    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)

    assert not any(isinstance(packet, SetDialFrequencyPacket) for packet in sent_packets(transport))
    assert app._forced_band_hop_pending_band == "30m"


def test_deferred_forced_hop_executes_after_qso_returns_idle() -> None:
    transport = FakeTransport()
    app = runtime(transport)
    app.engine.state.session.state = QsoState.QSO_ACTIVE
    app.engine.state.session.last_activity = NOW
    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)
    app.engine.state.reset()

    app.handle(None, NOW + timedelta(minutes=10, seconds=1), ENDPOINT)

    assert dial_command(transport).frequency_hz == 10_136_000


def test_set_dial_frequency_is_not_forced_hop_confirmation() -> None:
    transport = FakeTransport()
    app = runtime(transport)
    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)

    app.handle(status(14_074_000), NOW + timedelta(minutes=10, seconds=1), ENDPOINT)

    assert app._pending_dial_frequency is not None
    assert app._band_resume_at is None


def test_status_frequency_without_destination_drive_profile_blocks_forced_hop() -> None:
    transport = FakeTransport()
    app = runtime(transport, settle_seconds=30)
    app.handle(None, NOW + timedelta(minutes=10), ENDPOINT)

    app.handle(status(10_136_000), NOW + timedelta(minutes=10, seconds=1), ENDPOINT)

    assert app._pending_dial_frequency is None
    assert app._forced_band_hop_dwell_band == "30m"
    assert app._blocked_band_drive_change is not None
    assert app._band_resume_at is None


def test_disabled_forced_hopping_leaves_existing_behavior_unchanged() -> None:
    transport = FakeTransport()
    app = runtime(transport, enabled=False)

    app.handle(None, NOW + timedelta(minutes=11), ENDPOINT)

    assert not any(isinstance(packet, SetDialFrequencyPacket) for packet in sent_packets(transport))


def test_empty_forced_band_configuration_fails_safely(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    app = runtime(transport, bands=())

    app.handle(None, NOW + timedelta(minutes=11), ENDPOINT)

    assert not any(isinstance(packet, SetDialFrequencyPacket) for packet in sent_packets(transport))
    assert "Forced hopping is enabled but no configured bands are available" in caplog.text


def test_invalid_forced_band_configuration_fails_safely(caplog: pytest.LogCaptureFixture) -> None:
    transport = FakeTransport()
    app = runtime(transport, bands=("not-a-band",))

    app.handle(None, NOW + timedelta(minutes=11), ENDPOINT)

    assert not any(isinstance(packet, SetDialFrequencyPacket) for packet in sent_packets(transport))
    assert "Forced hopping is enabled but no configured bands are available" in caplog.text
