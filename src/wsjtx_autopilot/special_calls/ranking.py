from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from wsjtx_autopilot.engine.location import distance_and_bearing, grid_to_location
from wsjtx_autopilot.engine.models import StationMetadata

from .models import SpecialCallActivity


def rank_activity(
    activity: SpecialCallActivity,
    allowed_bands: tuple[str, ...] | list[str],
    now: datetime | None = None,
    local_grid: str = "",
) -> SpecialCallActivity:
    """Return an explainable reachability estimate, never a TX authorization."""
    current = now or datetime.now(timezone.utc)
    allowed = tuple(dict.fromkeys(value.lower() for value in allowed_bands))
    usable = tuple(band for band in activity.bands if band.lower() in allowed)
    if not usable:
        return replace(activity, path_score=0, confidence="faible", best_band=None)

    freshness = 0
    if activity.last_ft8_seen is not None:
        age_minutes = max(0.0, (current - activity.last_ft8_seen).total_seconds() / 60)
        freshness = max(0, round(45 - min(45, age_minutes / 7)))
    schedule = 10 if activity.start and activity.end and activity.start <= current <= activity.end else 3
    hour_match = 15 if current.hour in activity.active_hours_utc else 5 if activity.active_hours_utc else 0
    distance_km = None
    local = grid_to_location(local_grid)
    if local is not None and activity.latitude is not None and activity.longitude is not None:
        path = distance_and_bearing(
            local,
            StationMetadata(reference_latitude=activity.latitude, reference_longitude=activity.longitude),
        )
        distance_km = path[0] if path is not None else None
    band_scores = {band: _band_path_score(band, distance_km, current.hour) for band in usable}
    best_band = max(usable, key=lambda band: (band_scores[band], -usable.index(band)))
    score = min(100, freshness + schedule + hour_match + band_scores[best_band])
    confidence = "forte" if freshness >= 40 and hour_match >= 15 else "moyenne" if freshness >= 20 else "faible"
    return replace(activity, path_score=score, confidence=confidence, best_band=best_band)


def _band_path_score(band: str, distance_km: int | None, utc_hour: int) -> int:
    daylight = 7 <= utc_hour < 19
    if distance_km is None:
        preferred = {"20m", "17m", "15m"} if daylight else {"40m", "30m", "20m"}
    elif distance_km < 1500:
        preferred = {"20m", "30m", "40m"} if daylight else {"80m", "40m", "30m"}
    elif distance_km < 5000:
        preferred = {"20m", "17m", "15m"} if daylight else {"40m", "30m", "20m"}
    else:
        preferred = {"20m", "17m", "15m", "12m", "10m"} if daylight else {"40m", "30m", "20m"}
    return 30 if band in preferred else 15
