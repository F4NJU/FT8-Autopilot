from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.config import AppConfig, DirectCallPolicy
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, StaticDxccResolver
from wsjtx_autopilot.engine.models import (
    CandidateKind,
    DecodeEvent,
    EngineEventKind,
    StationMetadata,
    WorkedCheck,
)
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.scoring import CandidateScorer, ScoringPreferences

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def event(text: str, snr: int = -10, age: float = 0, identity: str = "decode") -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(parsed, NOW - timedelta(seconds=age), "FT8", snr, 14_074_000, 15, identity)


def test_cty_dat_resolves_longest_prefix_and_exact_call(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(
        "Belgium:14:27:EU:50.8:-4.3:-1.0:ON:\n"
        "    OO,OP,OQ,OR,OS,OT;\n"
        "France:14:27:EU:46.0:-2.0:-1.0:F:\n"
        "    =F4NJU;\n",
        encoding="utf-8",
    )
    resolver = CtyDatResolver(path)

    assert resolver.resolve("ON4ABC").country_name == "Belgium"
    assert resolver.resolve("F4NJU").country_name == "France"
    assert resolver.resolve("K1ABC").country_name == "unknown"


def test_filters_are_applied_before_scoring() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(blacklist={"DL1BAD"}, minimum_snr=-20),
    )

    blacklisted = scorer.evaluate(event("CQ DL1BAD JO40"), CandidateKind.CQ)
    weak = scorer.evaluate(event("CQ ON4ABC JO20", snr=-21), CandidateKind.CQ)
    duplicate = scorer.evaluate(
        event("CQ ON4ABC JO20"),
        CandidateKind.CQ,
        WorkedCheck("20m", True),
    )

    assert not blacklisted.accepted and blacklisted.reason == "blacklisted"
    assert not weak.accepted and weak.reason.startswith("SNR below minimum")
    assert not duplicate.accepted and duplicate.reason == "worked today band=20m"


def test_score_breakdown_explains_dxcc_continent_and_signal() -> None:
    metadata = StationMetadata("ON", "ON", "Belgium", "EU")
    scorer = CandidateScorer(
        ScoringPreferences(preferred_dxcc={"ON"}, preferred_continents={"EU"}),
        StaticDxccResolver({"ON4ABC": metadata}),
    )

    result = scorer.evaluate(event("CQ ON4ABC JO20", snr=-5), CandidateKind.CQ)

    assert result.accepted
    assert result.metadata == metadata
    assert result.breakdown.total == 1_525
    assert [component.name for component in result.breakdown.components] == [
        "PREFERRED_DXCC_ON",
        "PREFERRED_EU",
        "SIGNAL",
    ]


def test_normal_direct_call_policy_competes_by_score() -> None:
    resolver = StaticDxccResolver(
        {"DL1AAA": StationMetadata("DL", "DL", "Germany", "EU")},
    )
    scorer = CandidateScorer(
        ScoringPreferences(
            preferred_dxcc={"DL"},
            direct_call_policy=DirectCallPolicy.NORMAL,
            favor_strong_signals=False,
        ),
        resolver,
    )
    observed_events = []
    engine = DecisionEngine(
        AppConfig(candidate_collection_seconds=1),
        scorer=scorer,
        event_sink=observed_events.append,
    )
    engine.observe(event("CQ DL1AAA JO40", age=2, identity="cq"), NOW)
    engine.observe(event("F4NJU ON4ABC -05", age=2, identity="direct"), NOW)

    action = engine.decide(NOW + timedelta(seconds=1))

    assert action is not None
    assert action.station == "DL1AAA"
    assert observed_events[-1].kind is EngineEventKind.CANDIDATE_SELECTED
    assert observed_events[-1].candidate is not None
    assert observed_events[-1].candidate.score_breakdown.total == 1_000


def test_direct_call_override_can_allow_a_duplicate() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(allow_direct_call_dupes=True),
    )

    result = scorer.evaluate(
        event("F4NJU ON4ABC -05"),
        CandidateKind.DIRECT_CALLER,
        WorkedCheck("20m", True),
    )

    assert result.accepted
    assert result.force_priority


def test_active_special_call_gets_reply_priority_bonus() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(
            prioritize_special_calls=True,
            special_calls={" tm8r "},
            favor_strong_signals=False,
        ),
    )

    special = scorer.evaluate(event("CQ TM8R JN18"), CandidateKind.CQ)
    ordinary = scorer.evaluate(event("CQ F1ABC JN18"), CandidateKind.CQ)

    assert special.breakdown.total == 5_000
    assert [component.name for component in special.breakdown.components] == ["SPECIAL_CALL"]
    assert ordinary.breakdown.total == 0


def test_special_call_priority_forces_selection_when_enabled() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(
            prioritize_special_calls=True,
            special_calls={"TM8R"},
            favor_strong_signals=False,
        ),
    )

    special = scorer.evaluate(event("CQ TM8R JN18"), CandidateKind.CQ)
    direct = scorer.evaluate(event("F4NJU ON4ABC -05"), CandidateKind.DIRECT_CALLER)

    assert special.breakdown.total == 5_000
    assert special.force_priority
    assert direct.breakdown.total == 10_000
    assert direct.force_priority


def test_rare_only_mode_refuses_regular_station() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(rare_stations_only=True, special_calls={"TM8R"})
    )

    result = scorer.evaluate(event("CQ EA7CTA IM66"), CandidateKind.CQ)

    assert not result.accepted
    assert result.reason == "rare-only mode"


def test_lotw_unconfirmed_dxcc_mode_prioritizes_new_entities_without_blocking_fallback() -> None:
    resolver = StaticDxccResolver(
        {
            "ON4ABC": StationMetadata("ON", "ON", "Belgium", "EU"),
            "EA7CTA": StationMetadata("EA", "EA", "Spain", "EU"),
        }
    )
    scorer = CandidateScorer(
        ScoringPreferences(
            unconfirmed_dxcc_only=True,
            lotw_confirmed_dxcc_entities={"ON"},
            favor_strong_signals=False,
        ),
        resolver,
    )

    confirmed = scorer.evaluate(event("CQ ON4ABC JO20"), CandidateKind.CQ)
    new = scorer.evaluate(event("CQ EA7CTA IM66"), CandidateKind.CQ)

    assert confirmed.accepted
    assert confirmed.breakdown.total == 0
    assert new.accepted
    assert [component.name for component in new.breakdown.components] == ["NEW_LOTW_DXCC"]


def test_lotw_unconfirmed_dxcc_mode_keeps_normal_calls_available_while_snapshot_is_pending() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(unconfirmed_dxcc_only=True, favor_strong_signals=False)
    )

    result = scorer.evaluate(event("CQ ON4ABC JO20"), CandidateKind.CQ)

    assert result.accepted
    assert result.breakdown.total == 0


def test_favorable_path_is_an_advisory_bonus_for_normal_cq_only() -> None:
    scorer = CandidateScorer(
        ScoringPreferences(
            prioritize_favorable_path=True,
            local_grid="JN18AA",
            solar_flux=180,
            kp=1.0,
            favor_strong_signals=False,
        )
    )

    cq = scorer.evaluate(event("CQ K1ABC FN42"), CandidateKind.CQ)
    direct = scorer.evaluate(event("F4NJU K1ABC -05"), CandidateKind.DIRECT_CALLER)

    assert any(component.name == "FAVORABLE_PATH" for component in cq.breakdown.components)
    assert all(component.name != "FAVORABLE_PATH" for component in direct.breakdown.components)
    assert direct.force_priority
