from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.engine.adaptive import BandOpportunityTracker


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_exhausted_means_every_recent_heard_station_is_worked_or_unreachable() -> None:
    tracker = BandOpportunityTracker()
    tracker.reset("20m", "FT8", NOW)
    tracker.observe("DL1ABC", NOW, worked=True)
    tracker.observe("XQ3SK", NOW)
    assert not tracker.exhausted(NOW)

    tracker.mark_unavailable("XQ3SK", NOW, 600)
    assert tracker.exhausted(NOW)
    tracker.observe("OH2ZZ", NOW + timedelta(seconds=1))
    assert not tracker.exhausted(NOW + timedelta(seconds=1))


def test_quiet_cycle_expires_without_new_viable_station() -> None:
    tracker = BandOpportunityTracker()
    tracker.reset("20m", "FT8", NOW)
    tracker.observe("XQ3SK", NOW)
    tracker.mark_unavailable("XQ3SK", NOW, 600)
    tracker.observe("XQ3SK", NOW + timedelta(minutes=2))
    assert not tracker.quiet(NOW + timedelta(minutes=2), 180)
    assert tracker.quiet(NOW + timedelta(minutes=3), 180)


def test_fresh_viable_cq_and_new_band_restart_the_clock() -> None:
    tracker = BandOpportunityTracker()
    tracker.reset("20m", "FT8", NOW)
    tracker.observe("OH2ZZ", NOW + timedelta(minutes=2))
    assert not tracker.quiet(NOW + timedelta(minutes=3), 180)
    tracker.ensure_context("17m", "FT8", NOW + timedelta(minutes=4))
    assert tracker.snapshot(NOW + timedelta(minutes=4))["heard"] == 0
    assert not tracker.quiet(NOW + timedelta(minutes=4), 180)
