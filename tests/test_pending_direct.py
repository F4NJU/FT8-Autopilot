from datetime import datetime, time, timedelta, timezone

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.models import CooldownKind, DecodeEvent, OriginalDecode, StationCooldown, WorkedCheck
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.pending_direct import PendingDirectCallQueue
from wsjtx_autopilot.engine.state import QsoState

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def event(message: str, *, df: int, identity: str) -> DecodeEvent:
    parsed = parse_ft8_message(message)
    assert parsed is not None
    return DecodeEvent(
        parsed,
        NOW,
        "FT8",
        -8,
        14_074_000,
        15,
        identity,
        OriginalDecode(
            "WSJT-X",
            3,
            time(12, 0),
            -8,
            0.2,
            df,
            "~",
            message,
            False,
            True,
            ("127.0.0.1", 2237),
        ),
    )


def test_pending_direct_deduplicates_refreshes_raw_decode_and_expires() -> None:
    queue = PendingDirectCallQueue(ttl_seconds=120)
    first = event("F4NJU S51DD +01", df=1200, identity="first")
    refreshed = event("F4NJU S51DD +01", df=1450, identity="refreshed")

    queue.offer(first, NOW, "20m")
    queue.offer(refreshed, NOW + timedelta(seconds=15), "20m")

    entries = queue.entries(NOW + timedelta(seconds=15))
    assert len(entries) == 1
    assert entries[0].first_seen == NOW
    assert entries[0].last_seen == NOW + timedelta(seconds=15)
    assert entries[0].repeat_count == 2
    assert entries[0].event is refreshed
    assert entries[0].event.original is not None
    assert entries[0].event.original.delta_frequency == 1450
    assert queue.entries(NOW + timedelta(seconds=136)) == ()


def active_engine(*, worked_lookup=None) -> DecisionEngine:
    engine = DecisionEngine(AppConfig(candidate_collection_seconds=0), worked_lookup=worked_lookup)
    engine.state.start_station(event("CQ R5DT KO85", df=900, identity="current"), NOW)
    engine.state.observe(event("F4NJU R5DT -10", df=1000, identity="current-report"))
    assert engine.state.session.state is QsoState.QSO_ACTIVE
    return engine


def test_pending_direct_is_revalidated_against_worked_today_before_cq_selection() -> None:
    engine = active_engine(
        worked_lookup=lambda station, frequency, observed_at: WorkedCheck("20m", station == "S51DD")
    )
    engine.observe(event("F4NJU S51DD +01", df=1200, identity="pending"), NOW)
    assert len(engine.pending_direct_calls.entries(NOW)) == 1
    assert engine.complete_qso("R5DT", "WSJT-X", "test completion")

    engine.observe(event("CQ SP9MOC JO90", df=1400, identity="cq"), NOW)
    action = engine.decide(NOW + timedelta(seconds=1))

    assert action is not None
    assert action.station == "SP9MOC"
    assert len(engine.pending_direct_calls.entries(NOW + timedelta(seconds=1))) == 0


def test_pending_direct_overrides_soft_cooldown_when_promoted() -> None:
    engine = active_engine()
    engine.observe(event("F4NJU S51DD +01", df=1200, identity="pending"), NOW)
    engine._cooldowns["S51DD"] = StationCooldown(  # type: ignore[attr-defined]
        NOW + timedelta(minutes=5),
        CooldownKind.STALLED_QSO,
        "temporary stalled-QSO cooldown",
    )
    assert engine.complete_qso("R5DT", "WSJT-X", "test completion")
    engine.observe(event("CQ SP9MOC JO90", df=1400, identity="cq"), NOW)

    action = engine.decide(NOW + timedelta(seconds=1))

    assert action is not None
    assert action.station == "S51DD"
    assert action.reason == "PENDING_DIRECT_CALL"
    assert engine.cooldown_for("S51DD", NOW + timedelta(seconds=1)) is None
