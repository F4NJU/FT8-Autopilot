from dataclasses import dataclass
from datetime import datetime
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
