import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import DecodeEvent

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingDirectCall:
    station: str
    event: DecodeEvent
    first_seen: datetime
    last_seen: datetime
    repeat_count: int
    band: str

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.last_seen).total_seconds())


class PendingDirectCallQueue:
    def __init__(self, ttl_seconds: float = 120.0) -> None:
        self.ttl = timedelta(seconds=max(1.0, ttl_seconds))
        self._entries: dict[str, PendingDirectCall] = {}

    def offer(self, event: DecodeEvent, now: datetime, band: str) -> PendingDirectCall:
        station = event.parsed.sender.upper()
        current = self._entries.get(station)
        entry = PendingDirectCall(
            station,
            event,
            current.first_seen if current is not None else now,
            now,
            (current.repeat_count + 1) if current is not None else 1,
            band,
        )
        self._entries[station] = entry
        if current is None:
            LOGGER.info(
                "[DIRECT] pending station=%s band=%s mode=%s snr=%d df=%s",
                station,
                band,
                event.mode,
                event.snr,
                event.original.delta_frequency if event.original is not None else "-",
            )
        else:
            LOGGER.info("[DIRECT] pending refreshed station=%s repeats=%d", station, entry.repeat_count)
        return entry

    def entries(self, now: datetime) -> tuple[PendingDirectCall, ...]:
        self.prune(now)
        return tuple(self._entries.values())

    def remove(self, station: str) -> PendingDirectCall | None:
        return self._entries.pop(station.upper(), None)

    def prune(self, now: datetime) -> tuple[PendingDirectCall, ...]:
        expired = tuple(
            entry for entry in self._entries.values() if now - entry.last_seen > self.ttl
        )
        for entry in expired:
            self._entries.pop(entry.station, None)
            LOGGER.info(
                "[DIRECT] pending expired station=%s age=%.1f",
                entry.station,
                entry.age_seconds(now),
            )
        return expired

    def invalidate_instance(self, instance_id: str) -> tuple[PendingDirectCall, ...]:
        removed = tuple(
            entry
            for entry in self._entries.values()
            if entry.event.original is not None and entry.event.original.instance_id == instance_id
        )
        for entry in removed:
            self._entries.pop(entry.station, None)
        return removed

    def invalidate_context(
        self,
        instance_id: str,
        band: str,
        mode: str,
    ) -> tuple[PendingDirectCall, ...]:
        removed = tuple(
            entry
            for entry in self._entries.values()
            if entry.event.original is not None
            and (
                entry.event.original.instance_id != instance_id
                or entry.band != band
                or entry.event.mode != mode
            )
        )
        for entry in removed:
            self._entries.pop(entry.station, None)
        return removed

    def snapshot(self, now: datetime) -> list[dict[str, object]]:
        return [
            {
                "callsign": entry.station,
                "age_seconds": round(entry.age_seconds(now), 1),
                "repeat_count": entry.repeat_count,
                "snr": entry.event.snr,
                "df": entry.event.original.delta_frequency if entry.event.original is not None else None,
                "band": entry.band,
                "mode": entry.event.mode,
            }
            for entry in self.entries(now)
        ]

    def __len__(self) -> int:
        return len(self._entries)
