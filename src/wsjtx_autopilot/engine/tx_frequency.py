from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class OccupiedRange:
    start_hz: int
    end_hz: int


@dataclass(frozen=True, slots=True)
class TxFrequencyDecision:
    selected_df: int
    reason: str
    gap_width: int = 0
    fallback: bool = False


class SpectrumOccupancyTracker:
    """Recent decode-frequency map; this is not raw FFT occupancy."""

    def __init__(self, history_seconds: float, occupied_guard_hz: int) -> None:
        self.history = timedelta(seconds=history_seconds)
        self.guard_hz = occupied_guard_hz
        self._decodes: list[tuple[datetime, int]] = []

    def add_decode(self, df: int, observed_at: datetime) -> None:
        if df >= 0:
            self._decodes.append((observed_at, df))
        self._prune(observed_at)

    def occupied_ranges(
        self,
        now: datetime,
        reserved_df: int | None = None,
    ) -> tuple[OccupiedRange, ...]:
        self._prune(now)
        frequencies = [df for _, df in self._decodes]
        if reserved_df is not None:
            frequencies.append(reserved_df)
        ranges = sorted(
            (OccupiedRange(df - self.guard_hz, df + self.guard_hz) for df in frequencies),
            key=lambda item: (item.start_hz, item.end_hz),
        )
        merged: list[OccupiedRange] = []
        for item in ranges:
            if not merged or item.start_hz > merged[-1].end_hz:
                merged.append(item)
            else:
                previous = merged[-1]
                merged[-1] = OccupiedRange(previous.start_hz, max(previous.end_hz, item.end_hz))
        return tuple(merged)

    def signal_count(self, now: datetime) -> int:
        self._prune(now)
        return len(self._decodes)

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.history
        self._decodes = [(seen_at, df) for seen_at, df in self._decodes if seen_at >= cutoff]


class TxFrequencyPlanner:
    def plan(
        self,
        remote_df: int,
        occupied_ranges: tuple[OccupiedRange, ...],
        current_tx_df: int | None,
        tx_df_min: int,
        tx_df_max: int,
        minimum_free_gap_hz: int,
    ) -> TxFrequencyDecision:
        if not occupied_ranges:
            return self._fallback(remote_df, "no occupancy data")
        if tx_df_min >= tx_df_max or not tx_df_min <= remote_df <= tx_df_max:
            return self._fallback(remote_df, "invalid planning bounds or remote DF")

        clipped: list[OccupiedRange] = []
        for item in occupied_ranges:
            start = max(tx_df_min, item.start_hz)
            end = min(tx_df_max, item.end_hz)
            if start <= end:
                clipped.append(OccupiedRange(start, end))

        gaps: list[tuple[int, int]] = []
        cursor = tx_df_min
        for item in clipped:
            if item.start_hz > cursor:
                gaps.append((cursor, item.start_hz))
            cursor = max(cursor, item.end_hz)
        if cursor < tx_df_max:
            gaps.append((cursor, tx_df_max))

        suitable = [(start, end) for start, end in gaps if end - start >= minimum_free_gap_hz]
        if not suitable:
            return self._fallback(remote_df, "no suitable free gap")

        reference = current_tx_df if current_tx_df is not None else remote_df
        start, end = min(
            suitable,
            key=lambda gap: (-(gap[1] - gap[0]), abs(((gap[0] + gap[1]) // 2) - reference), gap[0]),
        )
        return TxFrequencyDecision((start + end) // 2, "free slot", end - start, False)

    @staticmethod
    def _fallback(remote_df: int, reason: str) -> TxFrequencyDecision:
        return TxFrequencyDecision(remote_df, reason, 0, True)
