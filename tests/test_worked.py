from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.models import ActionOutcome, DecodeEvent, IntendedAction
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.worked.adif import detect_wsjtx_log, import_adif
from wsjtx_autopilot.worked.bands import BandResolver
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.store import WorkedQsoStore
from wsjtx_autopilot.worked.sync import synchronize_adif
from wsjtx_autopilot.wsjtx.models import DecodePacket, PacketHeader, QsoLoggedPacket, StatusPacket

TODAY = date(2026, 8, 24)
NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
ENDPOINT = ("127.0.0.1", 2237)


class SpyControl(DryRunControl):
    def __init__(self) -> None:
        self.sent: list[IntendedAction] = []

    @property
    def actions_used(self) -> int:
        return len(self.sent)

    @property
    def max_actions(self) -> int:
        return 10

    def execute(self, action: IntendedAction, now: datetime) -> ActionOutcome:
        self.sent.append(action)
        return ActionOutcome.SENT


def record(store: WorkedQsoStore, *, qso_date: date = TODAY, call: str = "YO6LM", band: str = "20m", mode: str = "FT8") -> bool:
    return store.record(qso_date, call, band, mode, 14_074_000, NOW, "test")


def event(text: str, frequency: int, when: datetime = NOW, identity: str = "event") -> DecodeEvent:
    parsed = parse_ft8_message(text)
    assert parsed is not None
    return DecodeEvent(parsed, when, "FT8", -8, frequency, 15, identity)


def engine(service: WorkedTodayService, *, allow_dupes: bool = False) -> DecisionEngine:
    config = replace(AppConfig(), candidate_collection_seconds=0, allow_dupes=allow_dupes)
    return DecisionEngine(config, service.check)


def status(frequency: int) -> StatusPacket:
    return StatusPacket(
        PacketHeader(2, 1, "WSJT-X"),
        frequency,
        "FT8",
        "",
        "",
        "FT8",
        False,
        False,
        True,
        850,
        850,
        "F4NJU",
        "JN18",
        "",
        False,
        "",
        False,
        0,
        0xFFFFFFFF,
        15,
        "Default",
        "",
    )


def decode(message: str, df: int = 1_000) -> DecodePacket:
    return DecodePacket(
        PacketHeader(2, 2, "WSJT-X"),
        True,
        time(12),
        -8,
        0.2,
        df,
        "~",
        message,
        False,
        False,
    )


def logged(call: str = "YO6LM", frequency: int = 14_074_000, mode: str = "FT8") -> QsoLoggedPacket:
    return QsoLoggedPacket(
        PacketHeader(2, 5, "WSJT-X"),
        NOW,
        call,
        "KN25",
        frequency,
        mode,
        "-08",
        "-10",
        "50",
        "",
        "",
        NOW - timedelta(minutes=1),
        "F4NJU",
        "F4NJU",
        "JN18",
        "",
        "",
        "",
    )


def test_worked_today_same_band_refuses_cq_and_keeps_other_station_eligible(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store)
        service = WorkedTodayService(store)
        decision = engine(service)

        decision.observe(event("CQ YO6LM KN25", 14_074_000, identity="dupe"), NOW)
        decision.observe(event("CQ OH2ZZ KP20", 14_090_000, identity="other"), NOW)
        action = decision.decide(NOW)

        assert action is not None
        assert action.station == "OH2ZZ"


def test_worked_today_same_band_refuses_direct_call_without_engaging_state(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store)
        decision = engine(WorkedTodayService(store))

        decision.observe(event("F4NJU YO6LM -08", 14_074_000), NOW)

        assert decision.decide(NOW) is None
        assert decision.state.session.state is QsoState.IDLE
        assert decision.state.session.remote_callsign is None


def test_same_station_on_other_band_is_allowed(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store)
        decision = engine(WorkedTodayService(store))

        decision.observe(event("CQ YO6LM KN25", 7_074_000), NOW)

        assert decision.decide(NOW) is not None


def test_yesterdays_qso_does_not_block_today(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store, qso_date=TODAY - timedelta(days=1))
        decision = engine(WorkedTodayService(store))

        decision.observe(event("CQ YO6LM KN25", 14_074_000), NOW)

        assert decision.decide(NOW) is not None


