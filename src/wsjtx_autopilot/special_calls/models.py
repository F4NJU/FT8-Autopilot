from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SpecialCallActivity:
    callsign: str
    country: str
    dxcc: str
    continent: str
    start: datetime | None
    end: datetime | None
    bands: tuple[str, ...]
    active_hours_utc: tuple[int, ...]
    last_ft8_seen: datetime | None
    sources: tuple[str, ...]
    evidence: tuple[str, ...]
    latitude: float | None = None
    longitude: float | None = None
    path_score: int = 0
    confidence: str = "faible"
    best_band: str | None = None

    @property
    def is_french(self) -> bool:
        entity = self.country.casefold()
        return self.dxcc.upper() in {"F", "TK"} or entity in {"france", "corsica", "corse"}

    @property
    def ft8_verified(self) -> bool:
        return bool(self.last_ft8_seen is not None or any("FT8" in value.upper() for value in self.evidence))

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["start"] = self.start.isoformat() if self.start else None
        payload["end"] = self.end.isoformat() if self.end else None
        payload["last_ft8_seen"] = self.last_ft8_seen.isoformat() if self.last_ft8_seen else None
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> "SpecialCallActivity":
        def parse_datetime(key: str) -> datetime | None:
            value = payload.get(key)
            return datetime.fromisoformat(str(value)) if value else None

        return cls(
            callsign=str(payload.get("callsign", "")).upper(),
            country=str(payload.get("country", "")),
            dxcc=str(payload.get("dxcc", "")),
            continent=str(payload.get("continent", "")),
            start=parse_datetime("start"),
            end=parse_datetime("end"),
            bands=tuple(str(value).lower() for value in payload.get("bands", []) if value),
            active_hours_utc=tuple(int(value) for value in payload.get("active_hours_utc", []) if isinstance(value, int)),
            last_ft8_seen=parse_datetime("last_ft8_seen"),
            sources=tuple(str(value) for value in payload.get("sources", []) if value),
            evidence=tuple(str(value) for value in payload.get("evidence", []) if value),
            latitude=float(payload["latitude"]) if isinstance(payload.get("latitude"), (int, float)) else None,
            longitude=float(payload["longitude"]) if isinstance(payload.get("longitude"), (int, float)) else None,
            path_score=int(payload.get("path_score", 0)),
            confidence=str(payload.get("confidence", "faible")),
            best_band=str(payload["best_band"]).lower() if payload.get("best_band") else None,
        )


@dataclass(frozen=True, slots=True)
class SpecialCallSnapshot:
    generated_at: datetime
    activities: tuple[SpecialCallActivity, ...] = field(default_factory=tuple)
    attribution: str = ""
    stale: bool = False
    warning: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "activities": [activity.to_json() for activity in self.activities],
            "attribution": self.attribution,
            "stale": self.stale,
            "warning": self.warning,
        }

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> "SpecialCallSnapshot":
        raw_activities = payload.get("activities", [])
        activities = tuple(
            SpecialCallActivity.from_json(value)
            for value in raw_activities
            if isinstance(value, dict)
        ) if isinstance(raw_activities, list) else ()
        generated = payload.get("generated_at")
        return cls(
            datetime.fromisoformat(str(generated)) if generated else datetime.min,
            activities,
            str(payload.get("attribution", "")),
            bool(payload.get("stale", False)),
            str(payload.get("warning", "")),
        )
