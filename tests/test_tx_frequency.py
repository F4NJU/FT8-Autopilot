from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.engine.tx_frequency import OccupiedRange, SpectrumOccupancyTracker, TxFrequencyPlanner

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def plan(ranges: tuple[OccupiedRange, ...], remote: int = 895, minimum: int = 120):
    return TxFrequencyPlanner().plan(remote, ranges, 1500, 300, 2800, minimum)


def test_no_occupancy_falls_back_to_remote_df() -> None:
    decision = plan(())
    assert decision.selected_df == 895
    assert decision.fallback


def test_selects_center_of_largest_gap_deterministically() -> None:
    ranges = tuple(OccupiedRange(df - 70, df + 70) for df in (400, 520, 890, 1250, 1320, 2100))
    decision = plan(ranges)
    assert decision.selected_df == 1710
    assert decision.gap_width == 640
    assert decision.reason == "free slot"


def test_small_gaps_are_rejected_and_bounds_are_respected() -> None:
    ranges = (OccupiedRange(300, 900), OccupiedRange(990, 2800))
    decision = plan(ranges, minimum=100)
    assert decision.fallback
    assert decision.selected_df == 895


def test_tracker_applies_guard_merges_ranges_and_expires_old_decodes() -> None:
    tracker = SpectrumOccupancyTracker(45, 70)
    tracker.add_decode(1000, NOW - timedelta(seconds=46))
    tracker.add_decode(1100, NOW)
    tracker.add_decode(1200, NOW)
    assert tracker.occupied_ranges(NOW) == (OccupiedRange(1030, 1270),)
    assert tracker.signal_count(NOW) == 2


def test_reserved_own_tx_is_occupied() -> None:
    tracker = SpectrumOccupancyTracker(45, 70)
    tracker.add_decode(1000, NOW)
    assert tracker.occupied_ranges(NOW, 1700) == (
        OccupiedRange(930, 1070),
        OccupiedRange(1630, 1770),
    )
