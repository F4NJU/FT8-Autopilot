import json
import logging
import os
import platform
import sys
import threading
import traceback
import zipfile
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wsjtx_autopilot import __version__
from wsjtx_autopilot.config import AppPaths

LOGGER = logging.getLogger(__name__)
REDACTED = "***REDACTED***"
SECRET_MARKERS = ("token", "secret", "password", "passwd", "api_key", "apikey", "credential")


@dataclass(slots=True)
class DiagnosticContext:
    wsjtx_version: str = "unknown"
    wsjtx_schema: int | None = None
    wsjtx_instance: str = "unknown"
    wsjtx_endpoint: str = "unknown"
    wsjtx_revision: str = "unknown"
    ap1_controls_available: bool = False
    band: str = "unknown"
    mode: str = "unknown"
    database_schema_version: int = 1
    feature_flags: dict[str, bool] = field(default_factory=dict)
    pending_direct_calls: list[dict[str, object]] = field(default_factory=list)
    adaptive: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoggingSession:
    path: Path
    started_at: datetime
    level: int
    context: DiagnosticContext


_CURRENT_SESSION: LoggingSession | None = None


def configure_logging(
    paths: AppPaths,
    level: int = logging.INFO,
    console: bool = False,
    now: datetime | None = None,
) -> LoggingSession:
    global _CURRENT_SESSION
    paths.ensure_directories()
    started_at = now or datetime.now(timezone.utc)
    cleanup_logs(paths.log_dir, now=started_at)
    filename = f"FT8-AutoPilot_{started_at.astimezone(timezone.utc).strftime('%Y-%m-%d_%H%M%S_%f')[:-3]}.log"
    log_path = paths.log_dir / filename
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    handlers: list[logging.Handler] = [file_handler]
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        handlers.append(console_handler)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    _CURRENT_SESSION = LoggingSession(log_path, started_at, level, DiagnosticContext())
    install_exception_handlers()
    metadata = build_metadata(log_path)
    LOGGER.info("[APP] FT8-AutoPilot version=%s", metadata["app_version"])
    LOGGER.info("[APP] git_commit=%s", metadata["git_commit"])
    LOGGER.info("[APP] build=%s", metadata["build"])
    LOGGER.info("[APP] build_time=%s", metadata["build_time"])
    LOGGER.info("[APP] python=%s", metadata["python"])
    LOGGER.info("[APP] windows=%s", metadata["os"])
    LOGGER.info("[APP] executable=%s", metadata["executable"])
    LOGGER.info("[APP] log_file=%s", log_path)
    return _CURRENT_SESSION


def current_logging_session() -> LoggingSession | None:
    return _CURRENT_SESSION


def set_file_log_level(level_name: str) -> None:
    level = logging.DEBUG if level_name.strip().lower() == "debug" else logging.INFO
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.setLevel(level)
    LOGGER.info("[APP] logging level=%s", logging.getLevelName(level))


def install_exception_handlers() -> None:
    def handle_exception(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger(__name__).critical(
            "[FATAL] Unhandled exception\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )

    def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        handle_exception(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


def sanitize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _secret_key(str(key)) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int, float, bool)):
        return value.value
    return value


def export_diagnostic(
    paths: AppPaths,
    settings: Any,
    destination: Path | None = None,
    now: datetime | None = None,
) -> Path:
    session = current_logging_session()
    created_at = now or datetime.now(timezone.utc)
    destination = destination or paths.log_dir / f"FT8-AutoPilot-diagnostic-{created_at.strftime('%Y%m%d-%H%M%S')}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = build_metadata(session.path if session is not None else None)
    context = asdict(session.context) if session is not None else asdict(DiagnosticContext())
    diagnostic = sanitize({**metadata, **context, "created_at": created_at.isoformat()})
    safe_settings = sanitize(settings)
    current_log = session.path if session is not None and session.path.is_file() else None
    logs = sorted(
        (path for path in paths.log_dir.glob("FT8-AutoPilot_*.log") if path != current_log),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if current_log is not None:
            archive.write(current_log, "current.log")
        if logs:
            archive.write(logs[0], "previous.log" if current_log is not None else "current.log")
        archive.writestr("diagnostic.json", json.dumps(diagnostic, indent=2, sort_keys=True))
        archive.writestr("settings-sanitized.json", json.dumps(safe_settings, indent=2, sort_keys=True))
    LOGGER.info("[DIAGNOSTIC] exported path=%s", destination)
    return destination


def cleanup_logs(
    log_dir: Path,
    retention_days: int = 14,
    max_total_bytes: int = 100_000_000,
    now: datetime | None = None,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    current = now or datetime.now(timezone.utc)
    cutoff = current.timestamp() - timedelta(days=retention_days).total_seconds()
    logs = sorted(log_dir.glob("FT8-AutoPilot_*.log"), key=lambda path: path.stat().st_mtime)
    for path in logs:
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
    logs = sorted(log_dir.glob("FT8-AutoPilot_*.log"), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in logs)
    for path in logs:
        if total <= max_total_bytes:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


def build_metadata(log_path: Path | None = None) -> dict[str, Any]:
    embedded = _embedded_build_info()
    version = (
        os.environ.get("FT8_AUTOPILOT_VERSION")
        or embedded.get("app_version")
    )
    executable = Path(sys.executable)
    try:
        build_time = datetime.fromtimestamp(executable.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        build_time = "unknown"
    return {
        "app_version": version or f"v{__version__}-wip.5",
        "git_commit": (
            os.environ.get("FT8_AUTOPILOT_GIT_COMMIT")
            or os.environ.get("GITHUB_SHA")
            or embedded.get("git_commit", "unknown")
        ),
        "build": "pyinstaller" if getattr(sys, "frozen", False) else "source",
        "build_time": os.environ.get("FT8_AUTOPILOT_BUILD_TIME") or embedded.get("build_time", build_time),
        "python": platform.python_version(),
        "os": platform.platform(),
        "executable": sys.executable,
        "log_file": str(log_path) if log_path is not None else "unknown",
    }


def _secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_MARKERS)


def _embedded_build_info() -> dict[str, str]:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        value = json.loads((root / "ft8-autopilot-build-info.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