def test_mode_is_not_part_of_duplicate_key_and_callsign_is_normalized(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        assert record(store, call="  yo6lm  ", mode="FT8")
        assert not record(store, call="YO6LM", mode="FT4")
        assert WorkedTodayService(store).check(" yo6lm ", 14_095_000, NOW).duplicate


def test_initiated_but_not_logged_qso_is_not_worked(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        control = SpyControl()
        app = AutopilotRuntime(
            replace(AppConfig(), candidate_collection_seconds=0),
            control=control,
            worked_service=service,
        )
        app.handle(status(14_074_000), NOW, ENDPOINT)
        app.handle(decode("CQ YO6LM KN25"), NOW, ENDPOINT)

        assert control.actions_used == 1
        assert not service.check("YO6LM", 14_074_000, NOW).duplicate


def test_qso_logged_records_manual_qso_even_without_active_session(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        app = AutopilotRuntime(AppConfig(), worked_service=service)

        app.handle(logged(), NOW, ENDPOINT)

        assert service.check("YO6LM", 14_074_000, NOW).duplicate
        assert app.engine.state.session.state is QsoState.IDLE


def test_qso_logged_date_is_normalized_to_utc(tmp_path: Path) -> None:
    local_time = datetime(2026, 8, 25, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    packet = replace(logged(), time_off=local_time)
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)

        service.record_qso_logged(packet)

        assert store.count_for_date(TODAY) == 1
        assert store.count_for_date(TODAY + timedelta(days=1)) == 0


def test_qso_logged_records_autopilot_initiated_qso(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        app = AutopilotRuntime(
            replace(AppConfig(), candidate_collection_seconds=0),
            control=SpyControl(),
            worked_service=service,
        )
        app.handle(status(14_074_000), NOW, ENDPOINT)
        app.handle(decode("CQ YO6LM KN25"), NOW, ENDPOINT)

        app.handle(logged(), NOW + timedelta(minutes=2), ENDPOINT)

        assert service.check("YO6LM", 14_074_000, NOW).duplicate
        assert app.engine.state.session.state is QsoState.IDLE


def test_store_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "worked.sqlite3"
    with WorkedQsoStore(path) as store:
        record(store)
    with WorkedQsoStore(path) as reopened:
        assert WorkedTodayService(reopened).check("YO6LM", 14_074_000, NOW).duplicate


def test_restart_bootstrap_blocks_real_sq100pkp_30m_regression(tmp_path: Path) -> None:
    path = tmp_path / "worked.sqlite3"
    with WorkedQsoStore(path) as first_process:
        assert first_process.record(TODAY, "SQ100PKP", "30m", "FT8", 10_136_000, NOW, "WSJTX_QSOLOGGED")

    with WorkedQsoStore(path) as restarted_store:
        service = WorkedTodayService(restarted_store)
        assert service.bootstrap(TODAY) == (1, 1)
        control = SpyControl()
        app = AutopilotRuntime(
            replace(AppConfig(), candidate_collection_seconds=0),
            control=control,
            worked_service=service,
        )
        app.handle(status(10_136_000), NOW, ENDPOINT)

        action = app.handle(decode("CQ SQ100PKP"), NOW + timedelta(hours=3), ENDPOINT)

        assert action is None
        assert control.sent == []


def test_existing_adif_record_bootstraps_even_when_import_adds_nothing(tmp_path: Path, caplog) -> None:
    caplog.set_level("INFO")
    adif = tmp_path / "wsjtx_log.adi"
    adif.write_text(
        "<CALL:8>SQ100PKP<QSO_DATE:8>20260824<BAND:3>30m<MODE:3>FT8<EOR>\n",
        encoding="ascii",
    )
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        first = synchronize_adif(adif, WorkedTodayService(store))
        assert first is not None and first.records_added == 1

        restarted = WorkedTodayService(store)
        second = synchronize_adif(adif, restarted)
        assert second is not None
        assert second.records_seen == 1
        assert second.records_added == 0
        assert second.records_existing == 1

        assert restarted.bootstrap(TODAY) == (1, 1)
        assert restarted.check("SQ100PKP", 10_136_000, NOW).duplicate
        assert "[WORKED] bootstrap date=2026-08-24 qsos=1 unique_call_band=1 source=database" in caplog.text


def test_bootstrap_keeps_same_call_available_on_another_band(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        assert store.record(TODAY, "SQ100PKP", "30m", "FT8", 10_136_000, NOW, "test")
        service = WorkedTodayService(store)
        service.bootstrap(TODAY)

        assert not service.check("SQ100PKP", 14_074_000, NOW).duplicate


def test_bootstrap_does_not_load_previous_utc_date(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        assert store.record(TODAY - timedelta(days=1), "SQ100PKP", "30m", "FT8", 10_136_000, NOW, "test")
        service = WorkedTodayService(store)

        assert service.bootstrap(TODAY) == (0, 0)
        assert not service.check("SQ100PKP", 10_136_000, NOW).duplicate


def test_live_qso_logged_then_restart_rebuilds_same_worked_key(tmp_path: Path) -> None:
    path = tmp_path / "worked.sqlite3"
    with WorkedQsoStore(path) as store:
        live_service = WorkedTodayService(store)
        assert live_service.record_qso_logged(logged("SQ100PKP", 10_136_000))
        assert live_service.check("SQ100PKP", 10_136_000, NOW).duplicate

    with WorkedQsoStore(path) as restarted_store:
        restarted_service = WorkedTodayService(restarted_store)
        restarted_service.bootstrap(TODAY)

        assert restarted_service.check("SQ100PKP", 10_136_000, NOW).duplicate


def test_adif_missing_band_uses_frequency_for_30m_bootstrap(tmp_path: Path) -> None:
    adif = tmp_path / "wsjtx_log.adi"
    adif.write_text(
        "<CALL:8>SQ100PKP<QSO_DATE:8>20260824<FREQ:6>10.136<MODE:3>FT8<EOR>\n",
        encoding="ascii",
    )
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        service = WorkedTodayService(store)
        result = synchronize_adif(adif, service)
        assert result is not None and result.records_added == 1
        service.bootstrap(TODAY)

        assert service.check("SQ100PKP", 10_136_000, NOW).duplicate


def test_adif_import_is_idempotent_and_derives_band_from_frequency(tmp_path: Path) -> None:
    adif = tmp_path / "wsjtx_log.adi"
    adif.write_text(
        "<CALL:5>yo6lm<QSO_DATE:8>20260824<FREQ:6>14.074<MODE:3>FT8<EOR>\n",
        encoding="ascii",
    )
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        first = import_adif(adif, store)
        second = import_adif(adif, store)

        assert first.records_added == 1
        assert second.records_added == 0
        assert WorkedTodayService(store).check("YO6LM", 14_095_000, NOW).duplicate


def test_allow_dupes_bypasses_only_worked_filter(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store)
        decision = engine(WorkedTodayService(store), allow_dupes=True)

        decision.observe(event("CQ YO6LM KN25", 14_074_000), NOW)
        action = decision.decide(NOW)

        assert action is not None
        assert action.station == "YO6LM"

        decision.observe(event("CQ DL1XYZ JO40", 14_074_000, NOW - timedelta(seconds=20), "stale"), NOW)
        assert decision.decide(NOW) is None


def test_dupe_refusal_sends_nothing_and_consumes_no_action(tmp_path: Path) -> None:
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store)
        service = WorkedTodayService(store)
        control = SpyControl()
        app = AutopilotRuntime(
            replace(AppConfig(), candidate_collection_seconds=0),
            control=control,
            worked_service=service,
        )
        app.handle(status(14_074_000), NOW, ENDPOINT)

        action = app.handle(decode("F4NJU YO6LM -08"), NOW, ENDPOINT)

        assert action is None
        assert control.actions_used == 0
        assert control.sent == []
        assert app.engine.state.session.state is QsoState.IDLE


def test_new_utc_date_makes_station_eligible_again(tmp_path: Path) -> None:
    tomorrow = NOW + timedelta(days=1)
    with WorkedQsoStore(tmp_path / "worked.sqlite3") as store:
        record(store)
        decision = engine(WorkedTodayService(store))

        decision.observe(event("CQ YO6LM KN25", 14_074_000, tomorrow), tomorrow)

        assert decision.decide(tomorrow) is not None


def test_band_resolver_uses_canonical_band_ranges() -> None:
    resolver = BandResolver()

    assert resolver.resolve(14_074_000) == "20m"
    assert resolver.resolve(14_095_000) == "20m"
    assert resolver.resolve(7_074_000) == "40m"
    assert resolver.resolve(144_174_000) == "2m"
    assert resolver.resolve(432_174_000) == "70cm"


def test_wsjtx_log_autodetection_requires_unambiguous_profile(tmp_path: Path) -> None:
    default = tmp_path / "WSJT-X"
    default.mkdir()
    first = default / "wsjtx_log.adi"
    first.write_text("", encoding="ascii")

    detected, candidates = detect_wsjtx_log(tmp_path)
    assert detected == first
    assert candidates == [first]

    rig = tmp_path / "WSJT-X - IC7300"
    rig.mkdir()
    second = rig / "wsjtx_log.adi"
    second.write_text("", encoding="ascii")

    detected, candidates = detect_wsjtx_log(tmp_path)
    assert detected is None
    assert set(candidates) == {first, second}
