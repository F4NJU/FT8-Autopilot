from dataclasses import dataclass, field
from pathlib import Path

from .paths import AppPaths

AUTO_TX_ENABLED = False


@dataclass(frozen=True, slots=True)
class AppConfig:
    local_callsign: str = "F4NJU"
    bind_address: str = "127.0.0.1"
    udp_port: int = 2237
    dry_run: bool = True
    autocall_enabled: bool = True
    autocq_enabled: bool = False
    direct_caller_priority: int = 100
    max_retries: int = 3
    stale_decode_seconds: float = 15.0
    qso_timeout_seconds: float = 120.0
    candidate_collection_seconds: float = 0.25
    dry_run_cooldown_seconds: float = 90.0
    max_initiation_attempts: int = 1
    control_enabled: bool = AUTO_TX_ENABLED
    auto_reply_armed: bool = False
    wsjtx_direct_reply_patched: bool = False
    direct_reply_confirmation_timeout_seconds: float = 20.0
    qso_completion_grace_seconds: float = 2.0
    allow_dupes: bool = False
    respond_to_cq_dx: bool = False
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
    allowed_auto_hop_bands: tuple[str, ...] = ()
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
    cty_dat_path: Path | None = None
    worked_store_path: Path = field(default_factory=lambda: AppPaths.from_environment().database_path)
    wsjtx_log_path: Path | None = None


from .settings import ActivityPolicy, DirectCallPolicy, SettingsStore, UserSettings, default_data_dir, default_settings_path

__all__ = [
    "AUTO_TX_ENABLED",
    "AppConfig",
    "AppPaths",
    "ActivityPolicy",
    "DirectCallPolicy",
    "SettingsStore",
    "UserSettings",
    "default_data_dir",
    "default_settings_path",
]
