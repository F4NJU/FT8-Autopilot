from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.replay import ReplayFrame, replay_frames
from wsjtx_autopilot.wsjtx.models import DecodePacket, PacketHeader


NOW = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
HEADER = PacketHeader(2, 2, "WSJT-X")


def decode(message: str, df: int = 900) -> DecodePacket:
    return DecodePacket(
        HEADER,
        True,
        time(18, 0),
        -10,
        0.2,
        df,
        "~",
        message,
        False,
        False,
    )


def test_replay_never_replies_to_candidate_seen_working_another_station() -> None:
    result = replay_frames(
        replace(AppConfig(), candidate_collection_seconds=0.25),
        (
            ReplayFrame(NOW, decode("CQ TA1SW KN41"), label="CQ candidate"),
            ReplayFrame(NOW + timedelta(milliseconds=100), decode("K1ABC TA1SW -10"), label="remote busy"),
            ReplayFrame(NOW + timedelta(milliseconds=400), label="close decode window"),
        ),
    )

    assert result.actions == ()
    assert result.diagnostics["stations_busy"] == 1
    assert result.diagnostics["candidates_refused"] == 1


def test_replay_keeps_direct_call_priority_without_transmitting() -> None:
    result = replay_frames(
        replace(AppConfig(), candidate_collection_seconds=0.25),
        (
            ReplayFrame(NOW, decode("CQ TA1SW KN41"), label="CQ"),
            ReplayFrame(NOW + timedelta(milliseconds=50), decode("F4NJU OH2ZZ KP20", 1200), label="direct call"),
            ReplayFrame(NOW + timedelta(milliseconds=400), label="close decode window"),
        ),
    )

    assert [action.station for action in result.actions] == ["OH2ZZ"]
    assert result.diagnostics["candidates_selected"] == 1
    assert result.diagnostics["replies_sent"] == 0
