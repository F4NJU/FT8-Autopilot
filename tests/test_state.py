from datetime import datetime, timezone

from wsjtx_autopilot.engine.models import DecodeEvent
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState, QsoStateMachine

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def event(text: str) -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(parsed, NOW, "FT8", -8)


def test_rr73_waits_for_local_terminal_tx() -> None:
    machine = QsoStateMachine("F4NJU", timeout_seconds=120, max_retries=3)
    machine.start_station(event("F4NJU ON4ABC -08"))

    assert machine.observe(event("F4NJU ON4ABC RR73"))
    assert machine.session.state is QsoState.WAITING_FINAL_TX
    assert not machine.session.completed


def test_rrr_waits_for_local_terminal_tx() -> None:
    machine = QsoStateMachine("F4NJU", timeout_seconds=120, max_retries=3)
    machine.start_station(event("F4NJU ON4ABC -08"))

    assert machine.observe(event("F4NJU ON4ABC RRR"))
    assert machine.session.state is QsoState.WAITING_FINAL_TX


def test_73_completes_active_qso() -> None:
    machine = QsoStateMachine("F4NJU", timeout_seconds=120, max_retries=3)
    machine.start_station(event("F4NJU ON4ABC -08"))

    assert machine.observe(event("F4NJU ON4ABC 73"))
    assert machine.session.state is QsoState.COMPLETE
