import math
import re
from dataclasses import dataclass

from .models import StationMetadata

_GRID = re.compile(r"^[A-R]{2}\d{2}(?:[A-X]{2})?$")
_FT8_NON_GRID_TOKENS = frozenset({"RR73"})
_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True, slots=True)
class StationLocation:
    grid: str
    latitude: float
    longitude: float


class StationLocatorCache:
    """Latest valid Maidenhead locator learned from decoded FT8 traffic."""

    def __init__(self) -> None:
        self._locations: dict[str, StationLocation] = {}

    def note(self, callsign: str, grid: str | None) -> None:
        location = grid_to_location(grid) if grid is not None else None
        if location is None:
            return
        for form in _lookup_forms(callsign):
            self._locations[form] = location

    def get(self, callsign: str) -> StationLocation | None:
        return next((self._locations[form] for form in _lookup_forms(callsign) if form in self._locations), None)


def grid_to_location(grid: str | None) -> StationLocation | None:
    if grid is None:
        return None
    normalized = grid.strip().upper()
    if normalized in _FT8_NON_GRID_TOKENS or not _GRID.fullmatch(normalized):
        return None
    longitude = -180 + (ord(normalized[0]) - ord("A")) * 20 + int(normalized[2]) * 2
    latitude = -90 + (ord(normalized[1]) - ord("A")) * 10 + int(normalized[3])
    if len(normalized) == 4:
        longitude += 1
        latitude += 0.5
    else:
        longitude += (ord(normalized[4]) - ord("A")) * (2 / 24) + (1 / 24)
        latitude += (ord(normalized[5]) - ord("A")) * (1 / 24) + (1 / 48)
    return StationLocation(normalized, latitude, longitude)


def distance_and_bearing(
    local: StationLocation,
    remote: StationLocation | StationMetadata,
) -> tuple[int, int] | None:
    if isinstance(remote, StationMetadata):
        if remote.reference_latitude is None or remote.reference_longitude is None:
            return None
        remote_latitude, remote_longitude = remote.reference_latitude, remote.reference_longitude
    else:
        remote_latitude, remote_longitude = remote.latitude, remote.longitude
    local_latitude = math.radians(local.latitude)
    local_longitude = math.radians(local.longitude)
    target_latitude = math.radians(remote_latitude)
    target_longitude = math.radians(remote_longitude)
    delta_longitude = target_longitude - local_longitude
    haversine = (
        math.sin((target_latitude - local_latitude) / 2) ** 2
        + math.cos(local_latitude) * math.cos(target_latitude) * math.sin(delta_longitude / 2) ** 2
    )
    distance = round(_EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(haversine)))
    bearing = math.degrees(
        math.atan2(
            math.sin(delta_longitude) * math.cos(target_latitude),
            math.cos(local_latitude) * math.sin(target_latitude)
            - math.sin(local_latitude) * math.cos(target_latitude) * math.cos(delta_longitude),
        )
    )
    return distance, round((bearing + 360) % 360)


def _lookup_forms(callsign: str) -> tuple[str, ...]:
    normalized = callsign.strip().upper()
    if "/" not in normalized:
        return (normalized,)
    parts = tuple(part for part in normalized.split("/") if part)
    base_calls = tuple(part for part in parts if any(char.isdigit() for char in part))
    return tuple(dict.fromkeys((normalized, *base_calls, *parts)))
