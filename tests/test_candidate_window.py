from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import logging

import pytest

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.models import ActionOutcome, CandidateKind, CooldownKind, DecodeEvent, IntendedAction, OriginalDecode, StationMetadata, WorkedCheck
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.scoring import CandidateScorer, ScoringPreferences
from wsjtx_autopilot.engine.dxcc import StaticDxccResolver
from wsjtx_autopilot.engine.tx_frequency import TxFrequencyDecision
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.models import DecodePacket, PacketHeader

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
DEBOUNCE = 0.25


def event(
    text: str,
    *,
    snr: int = -10,
    identity: str | None = None,
    observed_at: datetime = NOW,
) -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(
        parsed,
        observed_at,
        "FT8",
        snr,
        14_074_000,
        15,
        identity or text,
    )


def engine(**changes: object) -> DecisionEngine:
    return DecisionEngine(replace(AppConfig(), candidate_collection_seconds=DEBOUNCE, **changes))


def close_window(app: DecisionEngine):
    return app.decide(NOW + timedelta(seconds=DEBOUNCE))


def test_cq_alone_waits_then_is_selected() -> None:
    app = engine()
    app.observe(event("CQ TA1SW KN41"), NOW)
    assert app.decide(NOW) is None
    action = close_window(app)
    assert action is not None and action.station == "TA1SW"


@pytest.mark.parametrize(
    "messages",
    [
        ("CQ TA1SW KN41", "F4NJU RW1CW KO59"),
        ("F4NJU RW1CW KO59", "CQ TA1SW KN41"),
        ("CQ EA7GHD IM66", "CQ TA1SW KN41", "F4NJU RW1CW KO59"),
    ],
)
def test_direct_call_wins_regardless_of_decode_order(messages: tuple[str, ...]) -> None:
    app = engine()
    for message in messages:
        app.observe(event(message), NOW)
        assert app.decide(NOW) is None
    action = close_window(app)
    assert action is not None and action.station == "RW1CW"


