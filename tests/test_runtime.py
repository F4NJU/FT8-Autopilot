import logging
import struct
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, IntendedAction
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.models import DecodePacket, PacketHeader, StatusPacket
from wsjtx_autopilot.wsjtx.protocol import MAGIC, parse_datagram

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


class CollectingControl(DryRunControl):
    def __init__(self, outcome: ActionOutcome = ActionOutcome.PROPOSED_ONLY) -> None:
        self.actions: list[IntendedAction] = []
        self.outcome = outcome

    def execute(self, action: IntendedAction, now: datetime) -> ActionOutcome:
        self.actions.append(action)
        return self.outcome


def status(dx_call: str = "CX6TU", tx_enabled: bool = False) -> StatusPacket:
    return StatusPacket(
        PacketHeader(2, 1, "WSJT-X"),
        14_074_000,
        "FT8",
        dx_call,
        "-08",
        "FT8",
        tx_enabled,
        False,
        True,
        850,
        850,
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


def decode(message: str, snr: int, df: int) -> DecodePacket:
    def qbytearray(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack(">I", len(encoded)) + encoded

    data = struct.pack(">III", MAGIC, 2, 2) + qbytearray("WSJT-X")
    data += struct.pack(">BIidI", 1, 12 * 3_600_000, snr, 0.2, df)
    data += qbytearray("~") + qbytearray(message) + struct.pack(">BB", 0, 0)
    packet = parse_datagram(data)
    assert isinstance(packet, DecodePacket)
    return packet


@pytest.mark.parametrize("attenuation", [0, 125, 450])
def test_ap1_attenuation_state_reaches_runtime_without_heartbeat_fields(attenuation: int) -> None:
    instance_id = b"WSJT-X"
    data = struct.pack(">III", MAGIC, 3, 23) + struct.pack(">I", len(instance_id)) + instance_id + struct.pack(">H", attenuation)
    packet = parse_datagram(data)
    app, _ = runtime()

    app.handle(packet, NOW)

    assert app.current_tx_audio_attenuation == attenuation


def runtime(
    cooldown_seconds: float = 90,
    outcome: ActionOutcome = ActionOutcome.PROPOSED_ONLY,
) -> tuple[AutopilotRuntime, CollectingControl]:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=1,
        dry_run_cooldown_seconds=cooldown_seconds,
    )
    control = CollectingControl(outcome)
    return AutopilotRuntime(config, control=control), control


def test_status_dx_call_does_not_block_idle_autocall(caplog: object) -> None:
    app, control = runtime()
    caplog.set_level(logging.INFO)  # type: ignore[attr-defined]

    app.handle(status(), NOW)
    assert app.engine.state.session.state is QsoState.IDLE
    app.handle(decode("CQ OH2ZZ KP20", 3, 1460), NOW)
    app.handle(decode("CQ RA3Y KO73", -4, 1154), NOW)
    app.handle(decode("CQ F5SFT JN23", -9, 1207), NOW)
    app.handle(decode("CQ DX OH6IH KP13", -18, 2796), NOW)
    app.handle(decode("CQ K3JGJ FM29", -17, 1292), NOW)

    action = app.handle(None, NOW + timedelta(seconds=1))

    assert action is not None
    assert action.station == "OH2ZZ"
    assert [item.station for item in control.actions] == ["OH2ZZ"]
    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert app.status.dx_call == "CX6TU"
    assert "[ENGINE] parsed type=CQ" in caplog.text  # type: ignore[attr-defined]
    assert "[ENGINE] candidate CQ: OH2ZZ" in caplog.text  # type: ignore[attr-defined]
    assert "[ENGINE] selected: OH2ZZ" in caplog.text  # type: ignore[attr-defined]


def test_real_active_qso_is_not_diverted_by_external_decode() -> None:
    app, control = runtime(outcome=ActionOutcome.SENT)
    app.handle(status(), NOW)
    assert app.handle(decode("F4NJU ON4ABC -08", -8, 900), NOW) is None
    first = app.handle(None, NOW + timedelta(seconds=1))
    assert first is not None
    assert app.engine.state.session.remote_callsign == "ON4ABC"

    assert app.handle(decode("CQ OH2ZZ KP20", 3, 1460), NOW + timedelta(seconds=1)) is None
    assert app.handle(decode("F4NJU DL1AAA -04", -4, 1200), NOW + timedelta(seconds=2)) is None

    assert app.engine.state.session.remote_callsign == "ON4ABC"
    assert app.engine.state.session.state is QsoState.DIRECT_REPLY_SENT
    assert [item.station for item in control.actions] == ["ON4ABC"]


def test_dry_run_cooldown_skips_same_station_but_allows_another() -> None:
    app, control = runtime()
    app.handle(decode("CQ SQ5ANT KO02", -3, 1000), NOW)
    app.handle(None, NOW + timedelta(seconds=1))
    assert app.engine.state.session.state is QsoState.IDLE

    app.handle(decode("CQ SQ5ANT KO02", -2, 1001), NOW + timedelta(seconds=2))
    app.handle(decode("CQ OH2ZZ KP20", -6, 1460), NOW + timedelta(seconds=2))
    app.handle(None, NOW + timedelta(seconds=3))

    assert [item.station for item in control.actions] == ["SQ5ANT", "OH2ZZ"]
    assert app.engine.state.session.state is QsoState.IDLE


def test_exchange_between_other_stations_does_not_create_local_qso() -> None:
    app, control = runtime()

    assert app.handle(decode("EA1C SQ5ANT R-13", -10, 1200), NOW) is None

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert control.actions == []


def test_proposed_direct_call_does_not_create_observed_qso() -> None:
    app, control = runtime()

    assert app.handle(decode("F4NJU SQ5ANT -10", -10, 1200), NOW) is None
    action = app.handle(None, NOW + timedelta(seconds=1))

    assert action is not None
    assert action.station == "SQ5ANT"
    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None
    assert [item.station for item in control.actions] == ["SQ5ANT"]


def test_active_remote_talking_to_someone_else_aborts_qso() -> None:
    app, _ = runtime(outcome=ActionOutcome.SENT)
    app.handle(decode("F4NJU SQ5ANT -10", -10, 1200), NOW)
    app.handle(decode("EA1C SQ5ANT R-13", -8, 1201), NOW + timedelta(seconds=1))

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.session.remote_callsign is None


def test_cooldown_expiration_uses_supplied_replay_clock() -> None:
    app, control = runtime(cooldown_seconds=30)
    app.handle(decode("CQ SQ5ANT KO02", -3, 1000), NOW)
    app.handle(None, NOW + timedelta(seconds=1))

    app.handle(decode("CQ SQ5ANT KO02", -2, 1001), NOW + timedelta(seconds=30))
    app.handle(None, NOW + timedelta(seconds=31))
    assert [item.station for item in control.actions] == ["SQ5ANT"]

    app.handle(decode("CQ SQ5ANT KO02", -1, 1002), NOW + timedelta(seconds=32))
    app.handle(None, NOW + timedelta(seconds=33))

    assert [item.station for item in control.actions] == ["SQ5ANT", "SQ5ANT"]
    assert app.engine.state.session.state is QsoState.IDLE
