from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto


class AdaptiveState(Enum):
    NORMAL = auto()
    PARITY_CHANGE_PENDING = auto()
    PARITY_TRIAL = auto()
    BAND_HOP_PENDING = auto()
    BAND_CHANGING = auto()
    BAND_TRIAL = auto()


class AttemptOutcome(Enum):
    SUCCESS = auto()
    NO_RESPONSE = auto()
    REMOTE_BUSY = auto()
    REMOTE_RETURNED_TO_CQ = auto()
    STALLED = auto()
    ABORTED = auto()


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    callsign: str
    band: str
    mode: str
    timestamp: datetime
    tx_first: bool | None
    outcome: AttemptOutcome


@dataclass(frozen=True, slots=True)
class BandProfile:
    band: str
    ft8_frequency_hz: int | None = None
    ft4_frequency_hz: int | None = None

    def frequency_for(self, mode: str) -> int | None:
        return self.ft8_frequency_hz if mode == "FT8" else self.ft4_frequency_hz if mode == "FT4" else None


# WSJT-X standard FT8 dial frequencies. FT4 remains ineligible until explicitly configured.
DEFAULT_BAND_PROFILES = (
    BandProfile("160m", 1_840_000), BandProfile("80m", 3_573_000),
    BandProfile("40m", 7_074_000), BandProfile("30m", 10_136_000),
    BandProfile("20m", 14_074_000), BandProfile("17m", 18_100_000),
    BandProfile("15m", 21_074_000), BandProfile("12m", 24_915_000),
    BandProfile("10m", 28_074_000), BandProfile("6m", 50_313_000),
)


class StagnationTracker:
    def __init__(self, window: int, minimum_failed: int, maximum_unique_calls: int) -> None:
        self.window = max(1, window)
        self.minimum_failed = max(1, minimum_failed)
        self.maximum_unique_calls = max(1, maximum_unique_calls)
        self._attempts: list[AttemptRecord] = []

    def record(self, attempt: AttemptRecord) -> None:
        self._attempts.append(attempt)
        del self._attempts[:-self.window]

    def reset(self) -> None:
        self._attempts.clear()

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "callsign": attempt.callsign,
                "band": attempt.band,
                "mode": attempt.mode,
                "timestamp": attempt.timestamp.isoformat(),
                "tx_first": attempt.tx_first,
                "outcome": attempt.outcome.name,
            }
            for attempt in self._attempts
        ]

    @property
    def failed_attempts(self) -> int:
        return sum(attempt.outcome is not AttemptOutcome.SUCCESS for attempt in self._attempts)

    @property
    def unique_calls(self) -> int:
        return len({attempt.callsign for attempt in self._attempts if attempt.outcome is not AttemptOutcome.SUCCESS})

    def is_stagnating(self) -> bool:
        return (
            self.failed_attempts >= self.minimum_failed
            and self.unique_calls <= self.maximum_unique_calls
        )


class BandOpportunityTracker:
    """Track distinct, currently useful CQ stations on one band and TX cycle."""

    def __init__(self, observation_seconds: float = 600.0) -> None:
        self.observation = timedelta(seconds=max(1.0, observation_seconds))
        self.band: str | None = None
        self.mode: str | None = None
        self.started_at: datetime | None = None
        self.last_viable_at: datetime | None = None
        self.heard: dict[str, datetime] = {}
        self.worked: set[str] = set()
        self.unavailable_until: dict[str, datetime] = {}

    def reset(self, band: str | None, mode: str | None, now: datetime) -> None:
        self.band = band
        self.mode = mode
        self.started_at = now
        self.last_viable_at = None
        self.heard.clear()
        self.worked.clear()
        self.unavailable_until.clear()

    def ensure_context(self, band: str | None, mode: str | None, now: datetime) -> None:
        if self.started_at is None or band != self.band or mode != self.mode:
            self.reset(band, mode, now)

    def is_unavailable(self, callsign: str, now: datetime) -> bool:
        return self.unavailable_until.get(callsign.upper(), now) > now

    def observe(self, callsign: str, now: datetime, *, worked: bool = False) -> None:
        station = callsign.upper()
        self._prune(now)
        self.heard[station] = now
        if worked:
            self.worked.add(station)
        elif station not in self.worked and not self.is_unavailable(station, now):
            self.last_viable_at = now

    def mark_worked(self, callsign: str) -> None:
        self.worked.add(callsign.upper())

    def mark_unavailable(self, callsign: str, now: datetime, seconds: float) -> None:
        station = callsign.upper()
        self.unavailable_until[station] = now + timedelta(seconds=max(0.0, seconds))

    def exhausted(self, now: datetime) -> bool:
        self._prune(now)
        return len(self.heard) >= 2 and all(
            station in self.worked or self.is_unavailable(station, now)
            for station in self.heard
        )

    def quiet(self, now: datetime, seconds: float) -> bool:
        anchor = self.last_viable_at or self.started_at
        return anchor is not None and now - anchor >= timedelta(seconds=max(1.0, seconds))

    def snapshot(self, now: datetime) -> dict[str, object]:
        self._prune(now)
        available = [
            station for station in self.heard
            if station not in self.worked and not self.is_unavailable(station, now)
        ]
        return {
            "heard": len(self.heard),
            "worked": sum(station in self.worked for station in self.heard),
            "unreachable": sum(self.is_unavailable(station, now) for station in self.heard),
            "available": available,
            "last_viable_at": self.last_viable_at.isoformat() if self.last_viable_at else None,
        }

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.observation
        self.heard = {station: seen for station, seen in self.heard.items() if seen >= cutoff}
        self.unavailable_until = {
            station: until for station, until in self.unavailable_until.items() if until > now
        }
