import json
from datetime import datetime, timezone

from wsjtx_autopilot.wavelog import WavelogLotwCatalog, WavelogV2Client


class Response:
    headers: dict[str, str] = {}

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload


def test_wavelog_lotw_client_paginates_and_uses_bearer_auth() -> None:
    observed_urls: list[str] = []

    def opener(request, **_kwargs):
        observed_urls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer wl2_secret"
        if "&page=1" in request.full_url:
            return Response({"data": [{"callsign": "ON4ABC"}], "meta": {"current_page": 1, "last_page": 2}})
        return Response({"data": [{"callsign": "EA7CTA"}], "meta": {"current_page": 2, "last_page": 2}})

    callsigns = WavelogV2Client("https://log.example", "wl2_secret", opener=opener).lotw_confirmation_callsigns()

    assert callsigns == {"ON4ABC", "EA7CTA"}
    assert all("/index.php/api/v2/confirmation?" in url for url in observed_urls)
    assert all("type=lotw" in url for url in observed_urls)


def test_wavelog_catalog_uses_last_good_cache_when_refresh_is_unavailable(tmp_path) -> None:
    catalog = WavelogLotwCatalog(tmp_path / "lotw.json")

    class Client:
        def lotw_confirmation_callsigns(self):
            return {"ON4ABC"}

    snapshot = catalog.refresh(Client(), datetime(2026, 10, 29, 12, tzinfo=timezone.utc))
    cached = catalog.load_cache("Wavelog inaccessible")

    assert snapshot.available and snapshot.callsigns == {"ON4ABC"}
    assert cached.available and cached.stale
    assert cached.warning == "Wavelog inaccessible"
