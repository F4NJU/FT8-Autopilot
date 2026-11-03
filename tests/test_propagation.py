from datetime import datetime, timezone

from wsjtx_autopilot.engine.propagation import mqtt_path_probability, reply_path_probability
from wsjtx_autopilot.engine.space_weather import NoaaSpaceWeatherClient


def test_distant_40m_spot_without_local_reception_evidence_is_deprioritized() -> None:
    score = mqtt_path_probability(
        "JN18AA", "MN72AA", "PM95AA", "40m", datetime(2026, 10, 29, 12, tzinfo=timezone.utc), -10
    )

    assert score < 55


def test_nearby_receiver_is_strong_evidence_for_an_otherwise_uncertain_path() -> None:
    score = mqtt_path_probability(
        "JN18AA", "MN72AA", "JN28AA", "40m", datetime(2026, 10, 29, 22, tzinfo=timezone.utc), -8
    )

    assert score >= 55


def test_reply_path_rewards_solar_flux_on_15m_and_penalizes_high_kp() -> None:
    good = reply_path_probability(
        "JN18AA", "PM95AA", "15m", datetime(2026, 10, 29, 10, tzinfo=timezone.utc), 180, 1.0
    )
    disturbed = reply_path_probability(
        "JN18AA", "PM95AA", "15m", datetime(2026, 10, 29, 10, tzinfo=timezone.utc), 80, 7.0
    )

    assert good > disturbed


def test_reply_path_needs_valid_local_and_remote_grids() -> None:
    assert reply_path_probability("", "PM95AA", "20m", datetime(2026, 10, 29, tzinfo=timezone.utc)) == 0


def test_noaa_snapshot_uses_latest_valid_values_without_network() -> None:
    payloads = {
        "f107": [{"flux": "bad"}, {"flux": "165.4"}],
        "planetary": [{"kp_index": "3.0"}],
    }

    snapshot = NoaaSpaceWeatherClient(
        fetch=lambda url, _timeout: payloads["f107" if "f107" in url else "planetary"]
    ).refresh()

    assert snapshot.solar_flux == 165.4
    assert snapshot.kp == 3.0
