from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .client import WavelogV2Client


@dataclass(frozen=True, slots=True)
class WavelogLotwSnapshot:
    callsigns: frozenset[str]
    refreshed_at: datetime | None
    available: bool
    stale: bool = False
    warning: str = ""


class WavelogLotwCatalog:
    """Caches a LoTW callsign snapshot only; credentials are never cached."""

    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path

    def refresh(self, client: WavelogV2Client, now: datetime | None = None) -> WavelogLotwSnapshot:
        refreshed_at = now or datetime.now(timezone.utc)
        callsigns = frozenset(client.lotw_confirmation_callsigns())
        payload = {
            "refreshed_at": refreshed_at.isoformat(),
            "callsigns": sorted(callsigns),
        }
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self._cache_path)
        return WavelogLotwSnapshot(callsigns, refreshed_at, True)

    def load_cache(self, warning: str = "") -> WavelogLotwSnapshot:
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            raw_callsigns = payload.get("callsigns", [])
            callsigns = frozenset(str(value).strip().upper() for value in raw_callsigns if str(value).strip())
            refreshed_at = datetime.fromisoformat(str(payload.get("refreshed_at", "")).replace("Z", "+00:00"))
            if refreshed_at.tzinfo is None:
                refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
            return WavelogLotwSnapshot(callsigns, refreshed_at.astimezone(timezone.utc), True, True, warning)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return WavelogLotwSnapshot(frozenset(), None, False, False, warning)
