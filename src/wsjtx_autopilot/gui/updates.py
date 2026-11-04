"""Qt workers for update work that must never delay radio control or the GUI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from wsjtx_autopilot.updates import GitHubUpdateClient, UpdateCheckResult, UpdateError, UpdateRelease


class UpdateCheckPoller(QThread):
    completed = Signal(object)

    def __init__(self, current_version: str) -> None:
        super().__init__()
        self._current_version = current_version

    def run(self) -> None:
        self.completed.emit(GitHubUpdateClient().check(self._current_version))


class UpdateDownloadPoller(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, release: UpdateRelease, destination_dir: Path) -> None:
        super().__init__()
        self._release = release
        self._destination_dir = destination_dir

    def run(self) -> None:
        try:
            self.completed.emit(GitHubUpdateClient(timeout_seconds=20.0).download_verified_installer(
                self._release,
                self._destination_dir,
            ))
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit("Mise à jour impossible : erreur inattendue.")

