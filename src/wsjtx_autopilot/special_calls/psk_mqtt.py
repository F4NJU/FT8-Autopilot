"""Small, testable PSK Reporter MQTT protocol helpers.

Remote PSK Reporter receptions are advisory: they can guide a Hunt receiver to
the appropriate band but can never create a transmit candidate by themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PSK_REPORTER_HOST = "mqtt.pskreporter.info"
PSK_REPORTER_TLS_PORT = 1884


@dataclass(frozen=True, slots=True)
class PskReporterSpot:
    callsign: str
    band: str
    observed_at: datetime
    frequency_hz: int | None = None
    snr: int | None = None
    receiver: str = ""
    transmitter_grid: str = ""
    receiver_grid: str = ""


def subscription_topic(callsign: str) -> str:
    """Return the official FT8 topic for a transmitting callsign."""
    normalized = callsign.strip().upper()
    if not normalized or "/" in normalized or "#" in normalized or "+" in normalized:
        raise ValueError("invalid callsign for PSK Reporter topic")
    return f"pskr/filter/v2/+/FT8/{normalized}/#"


def band_subscription_topic(band: str) -> str:
    """Return the FT8 topic for all transmitting stations on one band."""
    normalized = band.strip().lower()
    if not normalized or not normalized.endswith("m") or not normalized[:-1].isdigit():
        raise ValueError("invalid band for PSK Reporter topic")
    return f"pskr/filter/v2/{normalized}/FT8/+/#"


def parse_spot(payload: bytes | str, expected_callsign: str = "") -> PskReporterSpot | None:
    """Parse one PSK Reporter MQTT JSON message without trusting its topic."""
    try:
        raw: Any = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    callsign = str(raw.get("sc", "")).strip().upper()
    expected = expected_callsign.strip().upper()
    if not callsign or (expected and callsign != expected):
        return None
    if str(raw.get("md", "")).strip().upper() != "FT8":
        return None
    band = str(raw.get("b", "")).strip().lower()
    if not band:
        return None
    timestamp = raw.get("t_tx", raw.get("t"))
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    try:
        frequency = int(raw["f"]) if raw.get("f") is not None else None
        snr = int(raw["rp"]) if raw.get("rp") is not None else None
    except (TypeError, ValueError):
        return None
    return PskReporterSpot(
        callsign=callsign,
        band=band,
        observed_at=datetime.fromtimestamp(timestamp, timezone.utc),
        frequency_hz=frequency,
        snr=snr,
        receiver=str(raw.get("rc", "")).strip().upper(),
        transmitter_grid=str(raw.get("sl", "")).strip().upper(),
        receiver_grid=str(raw.get("rl", "")).strip().upper(),
    )
