from pathlib import Path

from wsjtx_autopilot.wsjtx_process import WsjtxProcessManager, bundled_wsjtx_executable, discover_wsjtx_executable


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float) -> int:
        assert timeout == 3.0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_disabled_manager_does_not_start_or_stop_anything() -> None:
    started: list[object] = []
    manager = WsjtxProcessManager(process_factory=lambda *_args, **_kwargs: started.append(object()))

    status = manager.reconcile(False, "")

    assert status.state == "disabled"
    assert started == []
    assert manager.stop().state == "disabled"


def test_manager_launches_configured_wsjtx_minimized_and_owns_only_that_process() -> None:
    executable = "C:/Radio/WSJT-X/bin/wsjtx.exe"
    process = FakeProcess()
    commands: list[list[str]] = []

    def start(command, **_kwargs):
        commands.append(command)
        return process

    manager = WsjtxProcessManager(
        process_factory=start,
        discover=lambda _configured: Path(executable),
        external_process_running=lambda _name: False,
    )

    status = manager.reconcile(True, executable)

    assert status.state == "started"
    assert commands == [[str(Path(executable))]]
    assert manager.owns_process
    assert manager.stop().state == "stopped"
    assert process.terminated
    assert not process.killed
    assert not manager.owns_process


def test_manager_never_starts_or_stops_an_already_running_wsjtx() -> None:
    executable = "C:/Radio/WSJT-X/bin/wsjtx.exe"
    started: list[object] = []
    manager = WsjtxProcessManager(
        process_factory=lambda *_args, **_kwargs: started.append(object()),
        discover=lambda _configured: Path(executable),
        external_process_running=lambda executable_name: executable_name == "wsjtx.exe",
    )

    status = manager.reconcile(True, executable)

    assert status.state == "external"
    assert started == []
    assert not manager.owns_process
    assert manager.stop().state == "external"


def test_manager_reports_missing_executable_without_starting() -> None:
    started: list[object] = []
    manager = WsjtxProcessManager(
        process_factory=lambda *_args, **_kwargs: started.append(object()),
        discover=lambda _configured: None,
    )

    status = manager.reconcile(True, "C:/missing/wsjtx.exe")

    assert status.state == "unavailable"
    assert "introuvable" in status.message
    assert started == []


def test_reconcile_stops_the_owned_process_when_launch_is_disabled() -> None:
    executable = "C:/Radio/WSJT-X/bin/wsjtx.exe"
    process = FakeProcess()
    manager = WsjtxProcessManager(
        process_factory=lambda *_args, **_kwargs: process,
        discover=lambda _configured: Path(executable),
        external_process_running=lambda _name: False,
    )
    assert manager.reconcile(True, executable).state == "started"

    status = manager.reconcile(False, "")

    assert status.state == "disabled"
    assert process.terminated


def test_bundled_ap1_has_priority_over_standard_installation(monkeypatch, tmp_path) -> None:
    bundled = tmp_path / "_internal" / "wsjtx-ap1" / "bin" / "wsjtx.exe"
    bundled.parent.mkdir(parents=True)
    bundled.touch()
    monkeypatch.setenv("WSJTX_AUTOPILOT_APP_DIR", str(tmp_path))

    assert bundled_wsjtx_executable() == bundled
    assert discover_wsjtx_executable() == bundled

