import json
import logging
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wsjtx_autopilot.config import AppPaths, UserSettings
from wsjtx_autopilot.logging_setup import (
    cleanup_logs,
    configure_logging,
    export_diagnostic,
    sanitize,
    set_file_log_level,
)

NOW = datetime(2026, 8, 26, 12, 34, 56, 789000, tzinfo=timezone.utc)


def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_environment(
        {
            "APPDATA": str(tmp_path / "Roaming"),
            "LOCALAPPDATA": str(tmp_path / "Local"),
            "USERPROFILE": str(tmp_path / "User"),
        }
    )


def flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_configure_logging_creates_timestamped_session_with_metadata(tmp_path: Path) -> None:
    session = configure_logging(paths(tmp_path), now=NOW)
    logging.getLogger("test").info("[TEST] visible")
    flush_logs()

    assert session.path.parent == tmp_path / "Local" / "FT8-AutoPilot" / "logs"
    assert session.path.name == "FT8-AutoPilot_2026-08-26_123456_789.log"
    content = session.path.read_text(encoding="utf-8")
    assert "[APP] FT8-AutoPilot version=v0.1.0-wip.4" in content
    assert "[APP] git_commit=" in content
    assert "[APP] build_time=" in content
    assert "[TEST] visible" in content


def test_normal_and_debug_file_levels_can_change_at_runtime(tmp_path: Path) -> None:
    session = configure_logging(paths(tmp_path), level=logging.INFO, now=NOW)
    logger = logging.getLogger("level-test")
    logger.debug("hidden-debug")
    set_file_log_level("debug")
    logger.debug("visible-debug")
    flush_logs()

    content = session.path.read_text(encoding="utf-8")
    assert "hidden-debug" not in content
    assert "visible-debug" in content


def test_sanitize_redacts_nested_secrets() -> None:
    safe = sanitize(
        {
            "station": "F4NJU",
            "api_key": "abc",
            "nested": {"password": "def", "access_token": "ghi"},
        }
    )

    assert safe == {
        "station": "F4NJU",
        "api_key": "***REDACTED***",
        "nested": {"password": "***REDACTED***", "access_token": "***REDACTED***"},
    }


def test_export_contains_logs_metadata_and_sanitized_settings(tmp_path: Path) -> None:
    app_paths = paths(tmp_path)
    session = configure_logging(app_paths, now=NOW)
    session.context.wsjtx_version = "2.7.0"
    logging.getLogger("test").info("session content")
    flush_logs()
    settings = {"local_callsign": "F4NJU", "api_token": "do-not-export"}

    destination = export_diagnostic(app_paths, settings, now=NOW + timedelta(minutes=1))

    with zipfile.ZipFile(destination) as archive:
        assert {"current.log", "diagnostic.json", "settings-sanitized.json"} <= set(archive.namelist())
        diagnostic = json.loads(archive.read("diagnostic.json"))
        exported_settings = json.loads(archive.read("settings-sanitized.json"))
    assert diagnostic["wsjtx_version"] == "2.7.0"
    assert exported_settings["api_token"] == "***REDACTED***"
    assert "do-not-export" not in destination.read_bytes().decode("latin1")


def test_cleanup_applies_age_and_total_size_limits(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "FT8-AutoPilot_old.log"
    first = log_dir / "FT8-AutoPilot_first.log"
    second = log_dir / "FT8-AutoPilot_second.log"
    old.write_bytes(b"old")
    first.write_bytes(b"12345678")
    second.write_bytes(b"abcdefgh")
    os.utime(old, (NOW.timestamp() - 20 * 86_400,) * 2)
    os.utime(first, (NOW.timestamp() - 20,) * 2)
    os.utime(second, (NOW.timestamp() - 10,) * 2)

    cleanup_logs(log_dir, retention_days=14, max_total_bytes=10, now=NOW)

    assert not old.exists()
    assert not first.exists()
    assert second.exists()


def test_logging_level_setting_round_trips(tmp_path: Path) -> None:
    from wsjtx_autopilot.config import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(logging_level="debug"))

    assert store.load().logging_level == "debug"
