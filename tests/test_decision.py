from dataclasses import replace
from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, DecodeEvent
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
import pytest

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def event(text: str, *, age: float = 0, snr: int = -10, identity: str = "") -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(parsed, NOW - timedelta(seconds=age), "FT8", snr, 14_074_000, 15, identity)


def config(**changes: object) -> AppConfig:
    return replace(AppConfig(candidate_collection_seconds=1), **changes)


def test_direct_caller_outranks_unrelated_cq_while_idle() -> None:
    engine = DecisionEngine(config())
    engine.observe(event("CQ DL1AAA JO40", age=2, identity="cq1"), NOW)
    engine.observe(event("CQ EA2BBB IN83", age=2, snr=-5, identity="cq2"), NOW)
    engine.observe(event("F4NJU ON4CCC -11", identity="direct"), NOW)

    action = engine.decide(NOW + timedelta(seconds=1))

    assert action is not None
    assert action.station == "ON4CCC"
    assert action.reason == "direct caller"
    assert action.kind is ActionKind.DIRECT_REPLY


def test_active_qso_is_not_hijacked_by_new_caller() -> None:
    engine = DecisionEngine(config())
    engine.observe(event("F4NJU ON4ABC -08", identity="first"), NOW)
    action = engine.decide(NOW + timedelta(seconds=1))
    assert action is not None
    engine.record_action_outcome(action, ActionOutcome.SENT, NOW)

    engine.observe(event("F4NJU DL1AAA -04", identity="other"), NOW)

    assert engine.state.session.remote_callsign == "ON4ABC"
    assert engine.state.session.state is QsoState.DIRECT_REPLY_SENT
    assert engine.decide(NOW) is None


def test_call_addressed_to_another_local_call_is_not_actionable() -> None:
    engine = DecisionEngine(config())

    engine.observe(event("F4ABC ON4ABC -08", identity="not-for-us"), NOW)

    assert engine.decide(NOW) is None
    assert engine.state.session.state is QsoState.IDLE


def test_stale_candidate_is_ignored() -> None:
    engine = DecisionEngine(config(stale_decode_seconds=10))

    engine.observe(event("CQ DL1AAA JO40", age=11, identity="stale"), NOW)

    assert engine.decide(NOW) is None
    assert engine.state.session.state is QsoState.IDLE


def test_duplicate_decode_does_not_produce_duplicate_action() -> None:
    engine = DecisionEngine(config())
    duplicate = event("F4NJU ON4ABC -08", identity="same-decode")
    engine.observe(duplicate, NOW)
    assert engine.decide(NOW + timedelta(seconds=1)) is not None
    engine.state.reset()

    engine.observe(duplicate, NOW)

    assert engine.decide(NOW) is None


def test_cq_waits_for_collection_window() -> None:
    engine = DecisionEngine(config(candidate_collection_seconds=1))
    engine.observe(event("CQ DL1AAA JO40", identity="cq"), NOW)

    assert engine.decide(NOW) is None
    assert engine.decide(NOW + timedelta(seconds=1)) is not None


def test_only_confirmed_action_engages_qso_from_cq() -> None:
    engine = DecisionEngine(config(candidate_collection_seconds=1))
    engine.observe(event("CQ DL1AAA JO40", identity="cq-confirmed"), NOW)
    action = engine.decide(NOW + timedelta(seconds=1))

    assert action is not None
    assert engine.state.session.state is QsoState.IDLE

    engine.record_action_outcome(action, ActionOutcome.SENT, NOW + timedelta(seconds=1))

    assert engine.state.session.state is QsoState.CALLING_STATION
    assert engine.state.session.remote_callsign == "DL1AAA"


def test_selected_candidate_alone_stays_idle() -> None:
    engine = DecisionEngine(config(candidate_collection_seconds=0))
    engine.observe(event("CQ DL1AAA JO40", identity="selected-only"), NOW)

    assert engine.decide(NOW) is not None
    assert engine.state.session.state is QsoState.IDLE
    assert engine.state.session.remote_callsign is None


@pytest.mark.parametrize(
    "outcome",
    [ActionOutcome.PROPOSED_ONLY, ActionOutcome.REJECTED_LOCAL, ActionOutcome.FAILED],
)
def test_non_sent_outcomes_stay_idle(outcome: ActionOutcome) -> None:
    engine = DecisionEngine(config(candidate_collection_seconds=0))
    engine.observe(event("CQ DL1AAA JO40", identity=outcome.name), NOW)
    action = engine.decide(NOW + timedelta(seconds=1))
    assert action is not None

    engine.record_action_outcome(action, outcome, NOW)

    assert engine.state.session.state is QsoState.IDLE
    assert engine.state.session.remote_callsign is None


def test_timeout_returns_engine_to_safe_idle_state() -> None:
    engine = DecisionEngine(config(qso_timeout_seconds=10, max_retries=1))
    engine.observe(event("F4NJU ON4ABC -08", identity="caller"), NOW)
    action = engine.decide(NOW + timedelta(seconds=1))
    assert action is not None
    engine.record_action_outcome(action, ActionOutcome.SENT, NOW)

    assert engine.abort_qso("test inactivity timeout")
    assert engine.state.session.state is QsoState.IDLE
    assert engine.state.session.remote_callsign is None
