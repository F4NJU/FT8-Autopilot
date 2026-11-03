from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.engine.finalization import FinalizationTracker


NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def test_confirmed_final_tx_without_end_status_expires_at_hard_deadline() -> None:
    tracker = FinalizationTracker(
        hold_periods=1,
        max_retries=3,
        final_tx_timeout_periods=2,
    )
    assert tracker.begin_qso_logged_hold(
        "OH2ZZ",
        "WSJT-X",
        "FT8",
        14_074_000,
        15,
        NOW,
    )

    assert not tracker.expire(NOW + timedelta(seconds=30))
    assert tracker.active
    assert tracker.expire(NOW + timedelta(seconds=45))
    assert not tracker.active
