"""Small, failure-tolerant NOAA space-weather snapshot for Reply ranking.

It is deliberately advisory: failure to obtain a snapshot must never affect
radio control or make a decoded CQ ineligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Callable
from urllib.request import Request, urlopen


SOLAR_FLUX_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"
PLANETARY_K_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"


@dataclass(frozen=True, slots=True)
class SpaceWeatherSnapshot:
    solar_flux: float | None = None
    kp: float | None = None
    fetched_at: datetime | None = None
    warning: str = ""


class NoaaSpaceWeatherClient:
    """Fetch only the latest usable F10.7 and planetary K values."""

    def __init__(self, timeout_seconds: float = 5.0, fetch: Callable[[str, float], object] | None = None) -> None:
        self._timeout_seconds = timeout_seconds
        self._fetch = fetch or _fetch_json

    def refresh(self) -> SpaceWeatherSnapshot:
        try:
            solar_flux = _latest_number(self._fetch(SOLAR_FLUX_URL, self._timeout_seconds), ("flux", "observed_flux", "f107", "f10_7"))
            kp = _latest_number(self._fetch(PLANETARY_K_URL, self._timeout_seconds), ("kp_index", "kp"))
        except Exception as exc:
            return SpaceWeatherSnapshot(warning=f"NOAA indisponible : {exc}")
        if solar_flux is None and kp is None:
            return SpaceWeatherSnapshot(warning="NOAA : aucune mesure exploitable")
        return SpaceWeatherSnapshot(solar_flux=solar_flux, kp=kp, fetched_at=datetime.now(timezone.utc))


def _fetch_json(url: str, timeout_seconds: float) -> object:
    request = Request(url, headers={"User-Agent": "WSJTX-AutoPilot/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed public NOAA endpoints
        return json.loads(response.read().decode("utf-8"))


def _latest_number(payload: object, keys: tuple[str, ...]) -> float | None:
    rows = payload if isinstance(payload, list) else [payload]
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        normalized = {str(key).strip().lower().replace("-", "_"): value for key, value in row.items()}
        for key in keys:
            try:
                return float(normalized.get(key))
            except (TypeError, ValueError):
                continue
    return None
