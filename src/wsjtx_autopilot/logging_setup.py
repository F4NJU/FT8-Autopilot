import logging
from logging.handlers import RotatingFileHandler

from wsjtx_autopilot.config import AppPaths


def configure_logging(paths: AppPaths, level: int = logging.INFO, console: bool = False) -> None:
    paths.ensure_directories()
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            paths.log_dir / "autopilot.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
