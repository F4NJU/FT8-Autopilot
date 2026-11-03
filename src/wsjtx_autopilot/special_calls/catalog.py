from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .models import SpecialCallActivity, SpecialCallSnapshot
from .ranking import rank_activity


class SpecialCallCatalog:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path

    def refresh(self, provider, allowed_bands: tuple[str, ...] | list[str], local_grid: str = "") -> SpecialCallSnapshot:
        snapshot = provider.fetch()
        activities = self._visible_ranked(snapshot.activities, allowed_bands, snapshot.generated_at, local_grid)
        result = replace(snapshot, activities=activities)
        self._save(result)
        return result

    def load_cache(self, allowed_bands: tuple[str, ...] | list[str], warning: str = "", local_grid: str = "") -> SpecialCallSnapshot:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            snapshot = SpecialCallSnapshot.from_json(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return SpecialCallSnapshot(datetime.now(timezone.utc), (), "", True, warning)
        activities = self._visible_ranked(snapshot.activities, allowed_bands, datetime.now(timezone.utc), local_grid)
        return replace(snapshot, activities=activities, stale=True, warning=warning)

    @staticmethod
    def _visible_ranked(
        activities: tuple[SpecialCallActivity, ...],
        allowed_bands: tuple[str, ...] | list[str],
        now: datetime,
        local_grid: str = "",
    ) -> tuple[SpecialCallActivity, ...]:
        ranked = (
            rank_activity(activity, allowed_bands, now, local_grid)
            for activity in activities
            if activity.ft8_verified and (activity.end is None or activity.end >= now)
        )
        return tuple(sorted((item for item in ranked if item.best_band is not None), key=lambda item: (-item.path_score, item.callsign)))

    def _save(self, snapshot: SpecialCallSnapshot) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(snapshot.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.cache_path)
