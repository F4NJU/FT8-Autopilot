"""Lifecycle management for the optional WSJT-X companion process.

AutoPilot talks to WSJT-X exclusively over its existing UDP protocol.  This
module deliberately manages only the process it starts itself: an operator's
manually started WSJT-X instance is never terminated or reconfigured.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import os
import shutil
import subprocess
import sys
from typing import Callable, Protocol


LOGGER = logging.getLogger(__name__)


def application_directory() -> Path:
    """Return the directory containing the installed AutoPilot application."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    override = os.environ.get("WSJTX_AUTOPILOT_APP_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2]


def bundled_wsjtx_executable(application_root: Path | None = None) -> Path | None:
    """Return the AP1 runtime shipped beside AutoPilot, if present."""

    root = application_root or application_directory()
    # Recent PyInstaller one-folder builds place application data in
    # ``_internal``. Keep the direct location as a compatibility fallback for
    # developer and older release layouts.
    candidates = (
        root / "_internal" / "wsjtx-ap1" / "bin" / "wsjtx.exe",
        root / "wsjtx-ap1" / "bin" / "wsjtx.exe",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


class ManagedProcess(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float) -> int: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WsjtxLaunchStatus:
    state: str
    message: str
    executable: Path | None = None


def discover_wsjtx_executable(configured_path: str = "") -> Path | None:
    """Find an explicit, bundled, or standard WSJT-X executable without starting it."""

    configured = Path(configured_path.strip()).expanduser() if configured_path.strip() else None
    if configured is not None:
        return configured if configured.is_file() else None

    candidates: list[Path] = []
    bundled = bundled_wsjtx_executable()
    if bundled is not None:
        candidates.append(bundled)
    for environment_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(Path(root) / "WSJT-X" / "bin" / "wsjtx.exe")
    which = shutil.which("wsjtx.exe") or shutil.which("wsjtx")
    if which:
        candidates.append(Path(which))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def is_wsjtx_process_running(executable_name: str) -> bool:
    """Return whether Windows already has a process with this executable name."""

    if not executable_name:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable_name}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        # Failing closed here could start a second radio engine.  The caller
        # remains usable with a manual WSJT-X instance, so report it as busy.
        return True
    return executable_name.casefold() in result.stdout.casefold()


class WsjtxProcessManager:
    """Start WSJT-X minimized and stop only the instance owned by AutoPilot."""

    def __init__(
        self,
        *,
        process_factory: Callable[..., ManagedProcess] | None = None,
        discover: Callable[[str], Path | None] = discover_wsjtx_executable,
        external_process_running: Callable[[str], bool] = is_wsjtx_process_running,
    ) -> None:
        self._process_factory = process_factory or subprocess.Popen
        self._discover = discover
        self._external_process_running = external_process_running
        self._process: ManagedProcess | None = None
        self._executable: Path | None = None
        self._external_seen = False

    @property
    def owns_process(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def reconcile(self, enabled: bool, configured_path: str) -> WsjtxLaunchStatus:
        """Make the managed process match the saved preference safely."""

        if not enabled:
            self.stop()
            return WsjtxLaunchStatus("disabled", "Lancement intégré de WSJT-X désactivé.")

        executable = self._discover(configured_path)
        if executable is None:
            self.stop()
            return WsjtxLaunchStatus(
                "unavailable",
                "WSJT-X est introuvable : indique son fichier wsjtx.exe dans les préférences.",
            )

        if self.owns_process and self._executable == executable:
            return WsjtxLaunchStatus("running", "WSJT-X est lancé par AutoPilot.", executable)
        if self.owns_process:
            self.stop()

        if self._external_process_running(executable.name):
            self._external_seen = True
            self._executable = executable
            return WsjtxLaunchStatus(
                "external",
                "WSJT-X est déjà lancé ; AutoPilot utilise cette instance sans en prendre possession.",
                executable,
            )

        if bundled_wsjtx_executable() == executable:
            # AP1 keeps a separate Qt profile. Seed it only when the bundled
            # engine will actually be started, never while an external WSJT-X
            # is already running and never by overwriting an AP1 profile.
            from wsjtx_autopilot.wsjtx_profile import migrate_existing_wsjtx_profile

            migration = migrate_existing_wsjtx_profile()
            LOGGER.info("[WSJTX] bundled AP1 profile migration state=%s detail=%s", migration.state, migration.message)

        try:
            self._process = self._process_factory([str(executable)], **self._popen_options())
        except OSError as exc:
            self._process = None
            self._executable = None
            LOGGER.exception("[WSJTX] integrated launch failed executable=%s", executable)
            return WsjtxLaunchStatus("failed", f"Impossible de démarrer WSJT-X : {exc}", executable)

        self._executable = executable
        self._external_seen = False
        LOGGER.info("[WSJTX] integrated launch started executable=%s", executable)
        return WsjtxLaunchStatus("started", "WSJT-X est démarré réduit par AutoPilot.", executable)

    def stop(self) -> WsjtxLaunchStatus:
        """Stop the owned process, never an externally started WSJT-X."""

        process = self._process
        self._process = None
        executable = self._executable
        if process is None:
            state = "external" if self._external_seen else "disabled"
            message = (
                "WSJT-X lancé manuellement reste ouvert."
                if state == "external"
                else "Aucune instance WSJT-X gérée par AutoPilot."
            )
            return WsjtxLaunchStatus(state, message, executable)
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                LOGGER.warning("[WSJTX] integrated process did not stop, terminating forcefully")
                process.kill()
            except OSError:
                LOGGER.exception("[WSJTX] failed to stop integrated process")
        self._executable = None
        self._external_seen = False
        LOGGER.info("[WSJTX] integrated process stopped")
        return WsjtxLaunchStatus("stopped", "WSJT-X lancé par AutoPilot a été fermé.", executable)

    @staticmethod
    def _popen_options() -> dict[str, object]:
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            # SW_SHOWMINNOACTIVE: open reduced without stealing focus from
            # AutoPilot.  It remains available from the taskbar for diagnosis.
            startupinfo.wShowWindow = 7
            options["startupinfo"] = startupinfo
        return options

