from datetime import datetime, timezone

from wsjtx_autopilot.special_calls.psk_mqtt import band_subscription_topic, parse_spot, subscription_topic


def test_subscription_is_targeted_to_one_ft8_transmitter() -> None:
    assert subscription_topic("xx9txn") == "pskr/filter/v2/+/FT8/XX9TXN/#"


def test_band_subscription_discovers_only_ft8_spots_on_an_allowed_band() -> None:
    assert band_subscription_topic("20m") == "pskr/filter/v2/20m/FT8/+/#"


def test_spot_uses_transmit_time_when_available() -> None:
    spot = parse_spot(
        '{"sc":"XX9TXN","md":"FT8","b":"80m","t":1760000000,"t_tx":1760000015,"f":3573000,"rp":-12,"rc":"F4NJU"}',
        "XX9TXN",
    )

    assert spot is not None
    assert spot.band == "80m"
    assert spot.observed_at == datetime.fromtimestamp(1760000015, timezone.utc)
    assert spot.frequency_hz == 3573000


def test_spot_preserves_transmit_and_receive_grids() -> None:
    spot = parse_spot(
        '{"sc":"XX9TXN","md":"FT8","b":"20m","t":1760000000,"sl":"OL72aa","rc":"F4ABC","rl":"JN18aa"}',
        "XX9TXN",
    )

    assert spot is not None
    assert spot.transmitter_grid == "OL72AA"
    assert spot.receiver_grid == "JN18AA"


def test_spot_rejects_a_different_transmitter() -> None:
    assert parse_spot('{"sc":"OTHER","md":"FT8","b":"20m","t":1760000000}', "XX9TXN") is None
