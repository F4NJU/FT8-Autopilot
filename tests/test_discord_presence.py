import threading

from wsjtx_autopilot.discord_presence import DiscordPresencePublisher


class FakePresence:
    def __init__(self) -> None:
        self.connected = False
        self.updates: list[dict[str, object]] = []
        self.cleared = False
        self.closed = False
        self.updated = threading.Event()

    def connect(self) -> None:
        self.connected = True

    def update(self, **fields: object) -> None:
        self.updates.append(fields)
        self.updated.set()

    def clear(self, pid: int = 0) -> None:
        self.cleared = True

    def close(self) -> None:
        self.closed = True


def test_discord_presence_publishes_band_and_working_callsign() -> None:
    client = FakePresence()
    publisher = DiscordPresencePublisher(True, "1561768680193400902", lambda _application_id: client)
    try:
        publisher.publish("FT8", "40m", "ea7cta", True)
        assert client.updated.wait(1.0)
        assert client.connected
        assert client.updates[-1]["details"] == "FT8 - 40m"
        assert client.updates[-1]["state"] == "Working EA7CTA"
    finally:
        publisher.close()
    assert client.cleared
    assert client.closed


def test_discord_presence_uses_listening_state_and_deduplicates_updates() -> None:
    client = FakePresence()
    publisher = DiscordPresencePublisher(True, "1561768680193400902", lambda _application_id: client)
    try:
        publisher.publish("FT8", "20m", None, True)
        assert client.updated.wait(1.0)
        publisher.publish("FT8", "20m", None, True)
        assert len(client.updates) == 1
        assert client.updates[0]["state"] == "Listening on 20m"
    finally:
        publisher.close()


def test_disabled_discord_presence_never_connects() -> None:
    called = False

    def factory(_application_id: str) -> FakePresence:
        nonlocal called
        called = True
        return FakePresence()

    publisher = DiscordPresencePublisher(False, "1561768680193400902", factory)
    publisher.publish("FT8", "40m", "EA7CTA", True)
    publisher.close()

    assert not called

