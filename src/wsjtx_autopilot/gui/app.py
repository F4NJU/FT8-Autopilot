import sys

from PySide6.QtWidgets import QApplication

from wsjtx_autopilot.config import AppPaths, SettingsStore
from wsjtx_autopilot.logging_setup import configure_logging

from .main_window import MainWindow


def run_gui() -> int:
    paths = AppPaths.from_environment().ensure_directories()
    configure_logging(paths)
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("WSJTX AutoPilot")
    application.setOrganizationName("WSJTX AutoPilot")
    window = MainWindow(SettingsStore(paths.settings_path))
    window.show()
    return application.exec()


def main() -> None:
    raise SystemExit(run_gui())


if __name__ == "__main__":
    main()