def test_cq_provisional_is_logged_as_preempted_by_direct(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    app = engine()
    app.observe(event("CQ TA1SW KN41"), NOW)
    app.observe(event("F4NJU RW1CW KO59"), NOW)
    assert "candidate preempted old=TA1SW type=CQ new=RW1CW type=DIRECT_CALL" in caplog.text


def test_real_ea7ghd_rw1cw_ta1sw_batch_selects_rw1cw() -> None:
    app = engine()
    app.observe(event("CQ EA7GHD IM66"), NOW)
    app.observe(event("F4NJU RW1CW KO59"), NOW + timedelta(milliseconds=40))
    app.observe(event("CQ TA1SW KN41"), NOW + timedelta(milliseconds=80))

    assert app.decide(NOW + timedelta(milliseconds=200)) is None
    action = app.decide(NOW + timedelta(milliseconds=330))

    assert action is not None
    assert action.station == "RW1CW"
    assert action.reason == "direct caller"


def test_worked_direct_call_does_not_block_cq() -> None:
    def worked(station: str, frequency: int | None, observed_at: datetime) -> WorkedCheck:
        return WorkedCheck("20m", station == "YO6LM")

    app = DecisionEngine(
        replace(AppConfig(), candidate_collection_seconds=DEBOUNCE),
        worked_lookup=worked,
    )
    app.observe(event("F4NJU YO6LM KN25"), NOW)
    app.observe(event("CQ TA1SW KN41"), NOW)
    action = close_window(app)
    assert action is not None and action.station == "TA1SW"


@pytest.mark.parametrize(
    "cooldown_kind",
    [
        CooldownKind.STATION_RETRY,
        CooldownKind.STALLED_QSO,
        CooldownKind.REMOTE_RETURNED_TO_CQ,
    ],
)
def test_direct_call_overrides_soft_cooldown_after_failed_attempt(
    cooldown_kind: CooldownKind,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = replace(
        AppConfig(),
        candidate_collection_seconds=DEBOUNCE,
        allow_dupes=True,
        dry_run_cooldown_seconds=300,
    )
    app = DecisionEngine(config)
    app.observe(event("CQ YO2LFP KN06", identity="initial"), NOW)
    initial = app.decide(NOW + timedelta(milliseconds=250))
    assert initial is not None
    app.record_action_outcome(initial, ActionOutcome.SENT, NOW + timedelta(milliseconds=250))
    if cooldown_kind is CooldownKind.STATION_RETRY:
        assert app.state.expire_reply_confirmation(NOW + timedelta(seconds=21), 20)
    else:
        assert app.abort_qso(
            "failed outgoing attempt",
            NOW + timedelta(seconds=21),
            300,
            cooldown_kind,
        )

    inbound_at = NOW + timedelta(seconds=135)
    app.observe(
        event("F4NJU YO2LFP -13", identity="inbound", observed_at=inbound_at),
        inbound_at,
    )
    app.observe(
        event("CQ ES3BH KO28", identity="other-cq", observed_at=inbound_at),
        inbound_at,
    )
    action = app.decide(inbound_at + timedelta(milliseconds=250))

    assert action is not None
    assert action.station == "YO2LFP"
    assert action.reason == "direct caller"
    assert app.cooldown_for("YO2LFP", inbound_at) is None
    assert f"override soft blocker={cooldown_kind.name}" in caplog.text
    assert "candidate accepted station=YO2LFP" in caplog.text


def test_exact_failed_yo2lfp_capture_never_selects_es3bh() -> None:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=DEBOUNCE,
        allow_dupes=True,
        dry_run_cooldown_seconds=300,
    )
    app = DecisionEngine(config)
    app.observe(event("CQ YO2LFP KN06", identity="145045"), NOW)
    first = app.decide(NOW + timedelta(milliseconds=250))
    assert first is not None and first.station == "YO2LFP"
    app.record_action_outcome(first, ActionOutcome.SENT, NOW + timedelta(seconds=15))
    assert app.state.expire_reply_confirmation(NOW + timedelta(seconds=36), 20)

    inbound_at = NOW + timedelta(minutes=2, seconds=30)
    app.observe(event("F4NJU YO2LFP -13", identity="145315-direct", observed_at=inbound_at), inbound_at)
    app.observe(event("CQ ES3BH KO28", identity="145315-cq", observed_at=inbound_at), inbound_at)
    selected = app.decide(inbound_at + timedelta(milliseconds=250))

    assert selected is not None
    assert selected.station == "YO2LFP"
    assert selected.station != "ES3BH"


def test_blacklisted_direct_call_does_not_block_cq() -> None:
    scorer = CandidateScorer(ScoringPreferences(blacklist={"YO6LM"}, allow_dupes=True))
    app = DecisionEngine(
        replace(AppConfig(), candidate_collection_seconds=DEBOUNCE, allow_dupes=True),
        scorer=scorer,
    )
    app.observe(event("F4NJU YO6LM KN25"), NOW)
    app.observe(event("CQ TA1SW KN41"), NOW)
    action = close_window(app)
    assert action is not None and action.station == "TA1SW"
    assert app.state.session.state.name == "IDLE"


def test_stale_direct_call_is_hard_blocked_and_cq_remains_selectable() -> None:
    app = engine(stale_decode_seconds=10)
    arrival = NOW + timedelta(seconds=11)
    app.observe(event("F4NJU YO2LFP -13", identity="stale"), arrival)
    app.observe(
        event("CQ ES3BH KO28", identity="fresh-cq", observed_at=arrival),
        arrival,
    )
    action = app.decide(arrival + timedelta(milliseconds=250))
    assert action is not None and action.station == "ES3BH"


def test_direct_during_already_sent_unprogressed_attempt_is_logged_not_preempted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    app = engine(allow_dupes=True)
    app.observe(event("CQ ES3BH KO28", identity="current"), NOW)
    current = close_window(app)
    assert current is not None
    app.record_action_outcome(current, ActionOutcome.SENT, NOW + timedelta(milliseconds=250))

    app.observe(
        event("F4NJU YO2LFP -13", identity="later-direct", observed_at=NOW + timedelta(seconds=1)),
        NOW + timedelta(seconds=1),
    )

    assert app.state.session.remote_callsign == "ES3BH"
    assert "pending direct caller YO2LFP current_attempt=ES3BH progressed=no" in caplog.text


def test_multiple_direct_calls_compete_by_score() -> None:
    resolver = StaticDxccResolver(
        {
            "RW1CW": StationMetadata("UA", "UA", "Russia", "EU"),
            "JA2ABC": StationMetadata("JA", "JA", "Japan", "AS"),
            "DL1XYZ": StationMetadata("DL", "DL", "Germany", "EU"),
        }
    )
    scorer = CandidateScorer(
        ScoringPreferences(preferred_dxcc={"JA"}, allow_dupes=True),
        resolver,
    )
    app = DecisionEngine(
        replace(AppConfig(), candidate_collection_seconds=DEBOUNCE, allow_dupes=True),
        scorer=scorer,
    )
    app.observe(event("F4NJU RW1CW KO59", snr=5), NOW)
    app.observe(event("F4NJU JA2ABC PM95", snr=-15), NOW)
    app.observe(event("F4NJU DL1XYZ JO40", snr=10), NOW)
    action = close_window(app)
    assert action is not None and action.station == "JA2ABC"


def test_preferred_cq_never_beats_always_priority_direct_call() -> None:
    resolver = StaticDxccResolver(
        {"EA7GHD": StationMetadata("EA", "EA", "Spain", "EU")}
    )
    scorer = CandidateScorer(
        ScoringPreferences(preferred_dxcc={"EA"}, preferred_continents={"EU"}, allow_dupes=True),
        resolver,
    )
    app = DecisionEngine(
        replace(AppConfig(), candidate_collection_seconds=DEBOUNCE, allow_dupes=True),
        scorer=scorer,
    )
    app.observe(event("CQ EA7GHD IM66", snr=20), NOW)
    app.observe(event("F4NJU RW1CW KO59", snr=-20), NOW)
    action = close_window(app)
    assert action is not None and action.station == "RW1CW"
    assert app.state.session.state.name == "IDLE"


class SpyPlanner:
    def __init__(self) -> None:
        self.remote_dfs: list[int] = []

    def plan(self, remote_df: int, *args: object) -> TxFrequencyDecision:
        self.remote_dfs.append(remote_df)
        return TxFrequencyDecision(remote_df, "test fallback", fallback=True)


class SentControl(DryRunControl):
    def __init__(self) -> None:
        self.actions: list[IntendedAction] = []

    def execute(self, action: IntendedAction, now: datetime) -> ActionOutcome:
        self.actions.append(action)
        return ActionOutcome.SENT


def packet(message: str, df: int) -> DecodePacket:
    return DecodePacket(
        PacketHeader(2, 2, "WSJT-X"),
        True,
        time(12, 0),
        -10,
        0.2,
        df,
        "~",
        message,
        False,
        False,
    )


def test_smart_tx_is_planned_only_for_final_candidate() -> None:
    config = replace(AppConfig(), candidate_collection_seconds=DEBOUNCE, allow_dupes=True)
    runtime = AutopilotRuntime(config, control=DryRunControl())
    planner = SpyPlanner()
    runtime.tx_frequency_planner = planner  # type: ignore[assignment]
    runtime.handle(packet("CQ TA1SW KN41", 900), NOW)
    runtime.handle(packet("F4NJU RW1CW KO59", 1200), NOW + timedelta(milliseconds=50))
    assert planner.remote_dfs == []

    action = runtime.handle(None, NOW + timedelta(milliseconds=300))

    assert action is not None and action.station == "RW1CW"
    assert planner.remote_dfs == [1200]


def test_exact_capture_sends_second_reply_to_yo2lfp_not_es3bh() -> None:
    config = replace(
        AppConfig(),
        candidate_collection_seconds=DEBOUNCE,
        allow_dupes=True,
        dry_run_cooldown_seconds=300,
    )
    control = SentControl()
    runtime = AutopilotRuntime(config, control=control)
    runtime.handle(packet("CQ YO2LFP KN06", 900), NOW)
    runtime.handle(None, NOW + timedelta(milliseconds=250))
    assert [action.station for action in control.actions] == ["YO2LFP"]
    runtime.handle(None, NOW + timedelta(seconds=21))

    inbound_at = NOW + timedelta(seconds=135)
    runtime.handle(packet("F4NJU YO2LFP -13", 1200), inbound_at)
    runtime.handle(packet("CQ ES3BH KO28", 1400), inbound_at)
    runtime.handle(None, inbound_at + timedelta(milliseconds=250))

    assert [action.station for action in control.actions] == ["YO2LFP", "YO2LFP"]
    assert control.actions[-1].original_decode is not None
    assert control.actions[-1].original_decode.message == "F4NJU YO2LFP -13"
