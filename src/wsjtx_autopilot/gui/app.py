import logging
import sys

from PySide6.QtWidgets import QApplication

from wsjtx_autopilot.config import AppPaths, SettingsStore
from wsjtx_autopilot.logging_setup import configure_logging

from .main_window import MainWindow


def run_gui() -> int:
    paths = AppPaths.from_environment().ensure_directories()
    settings_store = SettingsStore(paths.settings_path)
    settings = settings_store.load()
    level = logging.DEBUG if settings.logging_level == "debug" else logging.INFO
    configure_logging(paths, level)
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("WSJTX AutoPilot")
    application.setOrganizationName("WSJTX AutoPilot")
    window = MainWindow(settings_store)
    window.show()
    return application.exec()


def main() -> None:
    raise SystemExit(run_gui())


if __name__ == "__main__":
    main()
