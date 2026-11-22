import argparse
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wsjtx_autopilot import main as cli
from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl


def test_cli_defaults_keep_control_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["wsjtx-autopilot"])

    args = cli._arguments()

    assert args.control is False
    assert args.arm_auto_reply is False
    assert args.max_actions == 1
    assert args.gui is False


def test_positive_integer_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._positive_int("0")
    assert cli._positive_int("3") == 3


def test_build_control_requires_both_control_and_arming_flags(tmp_path) -> None:
    transport = object()
    kill_switch = tmp_path / "DISARM_AUTOPILOT"

    assert isinstance(
        cli.build_control(transport, AppConfig(), kill_switch),
        DryRunControl,
    )
    assert isinstance(
        cli.build_control(transport, AppConfig(control_enabled=True), kill_switch),
        DryRunControl,
    )


def test_existing_kill_switch_keeps_control_disarmed(tmp_path) -> None:
    kill_switch = tmp_path / "DISARM_AUTOPILOT"
    kill_switch.touch()
    config = AppConfig(control_enabled=True, auto_reply_armed=True)

    control = cli.build_control(object(), config, kill_switch)

    assert isinstance(control, WsjtxUdpControl)
    assert not control.armed


def test_build_control_honours_action_budget_and_kill_switch(tmp_path) -> None:
    transport = object()
    kill_switch = tmp_path / "DISARM_AUTOPILOT"
    config = AppConfig(
        control_enabled=True,
        auto_reply_armed=True,
        max_initiation_attempts=4,
    )

    control = cli.build_control(transport, config, kill_switch)

    assert isinstance(control, WsjtxUdpControl)
    assert control.max_actions == 4
    assert control.armed


def test_main_maps_arguments_to_runtime_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kill_switch = tmp_path / "DISARM_AUTOPILOT"
    worked_store = tmp_path / "worked.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wsjtx-autopilot",
            "--callsign",
            "f4nju",
            "--max-actions",
            "5",
            "--kill-switch-file",
            str(kill_switch),
            "--worked-store",
            str(worked_store),
        ],
    )
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "AppPaths",
        SimpleNamespace(
            from_environment=lambda: SimpleNamespace(
                ensure_directories=lambda: tmp_path,
                data_dir=tmp_path,
                database_path=worked_store,
            )
        ),
    )
    monkeypatch.setattr(cli, "WorkedQsoStore", lambda path: MagicMock())
    monkeypatch.setattr(cli, "WorkedTodayService", lambda store: MagicMock())
    monkeypatch.setattr(cli, "_synchronize_wsjtx_log", lambda *args: None)
    captured: dict[str, AppConfig] = {}
    monkeypatch.setattr(
        cli,
        "run",
        lambda config, *args, **kwargs: captured.setdefault("config", config),
    )

    cli.main()

    config = captured["config"]
    assert config.local_callsign == "F4NJU"
    assert config.max_initiation_attempts == 5
    assert config.worked_store_path == worked_store
    assert config.control_enabled is False
