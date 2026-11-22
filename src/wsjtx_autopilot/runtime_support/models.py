"""Value types shared by the runtime and its extracted collaborators."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class HuntTarget:
    callsign: str
    bands: tuple[str, ...]
    best_band: str | None
    path_score: int


@dataclass(frozen=True, slots=True)
class HuntRemoteSpot:
    """A recent remote FT8 reception used only to select a Hunt RX band."""

    callsign: str
    band: str
    observed_at: datetime
    frequency_hz: int | None = None
    snr: int | None = None
    receiver: str = ""
    transmitter_grid: str = ""
    receiver_grid: str = ""
    path_probability: int = 0


@dataclass(slots=True)
class PendingWatchdogHalt:
    instance_id: str
    remote: str
    deadline: datetime
    disarm_reason: str = "halt_tx_unconfirmed_wsjtx_watchdog"
    source: str = "AutopilotRuntime._poll_watchdog_halt_confirmation"
    context: str = "WATCHDOG"


@dataclass(slots=True)
class PendingAtu100Tune:
    """A bounded AP1 Tune carrier requested after a confirmed band change."""

    instance_id: str
    band: str
    ack_deadline: datetime
    stop_at: datetime | None = None
    active_confirmed: bool = False

