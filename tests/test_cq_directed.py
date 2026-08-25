from dataclasses import replace
from datetime import datetime, time, timezone

from wsjtx_autopilot.config import ActivityPolicy, AppConfig
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.dxcc import StaticDxccResolver
from wsjtx_autopilot.engine.models import (
    ActivityTag,
    CandidateKind,
    CqType,
    DecodeEvent,
    EngineEventKind,
    StationMetadata,
    StationProfile,
)
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.scoring import CandidateScorer, ScoringPreferences
from wsjtx_autopilot.gui.viewmodels import CandidateRow
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.models import DecodePacket, PacketHeader

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
ENDPOINT = ("127.0.0.1", 2237)
LOCAL = StationMetadata("F", "F", "France", "EU")
JAPAN = StationMetadata("JA", "JA", "Japan", "AS")


class Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        self.sent.append((data, endpoint))
        return len(data)


def parsed_event(text: str, snr: int = -10) -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(parsed, NOW, "FT8", snr, 14_074_000, 15, text)


def matcher() -> CqEligibility:
    resolver = StaticDxccResolver({"F4NJU": LOCAL, "F": LOCAL, "JA": JAPAN})
    return CqEligibility(StationProfile("F4NJU", LOCAL), resolver)


def engine() -> DecisionEngine:
    return DecisionEngine(
        replace(AppConfig(), candidate_collection_seconds=0),
        cq_eligibility=matcher(),
    )


def test_general_and_matching_continent_cq_are_actionable() -> None:
    for text in ("CQ DL1ABC JO40", "CQ EU DL1ABC JO40"):
        app = engine()
        app.observe(parsed_event(text), NOW)

        action = app.decide(NOW)

        assert action is not None
        assert action.station == "DL1ABC"


def test_matching_country_prefix_cq_is_actionable() -> None:
    app = engine()
    app.observe(parsed_event("CQ F DL1ABC JO40"), NOW)

    assert app.decide(NOW) is not None


def test_alphanumeric_directed_prefix_is_not_confused_with_sender() -> None:
    message = parse_ft8_message("CQ 3D2 K1ABC FN31")

    assert message is not None
    assert message.cq_type is CqType.DIRECTED_PREFIX
    assert message.cq_target == "3D2"
    assert message.sender == "K1ABC"


def test_nonmatching_continent_prefix_and_unknown_target_are_rejected() -> None:
    cases = {
        "CQ OC VK3ABC QF22": "directed CQ target=OC local=EU",
        "CQ JA W1ABC FN31": "directed CQ target=JA local=F",
        "CQ XX W1ABC FN31": "unknown directed CQ target=XX",
    }
    for text, reason in cases.items():
        events = []
        app = DecisionEngine(
            replace(AppConfig(), candidate_collection_seconds=0),
            event_sink=events.append,
            cq_eligibility=matcher(),
        )

        app.observe(parsed_event(text), NOW)

        assert app.decide(NOW) is None
        assert events[-1].kind is EngineEventKind.CANDIDATE_REFUSED
        assert events[-1].reason == reason


def test_cq_dx_is_disabled_by_default() -> None:
    app = engine()
    app.observe(parsed_event("CQ DX OH6IH KP13"), NOW)

    assert app.decide(NOW) is None


def test_rejected_directed_cq_sends_no_reply_or_action() -> None:
    transport = Transport()
    config = replace(AppConfig(), candidate_collection_seconds=0, max_initiation_attempts=2)
    control = WsjtxUdpControl(transport, 15, 2)
    decision = DecisionEngine(config, cq_eligibility=matcher())
    runtime = AutopilotRuntime(config, decision, control)
    packet = DecodePacket(
        PacketHeader(2, 2, "WSJT-X"),
        True,
        time(12, 0),
        -8,
        0.2,
        1200,
        "~",
        "CQ OC VK3ABC QF22",
        False,
        False,
    )

    assert runtime.handle(packet, NOW, ENDPOINT) is None
    assert transport.sent == []
    assert control.actions_used == 0


def test_activity_cqs_are_explicit_metadata_only() -> None:
    pota = parse_ft8_message("CQ POTA N8ABC EN80")
    sota = parse_ft8_message("CQ SOTA G4ABC IO91")
    portable = parse_ft8_message("CQ K1ABC/P FN42")
    weak = parse_ft8_message("CQ K1ABC FN42")
    qrp = parse_ft8_message("CQ QRP K1ABC FN42")

    assert pota is not None and pota.cq_type is CqType.ACTIVITY
    assert pota.activity_tags == frozenset({ActivityTag.POTA})
    assert sota is not None and sota.activity_tags == frozenset({ActivityTag.SOTA})
    assert portable is not None and portable.activity_tags == frozenset()
    assert weak is not None and weak.activity_tags == frozenset()
    assert qrp is not None and qrp.activity_tags == frozenset({ActivityTag.QRP})


def test_activity_preferences_score_or_filter_without_hard_coding() -> None:
    event = parsed_event("CQ POTA N8ABC EN80", snr=-30)
    preferred = CandidateScorer(
        ScoringPreferences(pota_policy=ActivityPolicy.PRIORITY, favor_strong_signals=False),
    ).evaluate(event, CandidateKind.CQ)
    ignored = CandidateScorer(
        ScoringPreferences(pota_policy=ActivityPolicy.IGNORE),
    ).evaluate(event, CandidateKind.CQ)

    assert preferred.accepted
    assert preferred.breakdown.total == 750
    assert preferred.breakdown.components[0].name == "PREFERRED_POTA"
    assert not ignored.accepted
    assert ignored.reason == "POTA activity ignored"


def test_candidate_viewmodel_exposes_dxcc_continent_target_and_activity() -> None:
    event = parsed_event("CQ POTA N8ABC EN80")
    metadata = StationMetadata("K", "K", "United States", "NA")
    scorer = CandidateScorer(
        ScoringPreferences(favor_strong_signals=False),
        StaticDxccResolver({"N8ABC": metadata}),
    )
    result = scorer.evaluate(event, CandidateKind.CQ)
    from wsjtx_autopilot.engine.models import Candidate

    row = CandidateRow.from_candidate(
        Candidate("N8ABC", CandidateKind.CQ, event, result.breakdown.total, result.metadata, result.breakdown),
    )

    assert row.country == "United States"
    assert row.dxcc == "K"
    assert row.continent == "NA"
    assert row.cq_target == "GENERAL"
    assert row.activities == "POTA"
