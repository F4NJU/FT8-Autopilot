"""Small, conservative FT8 path-likelihood heuristic for MQTT guidance.

This is deliberately not a propagation forecast.  It ranks live PSK Reporter
spots so the receiver follows the paths most likely to be audible locally,
while a real WSJT-X Decode remains mandatory before any reply.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .location import distance_and_bearing, grid_to_location


# (near distance, useful DX distance, long-path limit), in km.  The long-path
# side is intentionally strict on the low bands: a global spot alone should
# not make a French station leave a productive band for an unlikely 40 m path.
_BAND_DISTANCE_WINDOWS: dict[str, tuple[int, int, int]] = {
    "80m": (100, 1_200, 3_000),
    "40m": (250, 2_000, 4_000),
    "30m": (500, 4_000, 8_000),
    "20m": (1_000, 6_000, 15_000),
    "17m": (1_500, 7_000, 16_000),
    "15m": (1_800, 8_000, 17_000),
    "12m": (2_000, 8_000, 18_000),
    "10m": (2_000, 8_000, 18_000),
    "6m": (300, 1_500, 4_000),
}


def mqtt_path_probability(
    local_grid: str,
    transmitter_grid: str,
    receiver_grid: str,
    band: str,
    observed_at: datetime,
    snr: int | None = None,
) -> int:
    """Return a conservative 0-100 local audibility score for one live spot."""
    local = grid_to_location(local_grid)
    transmitter = grid_to_location(transmitter_grid)
    if local is None or transmitter is None:
        return 0

    direct = distance_and_bearing(local, transmitter)
    if direct is None:
        return 0
    distance_km = direct[0]
    distance_score = _distance_score(distance_km, band)
    time_score = _band_time_score(local.longitude, transmitter.longitude, band, observed_at)
    path_score = round(distance_score * 0.60 + time_score * 0.40)

    # A station decoded close to the operator is the strongest practical
    # evidence available from a remote MQTT report.
    receiver = grid_to_location(receiver_grid)
    local_receiver_score = 0
    if receiver is not None:
        receiver_distance = distance_and_bearing(local, receiver)
        if receiver_distance is not None:
            local_receiver_score = _nearby_receiver_score(receiver_distance[0])

    snr_score = 0 if snr is None else max(0, min(100, (snr + 24) * 3))
    return max(0, min(100, round(path_score * 0.65 + local_receiver_score * 0.25 + snr_score * 0.10)))


def reply_path_probability(
    local_grid: str,
    remote_grid: str,
    band: str,
    observed_at: datetime,
    solar_flux: float | None = None,
    kp: float | None = None,
) -> int:
    """Return a 0-100 advisory score for a locally decoded ordinary CQ."""
    local = grid_to_location(local_grid)
    remote = grid_to_location(remote_grid)
    if local is None or remote is None:
        return 0
    direct = distance_and_bearing(local, remote)
    if direct is None:
        return 0
    normalized_band = band.strip().lower()
    base = round(
        _distance_score(direct[0], normalized_band) * 0.60
        + _band_time_score(local.longitude, remote.longitude, normalized_band, observed_at) * 0.40
    )
    solar_adjustment = _solar_adjustment(normalized_band, solar_flux)
    kp_penalty = 0 if kp is None else round(max(0.0, min(9.0, kp) - 2.0) * 5)
    return max(0, min(100, base + solar_adjustment - kp_penalty))


def _solar_adjustment(band: str, solar_flux: float | None) -> int:
    if solar_flux is None:
        return 0
    flux = max(50.0, min(300.0, solar_flux))
    if band in {"10m", "12m", "15m"}:
        return round(max(-15.0, min(20.0, (flux - 115.0) * 0.22)))
    if band in {"17m", "20m"}:
        return round(max(-8.0, min(12.0, (flux - 100.0) * 0.12)))
    return 0


def _distance_score(distance_km: int, band: str) -> int:
    near, useful, maximum = _BAND_DISTANCE_WINDOWS.get(band.strip().lower(), (500, 4_000, 10_000))
    if distance_km <= near:
        return round(45 + 55 * distance_km / max(1, near))
    if distance_km <= useful:
        return 100
    if distance_km >= maximum:
        return 0
    return round(100 * (maximum - distance_km) / max(1, maximum - useful))


def _nearby_receiver_score(distance_km: int) -> int:
    if distance_km <= 500:
        return 100
    if distance_km <= 1_000:
        return 85
    if distance_km <= 2_000:
        return 65
    if distance_km <= 4_000:
        return 30
    return 0


def _band_time_score(local_longitude: float, remote_longitude: float, band: str, observed_at: datetime) -> int:
    timestamp = observed_at.astimezone(timezone.utc)
    midpoint_longitude = (local_longitude + remote_longitude) / 2
    midpoint_hour = (timestamp.hour + timestamp.minute / 60 + midpoint_longitude / 15) % 24
    normalized_band = band.strip().lower()
    night = midpoint_hour >= 18 or midpoint_hour < 7
    shoulder = 16 <= midpoint_hour < 18 or 7 <= midpoint_hour < 9
    if normalized_band in {"80m", "40m"}:
        return 100 if night else (45 if shoulder else 10)
    if normalized_band == "30m":
        return 85 if night or shoulder else 55
    if normalized_band in {"20m", "17m"}:
        return 100 if 8 <= midpoint_hour < 18 else (45 if shoulder else 10)
    if normalized_band in {"15m", "12m", "10m"}:
        return 100 if 9 <= midpoint_hour < 17 else (35 if shoulder else 5)
    return 70
