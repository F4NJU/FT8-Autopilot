from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.engine.adaptive import AttemptOutcome, AttemptRecord, StagnationTracker

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def attempt(callsign: str, second: int) -> AttemptRecord:
    return AttemptRecord(callsign, "20m", "FT8", NOW + timedelta(seconds=second), True, AttemptOutcome.NO_RESPONSE)


def test_repeated_small_station_pool_triggers_stagnation() -> None:
    tracker = StagnationTracker(8, 6, 3)
    for index, callsign in enumerate(("DL1ABC", "F5AAA", "DL1ABC", "ON4XYZ", "F5AAA", "DL1ABC")):
        tracker.record(attempt(callsign, index))
    assert tracker.failed_attempts == 6
    assert tracker.unique_calls == 3
    assert tracker.is_stagnating()


def test_many_unique_stations_do_not_trigger_stagnation() -> None:
    tracker = StagnationTracker(8, 6, 3)
    for index, callsign in enumerate(("DL1AAA", "F5BBB", "EA1CCC", "ON4DDD", "I2EEE", "SP3FFF")):
        tracker.record(attempt(callsign, index))
    assert not tracker.is_stagnating()


def test_success_reset_clears_attempt_history() -> None:
    tracker = StagnationTracker(8, 2, 3)
    tracker.record(attempt("DL1ABC", 0))
    tracker.record(attempt("DL1ABC", 1))
    assert tracker.is_stagnating()
    tracker.reset()
    assert tracker.failed_attempts == 0
    assert not tracker.is_stagnating()
