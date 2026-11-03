import json
from datetime import datetime, timezone

from wsjtx_autopilot.special_calls.models import SpecialCallActivity
from wsjtx_autopilot.special_calls.providers import AfuToolsProvider
from wsjtx_autopilot.special_calls.ranking import rank_activity


NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


class Response:
    headers: dict[str, str] = {}

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload


def test_provider_only_exposes_announced_calls_with_ft8_evidence() -> None:
    def opener(request, **_kwargs):
        if "/dx/spots?" in request.full_url:
            return Response(
                {
                    "quellenangabe": {"nennung": "Sources : afu.tools; cluster"},
                    "spots": [
                        {"call": "TM85VGC", "zeit": "2026-10-24T11:45:00Z", "band": "20m", "modus": "FT8", "quelle": "cluster"},
                        {"call": "N2S", "zeit": "2026-10-24T11:40:00Z", "band": "20m", "modus": "SSB", "quelle": "cluster"},
                    ],
                }
            )
        if "/dx/gehoert/" in request.full_url:
            return Response({"spots": []})
        return Response(
            {
                "dxpeditions": [
                    {"call": "TM85VGC", "start": "2026-10-20", "end": "2026-11-13", "entity": "France", "dxcc": "F", "cont": "EU"},
                    {"call": "N2S", "start": "2026-10-20", "end": "2026-10-30", "entity": "United States", "dxcc": "K", "cont": "NA"},
                ]
            }
        )

    provider = AfuToolsProvider("key", opener=opener)
    snapshot = provider.fetch(NOW)

    assert [activity.callsign for activity in snapshot.activities] == ["TM85VGC"]
    assert snapshot.activities[0].bands == ("20m",)
    assert snapshot.activities[0].active_hours_utc == (11,)
    assert snapshot.activities[0].is_french


def test_provider_uses_psk_reporter_when_cluster_has_no_ft8_spot() -> None:
    def opener(request, **_kwargs):
        if "/dx/spots?" in request.full_url:
            return Response({"quellenangabe": {"nennung": "Sources : afu.tools"}, "spots": []})
        if "/dx/gehoert/TM8R?" in request.full_url:
            return Response(
                {
                    "spots": [
                        {"zeit": "2026-10-24T11:59:00Z", "band": "20m", "modus": "FT8", "quelle": "psk"}
                    ]
                }
            )
        return Response(
            {
                "dxpeditions": [
                    {"call": "TM8R", "start": "2026-10-23", "end": "2026-11-01", "entity": "France", "dxcc": "F", "cont": "EU"}
                ]
            }
        )

    snapshot = AfuToolsProvider("key", opener=opener, allowed_bands=("20m", "40m")).fetch(NOW)

    assert [activity.callsign for activity in snapshot.activities] == ["TM8R"]
    assert snapshot.activities[0].bands == ("20m",)
    assert snapshot.activities[0].sources == ("afu.tools", "psk")


def test_ranking_rejects_activity_outside_allowed_bands() -> None:
    activity = SpecialCallActivity(
        "5W0ZP", "Samoa", "5W", "OC", NOW, NOW, ("10m",), (12,), NOW, ("cluster",), ("FT8",)
    )

    ranked = rank_activity(activity, ["20m", "40m"], NOW)

    assert ranked.path_score == 0
    assert ranked.best_band is None


def test_recent_ft8_evidence_produces_explainable_rank() -> None:
    activity = SpecialCallActivity(
        "TM85VGC", "France", "F", "EU", NOW, NOW, ("20m",), (12,), NOW, ("cluster",), ("FT8",)
    )

    ranked = rank_activity(activity, ["20m"], NOW)

    assert ranked.path_score == 100
    assert ranked.best_band == "20m"
    assert ranked.confidence == "forte"
