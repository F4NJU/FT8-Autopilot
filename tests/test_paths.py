import sys
from datetime import datetime, timezone
from pathlib import Path

from wsjtx_autopilot.config import AppPaths, SettingsStore, UserSettings
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.store import WorkedQsoStore
from wsjtx_autopilot.worked.sync import synchronize_adif
from wsjtx_autopilot.wsjtx.models import PacketHeader, QsoLoggedPacket

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "USERPROFILE": str(tmp_path / "User"),
    }


def logged(call: str = "DL1ABC") -> QsoLoggedPacket:
    return QsoLoggedPacket(
        PacketHeader(2, 5, "WSJT-X"),
        NOW,
        call,
        "JO40",
        14_074_000,
        "FT8",
        "-08",
        "-10",
        "50",
        "",
        "",
        NOW,
        "F4NJU",
        "F4NJU",
        "JN18",
        "",
        "",
        "",
    )


def test_app_paths_use_roaming_for_settings_and_local_for_mutable_data(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(environment(tmp_path))

    assert paths.settings_path == tmp_path / "Roaming" / "WSJTX-AutoPilot" / "settings.json"
    assert paths.database_path == tmp_path / "Local" / "WSJTX-AutoPilot" / "autopilot.sqlite3"
    assert paths.log_dir == tmp_path / "Local" / "FT8-AutoPilot" / "logs"
    assert paths.default_adif_path == tmp_path / "Local" / "WSJT-X" / "wsjtx_log.adi"


def test_missing_native_directories_and_database_are_created(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(environment(tmp_path)).ensure_directories()

    assert paths.config_dir.is_dir()
    assert paths.data_dir.is_dir()
    assert paths.log_dir.is_dir()
    with WorkedQsoStore(paths.database_path):
        pass
    assert paths.database_path.is_file()


def test_legacy_roaming_database_is_migrated_once(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(environment(tmp_path))
    paths.legacy_database_path.parent.mkdir(parents=True)
    with WorkedQsoStore(paths.legacy_database_path) as legacy:
        legacy.record(NOW.date(), "YO6LM", "20m", "FT8", 14_074_000, NOW, "legacy")

    paths.ensure_directories()

    with WorkedQsoStore(paths.database_path) as migrated:
        assert migrated.count_for_date(NOW.date()) == 1


def test_mutable_files_do_not_follow_executable_or_current_directory(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "Program Files" / "WSJTX-AutoPilot"
    install_dir.mkdir(parents=True)
    monkeypatch.chdir(install_dir)
    monkeypatch.setattr(sys, "_MEIPASS", str(install_dir), raising=False)
    paths = AppPaths.from_environment(environment(tmp_path / "Profile")).ensure_directories()
    SettingsStore(paths.settings_path).save(UserSettings())
    with WorkedQsoStore(paths.database_path):
        pass

    assert list(install_dir.iterdir()) == []
    assert paths.database_path.parent != install_dir
    assert paths.settings_path.parent != install_dir


def test_default_adif_is_detected_and_imported_idempotently(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(environment(tmp_path)).ensure_directories()
    paths.default_adif_path.parent.mkdir(parents=True)
    paths.default_adif_path.write_text(
        "<CALL:5>YO6LM<QSO_DATE:8>20260824<TIME_ON:6>123456<BAND:3>20M<MODE:3>FT8<EOR>",
        encoding="ascii",
    )
    assert paths.resolve_adif_path() == paths.default_adif_path
    with WorkedQsoStore(paths.database_path) as store:
        service = WorkedTodayService(store)
        first = synchronize_adif(paths.resolve_adif_path(), service)
        second = synchronize_adif(paths.resolve_adif_path(), service)

        assert first is not None and first.records_added == 1 and first.records_existing == 0
        assert second is not None and second.records_added == 0 and second.records_existing == 1
        assert store.source_for(NOW.date(), "YO6LM", "20m") == "WSJTX_ADIF"


def test_missing_adif_does_not_prevent_database_use(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(environment(tmp_path)).ensure_directories()
    with WorkedQsoStore(paths.database_path) as store:
        service = WorkedTodayService(store)

        assert synchronize_adif(paths.resolve_adif_path(), service) is None
        assert service.count(NOW.date()) == 0


def test_adif_and_qso_logged_share_one_store_with_provenance(tmp_path: Path) -> None:
    paths = AppPaths.from_environment(environment(tmp_path)).ensure_directories()
    adif = tmp_path / "custom.adi"
    adif.write_text(
        "<CALL:5>YO6LM<QSO_DATE:8>20260824<BAND:3>20M<MODE:3>FT8<EOR>",
        encoding="ascii",
    )
    with WorkedQsoStore(paths.database_path) as store:
        service = WorkedTodayService(store)
        synchronize_adif(adif, service)
        service.record_qso_logged(logged("DL1ABC"))
        service.record_qso_logged(logged("YO6LM"))

        assert service.count(NOW.date()) == 2
        assert store.source_for(NOW.date(), "YO6LM", "20m") == "WSJTX_ADIF"
        assert store.source_for(NOW.date(), "DL1ABC", "20m") == "WSJTX_QSOLOGGED"


def test_custom_adif_path_and_sync_preference_persist_without_arming(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    custom = tmp_path / "radio" / "wsjtx_log.adi"
    settings = UserSettings(wsjtx_log_path=str(custom), sync_adif_on_startup=False)
    store.save(settings)

    loaded = store.load()

    assert loaded.wsjtx_log_path == str(custom)
    assert not loaded.sync_adif_on_startup
    assert "armed" not in store.path.read_text(encoding="utf-8")
