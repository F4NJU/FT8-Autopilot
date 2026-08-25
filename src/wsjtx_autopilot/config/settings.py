import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from .paths import AppPaths


class DirectCallPolicy(StrEnum):
    ALWAYS_PRIORITY = "always_priority"
    NORMAL = "normal"
    IGNORE = "ignore"


class ActivityPolicy(StrEnum):
    NORMAL = "normal"
    PRIORITY = "priority"
    IGNORE = "ignore"


@dataclass(slots=True)
class UserSettings:
    local_callsign: str = "F4NJU"
    bind_address: str = "127.0.0.1"
    udp_port: int = 2237
    preferred_continents: set[str] = field(default_factory=set)
    preferred_dxcc: set[str] = field(default_factory=set)
    direct_call_policy: DirectCallPolicy = DirectCallPolicy.ALWAYS_PRIORITY
    allow_direct_call_dupes: bool = False
    allow_dupes: bool = False
    minimum_snr: int | None = None
    favor_strong_signals: bool = True
    direct_call_bonus: int = 10_000
    preferred_dxcc_bonus: int = 1_000
    preferred_continent_bonus: int = 500
    signal_bonus_max: int = 50
    pota_policy: ActivityPolicy = ActivityPolicy.NORMAL
    sota_policy: ActivityPolicy = ActivityPolicy.NORMAL
    qrp_policy: ActivityPolicy = ActivityPolicy.NORMAL
    activity_priority_bonus: int = 750
    respond_to_cq_dx: bool = False
    blacklist: set[str] = field(default_factory=set)
    ignore_minutes: int = 10
    cty_dat_path: str | None = None
    wsjtx_log_path: str | None = None
    sync_adif_on_startup: bool = True
    worked_store_path: str | None = None
    max_actions: int = 1
    direct_reply_patched: bool = False
    activity_debug: bool = False
    max_no_progress_periods: int = 10
    stalled_qso_cooldown_seconds: float = 300.0
    remote_busy_cooldown_seconds: float = 180.0
    max_remote_cq_during_attempt: int = 2
    remote_returned_to_cq_cooldown_seconds: float = 90.0
    finalization_hold_periods: int = 1
    max_final_retries: int = 3
    smart_tx_frequency: bool = True
    smart_tx_find_free: bool = True
    smart_tx_fallback_remote: bool = True
    occupied_guard_hz: int = 70
    occupancy_history_seconds: float = 45.0
    tx_df_min: int = 300
    tx_df_max: int = 2800
    minimum_free_gap_hz: int = 120

    def normalize(self) -> None:
        self.local_callsign = self.local_callsign.strip().upper()
        self.preferred_continents = {value.strip().upper() for value in self.preferred_continents if value.strip()}
        self.preferred_dxcc = {value.strip().upper() for value in self.preferred_dxcc if value.strip()}
        self.blacklist = {value.strip().upper() for value in self.blacklist if value.strip()}
        self.max_actions = max(1, int(self.max_actions))
        self.ignore_minutes = max(1, int(self.ignore_minutes))
        self.max_no_progress_periods = max(1, int(self.max_no_progress_periods))
        self.stalled_qso_cooldown_seconds = max(0.0, float(self.stalled_qso_cooldown_seconds))
        self.remote_busy_cooldown_seconds = max(0.0, float(self.remote_busy_cooldown_seconds))
        self.max_remote_cq_during_attempt = max(1, int(self.max_remote_cq_during_attempt))
        self.remote_returned_to_cq_cooldown_seconds = max(
            0.0,
            float(self.remote_returned_to_cq_cooldown_seconds),
        )
        self.finalization_hold_periods = max(1, int(self.finalization_hold_periods))
        self.max_final_retries = max(0, int(self.max_final_retries))
        self.occupied_guard_hz = max(0, int(self.occupied_guard_hz))
        self.occupancy_history_seconds = max(1.0, float(self.occupancy_history_seconds))
        self.tx_df_min = max(0, int(self.tx_df_min))
        self.tx_df_max = max(self.tx_df_min + 1, int(self.tx_df_max))
        self.minimum_free_gap_hz = max(1, int(self.minimum_free_gap_hz))


class SettingsStore:
    """Version-tolerant JSON preferences. Runtime arming is never persisted."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_settings_path()

    def load(self) -> UserSettings:
        if not self.path.is_file():
            return UserSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return UserSettings()
        if not isinstance(payload, dict):
            return UserSettings()
        allowed = UserSettings.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in allowed}
        for key in ("preferred_continents", "preferred_dxcc", "blacklist"):
            if key in values:
                raw_values = values[key]
                values[key] = set(raw_values) if isinstance(raw_values, list) else set()
        if "direct_call_policy" in values:
            try:
                values["direct_call_policy"] = DirectCallPolicy(values["direct_call_policy"])
            except ValueError:
                values["direct_call_policy"] = DirectCallPolicy.ALWAYS_PRIORITY
        for key in ("pota_policy", "sota_policy", "qrp_policy"):
            if key in values:
                try:
                    values[key] = ActivityPolicy(values[key])
                except ValueError:
                    values[key] = ActivityPolicy.NORMAL
        settings = UserSettings(**values)
        settings.normalize()
        return settings

    def save(self, settings: UserSettings) -> None:
        settings.normalize()
        payload = asdict(settings)
        for key in ("preferred_continents", "preferred_dxcc", "blacklist"):
            payload[key] = sorted(payload[key])
        payload["direct_call_policy"] = settings.direct_call_policy.value
        for key in ("pota_policy", "sota_policy", "qrp_policy"):
            payload[key] = getattr(settings, key).value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def default_data_dir() -> Path:
    return AppPaths.from_environment().data_dir


def default_settings_path() -> Path:
    return AppPaths.from_environment().settings_path
