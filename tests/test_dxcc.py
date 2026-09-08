import pytest

from wsjtx_autopilot.engine.dxcc import PrefixDxccResolver


@pytest.mark.parametrize(
    ("callsign", "country", "continent"),
    (
        ("IU4SQE", "Italy", "EU"),
        ("LZ1ZF", "Bulgaria", "EU"),
        ("K6ND", "United States", "NA"),
        ("VE3ARF", "Canada", "NA"),
        ("JA1ABC", "Japan", "AS"),
        ("F4NJU", "France", "EU"),
        ("EA8ABC", "Canary Islands", "AF"),
    ),
)
def test_prefix_resolver_returns_local_country_metadata(callsign: str, country: str, continent: str) -> None:
    metadata = PrefixDxccResolver().resolve(callsign)

    assert metadata.country_name == country
    assert metadata.continent == continent


def test_prefix_resolver_leaves_unknown_callsigns_unresolved() -> None:
    metadata = PrefixDxccResolver().resolve("ZZ9ZZ")

    assert metadata.country_name == "unknown"


def test_prefix_resolver_keeps_portable_calls_on_their_home_entity() -> None:
    metadata = PrefixDxccResolver().resolve("F4NJU/P")

    assert metadata.country_name == "France"
    assert metadata.continent == "EU"
