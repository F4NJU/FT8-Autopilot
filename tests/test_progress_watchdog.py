from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.models import DecodeEvent
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.progress import QsoProgressStage
from wsjtx_autopilot.engine.state import QsoState, QsoStateMachine
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.store import WorkedQsoStore
from wsjtx_autopilot.wsjtx.models import DecodePacket, HaltTxPacket, PacketHeader, QsoLoggedPacket, StatusPacket
from wsjtx_autopilot.wsjtx.protocol import parse_datagram

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
ENDPOINT = ("127.0.0.1", 2237)


class Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.fail = False

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        if self.fail:
            raise OSError("simulated send failure")
        self.sent.append((data, endpoint))
        return len(data)


def event(text: str, offset: int = 0) -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(parsed, NOW + timedelta(seconds=offset), "FT8", -8)


def decode(text: str, sequence: int, df: int | None = None) -> DecodePacket:
    return DecodePacket(
        PacketHeader(2, 2, "WSJT-X"),
        True,
        time(12, 0, sequence % 60),
        -8,
        0.2,
        df if df is not None else 1000 + sequence,
        "~",
        text,
        False,
        False,
    )


def status() -> StatusPacket:
    return StatusPacket(
        PacketHeader(2, 1, "WSJT-X"),
        14_074_000,
        "FT8",
        "OH2ZZ",
        "-08",
        "FT8",
        True,
        False,
        False,
        900,
        900,
        "F4NJU",
        "JN18",
        "KP20",
        False,
        "",
        False,
        0,
        0xFFFFFFFF,
        15,
        "Default",
        "OH2ZZ F4NJU -08",
    )


def runtime(maximum: int = 10, cooldown: float = 300) -> tuple[AutopilotRuntime, Transport]:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=3,
        max_no_progress_periods=maximum,
        stalled_qso_cooldown_seconds=cooldown,
    )
    transport = Transport()
    return AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3)), transport


def start_qso(app: AutopilotRuntime) -> None:
    action = app.handle(decode("CQ OH2ZZ KP20", 0), NOW, ENDPOINT)
    assert action is not None
    assert app.engine.state.session.state is QsoState.CALLING_STATION


