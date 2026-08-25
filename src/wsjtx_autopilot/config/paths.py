import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    database_path: Path
    log_dir: Path
    settings_path: Path
    default_adif_path: Path
    adif_candidates: tuple[Path, ...]
    legacy_database_path: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "AppPaths":
        env = environment or os.environ
        home = Path(env.get("USERPROFILE") or Path.home())
        roaming_root = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        local_root = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        config_dir = roaming_root / "WSJTX-AutoPilot"
        data_dir = local_root / "WSJTX-AutoPilot"
        standard_adif = local_root / "WSJT-X" / "wsjtx_log.adi"
        explicit_variant = local_root / "Local" / "WSJT-X" / "wsjtx_log.adi"
        return cls(
            config_dir=config_dir,
            data_dir=data_dir,
            database_path=data_dir / "autopilot.sqlite3",
            log_dir=data_dir / "logs",
            settings_path=config_dir / "settings.json",
            default_adif_path=standard_adif,
            adif_candidates=(standard_adif, explicit_variant),
            legacy_database_path=config_dir / "autopilot.sqlite3",
        )

    def ensure_directories(self) -> "AppPaths":
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_path.exists() and self.legacy_database_path.is_file():
            shutil.copy2(self.legacy_database_path, self.database_path)
        return self

    def resolve_adif_path(self, configured_path: str | Path | None = None) -> Path:
        if configured_path:
            return Path(configured_path).expanduser()
        return next((path for path in self.adif_candidates if path.is_file()), self.default_adif_path)
