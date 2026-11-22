"""GitHub update check, download confirmation and installer staging."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from wsjtx_autopilot.config import AppPaths
from wsjtx_autopilot.updates import RELEASES_PAGE_URL, UpdateCheckResult, UpdateRelease, version_key

from .updates import UpdateCheckPoller, UpdateDownloadPoller

LOGGER = logging.getLogger(__name__)


class UpdatePanelMixin:
    def _on_update_button_clicked(self) -> None:
        if self._available_update is None:
            self._check_for_updates(manual=True)
            return
        self._offer_update(self._available_update)

    def _check_for_updates(self, manual: bool = False) -> None:
        if self._update_check_poller is not None and self._update_check_poller.isRunning():
            return
        if version_key(self._current_version) is None:
            if manual:
                QDesktopServices.openUrl(QUrl(RELEASES_PAGE_URL))
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("VÉRIFICATION…")
        poller = UpdateCheckPoller(self._current_version)
        self._update_check_poller = poller
        poller.completed.connect(lambda result: self._show_update_check(result, manual))
        poller.finished.connect(poller.deleteLater)
        poller.start()

    def _show_update_check(self, result: object, manual: bool) -> None:
        self._update_check_poller = None
        self.update_button.setEnabled(True)
        if not isinstance(result, UpdateCheckResult):
            self.update_button.setText("MISES À JOUR")
            return
        if result.available and result.release is not None:
            self._available_update = result.release
            self.update_button.setText(f"MISE À JOUR v{result.release.version}")
            self.update_button.setProperty("available", True)
            self.update_button.style().unpolish(self.update_button)
            self.update_button.style().polish(self.update_button)
            self.statusBar().showMessage(
                f"Mise à jour v{result.release.version} disponible — clique sur le bouton MISE À JOUR.",
                10_000,
            )
            if manual:
                self._offer_update(result.release)
            return
        self.update_button.setText("À JOUR")
        self.update_button.setProperty("available", False)
        self.update_button.style().unpolish(self.update_button)
        self.update_button.style().polish(self.update_button)
        if manual:
            self.statusBar().showMessage(result.warning or "AutoPilot est déjà à jour.", 6_000)
        elif result.warning:
            LOGGER.info("[UPDATE] background check skipped: %s", result.warning)

    def _offer_update(self, release: UpdateRelease) -> None:
        if release.installer is None or release.checksums is None:
            answer = QMessageBox.information(
                self,
                "Mise à jour disponible",
                f"La version {release.version} est disponible, mais son installateur vérifiable n'est pas encore joint à la release.\n\n"
                "La page GitHub va s'ouvrir pour consulter les notes et téléchargements.",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Open,
            )
            if answer == QMessageBox.StandardButton.Open:
                QDesktopServices.openUrl(QUrl(release.release_url))
            return
        answer = QMessageBox.question(
            self,
            "Installer la mise à jour ?",
            f"Télécharger et vérifier WSJTX AutoPilot v{release.version} ?\n\n"
            "L'installateur ne sera lancé qu'après vérification SHA-256. AutoPilot restera ouvert pendant le téléchargement.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("TÉLÉCHARGEMENT…")
        paths = AppPaths.from_environment().ensure_directories()
        poller = UpdateDownloadPoller(release, paths.data_dir / "updates")
        self._update_download_poller = poller
        poller.completed.connect(self._offer_verified_installer)
        poller.failed.connect(self._show_update_download_error)
        poller.finished.connect(poller.deleteLater)
        poller.start()

    def _show_update_download_error(self, message: str) -> None:
        self._update_download_poller = None
        self.update_button.setEnabled(True)
        self.update_button.setText("MISE À JOUR")
        QMessageBox.warning(self, "Mise à jour non installée", message)

    def _offer_verified_installer(self, installer: object) -> None:
        self._update_download_poller = None
        self.update_button.setEnabled(True)
        self.update_button.setText("MISE À JOUR PRÊTE")
        path = Path(str(installer))
        answer = QMessageBox.question(
            self,
            "Mise à jour vérifiée",
            "La somme SHA-256 est correcte. AutoPilot va se fermer, s'installer discrètement, puis redémarrer. Continuer ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("Installateur vérifié, prêt lorsque tu cliqueras à nouveau sur MISE À JOUR.", 8_000)
            return
        installer_arguments = [
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
        ]
        if not QProcess.startDetached(str(path), installer_arguments):
            QMessageBox.critical(self, "Mise à jour", "Impossible de démarrer l'installateur vérifié.")
            return
        self.statusBar().showMessage("Mise à jour silencieuse lancée : arrêt d'AutoPilot…")
        QTimer.singleShot(0, self.close)