def test_nine_locator_repeats_do_not_abort_but_tenth_stalls(caplog) -> None:
    caplog.set_level("INFO")
    app, transport = runtime()
    start_qso(app)
    for index in range(1, 10):
        app.handle(decode("F4NJU OH2ZZ KP20", index), NOW + timedelta(seconds=index), ENDPOINT)

    assert app.engine.state.session.state is not QsoState.IDLE
    assert app.engine.state.session.no_progress_periods == 9

    app.handle(decode("F4NJU OH2ZZ KP20", 10), NOW + timedelta(seconds=10), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert any(isinstance(parse_datagram(data), HaltTxPacket) for data, _ in transport.sent)
    assert app.control.actions_used == 1
    assert app.control.armed  # type: ignore[attr-defined]
    assert "state CALLING_STATION -> ABORTED reason=QSO stalled" in caplog.text
    assert "state ABORTED -> IDLE reason=session reset" in caplog.text


def test_grid_to_report_resets_counter_but_changed_report_does_not_progress() -> None:
    machine = QsoStateMachine("F4NJU", 120, 3, 10)
    machine.start_station(event("CQ OH2ZZ KP20"))
    machine.observe(event("F4NJU OH2ZZ KP20", 1))
    assert machine.session.no_progress_periods == 1

    machine.observe(event("F4NJU OH2ZZ +03", 2))
    assert machine.session.progress_stage is QsoProgressStage.REPORT
    assert machine.session.no_progress_periods == 0

    machine.observe(event("F4NJU OH2ZZ +05", 3))
    assert machine.session.progress_stage is QsoProgressStage.REPORT
    assert machine.session.no_progress_periods == 1


def test_multiple_decodes_in_same_rx_period_count_once() -> None:
    app, _ = runtime()
    start_qso(app)
    app.handle(decode("F4NJU OH2ZZ KP20", 1, 1001), NOW + timedelta(seconds=1), ENDPOINT)
    app.handle(decode("F4NJU OH2ZZ KP20", 1, 1002), NOW + timedelta(seconds=2), ENDPOINT)

    assert app.engine.state.session.no_progress_periods == 1


def test_third_party_decode_and_status_do_not_reset_progress() -> None:
    app, _ = runtime()
    start_qso(app)
    app.handle(decode("F4NJU OH2ZZ KP20", 1), NOW + timedelta(seconds=1), ENDPOINT)
    assert app.engine.state.session.no_progress_periods == 1

    app.handle(decode("F4NJU DL1AAA JO40", 2), NOW + timedelta(seconds=2), ENDPOINT)
    app.handle(status(), NOW + timedelta(seconds=3), ENDPOINT)

    assert app.engine.state.session.no_progress_periods == 1


def test_stall_does_not_log_worked_and_applies_then_expires_cooldown(tmp_path) -> None:
    store = WorkedQsoStore(tmp_path / "worked.sqlite3")
    service = WorkedTodayService(store)
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        max_initiation_attempts=3,
        max_no_progress_periods=2,
        stalled_qso_cooldown_seconds=300,
    )
    transport = Transport()
    app = AutopilotRuntime(config, control=WsjtxUdpControl(transport, 15, 3), worked_service=service)
    try:
        app.handle(status(), NOW, ENDPOINT)
        start_qso(app)
        for index in range(1, 3):
            app.handle(decode("F4NJU OH2ZZ KP20", index), NOW + timedelta(seconds=index), ENDPOINT)

        assert service.count(NOW.date()) == 0
        assert app.handle(decode("CQ OH2ZZ KP20", 3), NOW + timedelta(seconds=3), ENDPOINT) is None
        assert app.control.actions_used == 1

        action = app.handle(decode("CQ OH2ZZ KP20", 4), NOW + timedelta(seconds=303), ENDPOINT)
        assert action is not None
        assert app.control.actions_used == 2
    finally:
        store.close()


def test_failed_halt_keeps_qso_blocked_and_disarms_control() -> None:
    app, transport = runtime(maximum=1)
    start_qso(app)
    transport.fail = True

    app.handle(decode("F4NJU OH2ZZ KP20", 1), NOW + timedelta(seconds=1), ENDPOINT)

    assert app.engine.state.session.state is QsoState.CALLING_STATION
    assert not app.control.armed  # type: ignore[attr-defined]
    assert app.last_qso_notice.startswith("SAFETY FAULT")


def test_progressing_qso_to_rr73_is_never_halted() -> None:
    app, transport = runtime(maximum=2)
    start_qso(app)
    messages = (
        "F4NJU OH2ZZ +03",
        "F4NJU OH2ZZ R+03",
        "F4NJU OH2ZZ RRR",
        "F4NJU OH2ZZ RR73",
    )
    for index, message in enumerate(messages, 1):
        app.handle(decode(message, index), NOW + timedelta(seconds=index), ENDPOINT)

    assert app.engine.state.session.state is QsoState.COMPLETE
    assert not any(isinstance(parse_datagram(data), HaltTxPacket) for data, _ in transport.sent)


def test_qso_logged_resets_progress_tracker() -> None:
    app, _ = runtime()
    start_qso(app)
    app.handle(decode("F4NJU OH2ZZ KP20", 1), NOW + timedelta(seconds=1), ENDPOINT)
    assert app.engine.state.session.no_progress_periods == 1
    logged = QsoLoggedPacket(
        PacketHeader(2, 5, "WSJT-X"),
        NOW,
        "OH2ZZ",
        "KP20",
        14_074_000,
        "FT8",
        "-08",
        "+03",
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

    app.handle(logged, NOW + timedelta(seconds=2), ENDPOINT)

    assert app.engine.state.session.state is QsoState.IDLE
    assert app.engine.state.progress.stage is None
    assert app.engine.state.progress.no_progress == 0
