from datetime import datetime, time, timezone

from wsjtx_autopilot.engine.models import (
    Candidate,
    CandidateKind,
    DecodeEvent,
    ParsedMessage,
    ScoreBreakdown,
    ScoreComponent,
    StationMetadata,
    MessageKind,
)
from wsjtx_autopilot.gui.viewmodels import CandidateRow, StatusView
from wsjtx_autopilot.runtime import WsjtxStatus


def test_candidate_view_exposes_score_explanation() -> None:
    event = DecodeEvent(
        ParsedMessage("CQ ON4ABC JO20", MessageKind.CQ, "ON4ABC"),
        datetime(2026, 8, 24, tzinfo=timezone.utc),
        "FT8",
        -5,
    )
    candidate = Candidate(
        "ON4ABC",
        CandidateKind.CQ,
        event,
        1_025,
        StationMetadata("ON", "ON", "Belgium", "EU"),
        ScoreBreakdown(1_025, (ScoreComponent("PREFERRED_DXCC_ON", 1_000), ScoreComponent("SIGNAL", 25))),
    )

    row = CandidateRow.from_candidate(candidate)

    assert row.country == "Belgium"
    assert row.score == 1_025
    assert row.score_detail == "PREFERRED_DXCC_ON +1000, SIGNAL +25"


def test_status_view_formats_radio_state() -> None:
    status = StatusView.from_status(WsjtxStatus(14_074_000, "FT8", "DL1AAA", True, False))

    assert status.frequency == "14.074000 MHz"
    assert status.mode == "FT8"
    assert status.tx_state == "TX ENABLED"
