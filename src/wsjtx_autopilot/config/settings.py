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
    final_tx_timeout_periods: int = 2
    max_final_retries: int = 3
    adaptive_operation_enabled: bool = True
    stagnation_attempt_window: int = 8
    stagnation_min_failed_attempts: int = 6
    stagnation_max_unique_calls: int = 3
    adaptive_parity_enabled: bool = True
    parity_trial_failed_attempts: int = 6
    automatic_band_hopping_enabled: bool = False
    allowed_auto_hop_bands: list[str] = field(default_factory=list)
    auto_hop_band_frequencies: dict[str, dict[str, int]] = field(default_factory=dict)
    minimum_band_dwell_minutes: float = 5.0
    dial_change_confirmation_timeout_seconds: float = 5.0
    pending_direct_ttl_seconds: float = 120.0
    ftx1_cat2_enabled: bool = False
    ftx1_cat2_confirmed_ftx1: bool = False
    ftx1_cat2_port: str = ""
    ftx1_cat2_baudrate: int = 38_400
    ftx1_cat2_timeout_seconds: float = 0.25
    ftx1_auto_apply_band_profiles: bool = False
    ftx1_band_profiles: dict[str, dict[str, int]] = field(default_factory=dict)
    logging_level: str = "normal"

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
        self.final_tx_timeout_periods = max(1, int(self.final_tx_timeout_periods))
        self.max_final_retries = max(0, int(self.max_final_retries))
        self.stagnation_attempt_window = max(1, int(self.stagnation_attempt_window))
        self.stagnation_min_failed_attempts = max(1, int(self.stagnation_min_failed_attempts))
        self.stagnation_max_unique_calls = max(1, int(self.stagnation_max_unique_calls))
        self.parity_trial_failed_attempts = max(1, int(self.parity_trial_failed_attempts))
        self.allowed_auto_hop_bands = [value.strip().lower() for value in self.allowed_auto_hop_bands if value.strip()]
        self.auto_hop_band_frequencies = {
            str(band).strip().lower(): {
                str(mode).upper(): int(frequency)
                for mode, frequency in values.items()
                if isinstance(frequency, int) and frequency > 0
            }
            for band, values in self.auto_hop_band_frequencies.items()
            if isinstance(values, dict)
        }
        self.minimum_band_dwell_minutes = max(0.0, float(self.minimum_band_dwell_minutes))
        self.dial_change_confirmation_timeout_seconds = max(1.0, float(self.dial_change_confirmation_timeout_seconds))
        self.pending_direct_ttl_seconds = max(1.0, float(self.pending_direct_ttl_seconds))
        self.ftx1_cat2_enabled = self.ftx1_cat2_enabled is True
        self.ftx1_cat2_confirmed_ftx1 = self.ftx1_cat2_confirmed_ftx1 is True
        self.ftx1_cat2_port = self.ftx1_cat2_port.strip()
        self.ftx1_cat2_baudrate = max(1_200, min(115_200, int(self.ftx1_cat2_baudrate)))
        self.ftx1_cat2_timeout_seconds = max(0.05, min(5.0, float(self.ftx1_cat2_timeout_seconds)))
        self.ftx1_auto_apply_band_profiles = self.ftx1_auto_apply_band_profiles is True
        raw_band_profiles = self.ftx1_band_profiles if isinstance(self.ftx1_band_profiles, dict) else {}
        self.ftx1_band_profiles = {
            str(band).strip().lower(): {
                "usb_mod_gain": values["usb_mod_gain"],
                "tx_audio_attenuation": values["tx_audio_attenuation"],
            }
            for band, values in raw_band_profiles.items()
            if str(band).strip().lower() in {"160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"}
            and isinstance(values, dict)
            and isinstance(values.get("usb_mod_gain"), int)
            and not isinstance(values.get("usb_mod_gain"), bool)
            and 0 <= values["usb_mod_gain"] <= 100
            and isinstance(values.get("tx_audio_attenuation"), int)
            and not isinstance(values.get("tx_audio_attenuation"), bool)
            and 0 <= values["tx_audio_attenuation"] <= 450
        }
        self.logging_level = "debug" if str(self.logging_level).strip().lower() == "debug" else "normal"


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
        if "allowed_auto_hop_bands" in values and not isinstance(values["allowed_auto_hop_bands"], list):
            values["allowed_auto_hop_bands"] = []
        if "auto_hop_band_frequencies" in values and not isinstance(values["auto_hop_band_frequencies"], dict):
            values["auto_hop_band_frequencies"] = {}
        if "ftx1_band_profiles" in values and not isinstance(values["ftx1_band_profiles"], dict):
            values["ftx1_band_profiles"] = {}
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
        payload["direct_call_policy"] = DirectCallPolicy(settings.direct_call_policy).value
        for key in ("pota_policy", "sota_policy", "qrp_policy"):
            payload[key] = ActivityPolicy(getattr(settings, key)).value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


def default_data_dir() -> Path:
    return AppPaths.from_environment().data_dir


def default_settings_path() -> Path:
    return AppPaths.from_environment().settings_path
