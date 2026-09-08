from wsjtx_autopilot.engine.location import StationLocatorCache, distance_and_bearing, grid_to_location
from wsjtx_autopilot.engine.models import StationMetadata
from wsjtx_autopilot.engine.parser import parse_ft8_message


def test_cq_decode_stores_the_sender_locator() -> None:
    parsed = parse_ft8_message("CQ IU4SQE JN54")
    assert parsed is not None

    cache = StationLocatorCache()
    cache.note(parsed.sender, parsed.grid)

    assert cache.get("IU4SQE") is not None
    assert cache.get("IU4SQE").grid == "JN54"


def test_reports_and_terminal_tokens_cannot_become_locators() -> None:
    cache = StationLocatorCache()
    for token in ("RR73", "R-12", "+03", "73"):
        cache.note("IU4SQE", token)

    assert cache.get("IU4SQE") is None


def test_known_grids_produce_distance_and_initial_bearing() -> None:
    local = grid_to_location("JN54")
    remote = grid_to_location("JN35")
    assert local is not None and remote is not None

    assert distance_and_bearing(local, remote) == (334, 291)


def test_country_reference_location_is_supported_as_an_approximation() -> None:
    local = grid_to_location("JN54")
    assert local is not None
    italy = StationMetadata("I", "I", "Italy", "EU", 42.8, 12.8)

    assert distance_and_bearing(local, italy) is not None
