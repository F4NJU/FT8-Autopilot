from __future__ import annotations

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import SpecialCallActivity, SpecialCallSnapshot


class SpecialCallProviderError(RuntimeError):
    pass


def _utc_datetime(value: object, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if len(text) == 10:
        parsed = datetime.combine(parsed.date(), time.max if end_of_day else time.min)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AfuToolsProvider:
    """Cross-check announced DX activity with cluster and PSK Reporter evidence."""

    BASE_URL = "https://afu.tools/api/v1"

    def __init__(
        self,
        api_key: str,
        *,
        lookback_minutes: int = 1440,
        timeout_seconds: float = 12.0,
        opener: Callable[..., object] = urlopen,
        cancelled: Callable[[], bool] | None = None,
        allowed_bands: tuple[str, ...] | list[str] = (),
    ) -> None:
        self.api_key = api_key.strip()
        self.lookback_minutes = max(5, min(2880, int(lookback_minutes)))
        self.timeout_seconds = timeout_seconds
        self._opener = opener
        self._cancelled = cancelled or (lambda: False)
        self.allowed_bands = tuple(dict.fromkeys(str(band).strip().lower() for band in allowed_bands if band))

    def fetch(self, now: datetime | None = None) -> SpecialCallSnapshot:
        if not self.api_key:
            raise SpecialCallProviderError("Clé API afu.tools absente")
        if self._cancelled():
            raise SpecialCallProviderError("Actualisation annulée")
        current = now or datetime.now(timezone.utc)
        announced = self._get_json("dx", {"days": 90, "art": "aktivitaet", "limit": 1000})
        if self._cancelled():
            raise SpecialCallProviderError("Actualisation annulée")
        spotted = self._get_json(
            "dx/spots",
            {"minuten": self.lookback_minutes, "limit": 1000, "quelle": "cluster,dxcache"},
        )
        schedules = {
            str(item.get("call", "")).strip().upper(): item
            for item in announced.get("dxpeditions", [])
            if isinstance(item, dict) and item.get("call")
        }
        evidence: dict[str, list[dict[str, object]]] = defaultdict(list)
        for item in spotted.get("spots", []):
            if not isinstance(item, dict):
                continue
            mode = str(item.get("modus") or item.get("modus_vermutet") or "").upper()
            callsign = str(item.get("call", "")).strip().upper()
            if mode == "FT8" and callsign in schedules:
                evidence[callsign].append(item)

        missing = [
            callsign
            for callsign, item in schedules.items()
            if callsign not in evidence and self._schedule_is_current(item, current)
        ]
        if missing and not self._cancelled():
            workers = min(8, len(missing))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="afu-ft8") as executor:
                futures = {executor.submit(self._fetch_ft8_heard, callsign): callsign for callsign in missing}
                for future in as_completed(futures):
                    if self._cancelled():
                        for pending in futures:
                            pending.cancel()
                        break
                    callsign = futures[future]
                    try:
                        heard_spots = future.result()
                        if heard_spots:
                            evidence[callsign].extend(heard_spots)
                    except SpecialCallProviderError:
                        # A single callsign lookup must not discard the whole catalog.
                        continue

        activities: list[SpecialCallActivity] = []
        for callsign, spots in evidence.items():
            schedule_item = schedules[callsign]
            seen = tuple(filter(None, (_utc_datetime(item.get("zeit")) for item in spots)))
            bands = tuple(dict.fromkeys(str(item.get("band", "")).lower() for item in spots if item.get("band")))
            hours = tuple(sorted({value.hour for value in seen}))
            source_names = {"afu.tools"}
            source_names.update(str(item.get("quelle")) for item in spots if item.get("quelle"))
            for source in schedule_item.get("quellen", []):
                if isinstance(source, dict) and source.get("name"):
                    source_names.add(str(source["name"]))
            activities.append(
                SpecialCallActivity(
                    callsign=callsign,
                    country=str(schedule_item.get("entity") or spots[0].get("entitaet") or "Inconnu"),
                    dxcc=str(schedule_item.get("dxcc") or spots[0].get("dxcc") or ""),
                    continent=str(schedule_item.get("cont") or spots[0].get("kontinent") or ""),
                    start=_utc_datetime(schedule_item.get("start")),
                    end=_utc_datetime(schedule_item.get("end"), end_of_day=True),
                    bands=bands,
                    active_hours_utc=hours,
                    last_ft8_seen=max(seen) if seen else None,
                    sources=tuple(sorted(source_names)),
                    evidence=tuple(
                        f"FT8 {item.get('band', '?')} {item.get('zeit', '?')} ({item.get('quelle', 'cluster')})"
                        for item in sorted(spots, key=lambda value: str(value.get("zeit", "")), reverse=True)[:5]
                    ),
                    latitude=float(schedule_item["lat"]) if isinstance(schedule_item.get("lat"), (int, float)) else None,
                    longitude=float(schedule_item["lon"]) if isinstance(schedule_item.get("lon"), (int, float)) else None,
                )
            )
        attribution = str(spotted.get("quellenangabe", {}).get("nennung", "Sources : afu.tools"))
        return SpecialCallSnapshot(current, tuple(activities), attribution)

    @staticmethod
    def _schedule_is_current(item: dict[str, object], current: datetime) -> bool:
        start = _utc_datetime(item.get("start"))
        end = _utc_datetime(item.get("end"), end_of_day=True)
        return (start is None or start <= current) and (end is None or end >= current)

    def _fetch_ft8_heard(self, callsign: str) -> list[dict[str, object]]:
        if self._cancelled():
            return []
        parameters: dict[str, object] = {
            "minuten": self.lookback_minutes,
            "quelle": "psk,rbndigi",
            "modus": "FT8",
            "limit": 50,
        }
        if self.allowed_bands:
            parameters["band"] = ",".join(self.allowed_bands)
        payload = self._get_json(f"dx/gehoert/{quote(callsign, safe='')}", parameters)
        result: list[dict[str, object]] = []
        for item in payload.get("spots", []):
            if not isinstance(item, dict):
                continue
            mode = str(item.get("modus") or "").upper()
            if mode != "FT8":
                continue
            enriched = dict(item)
            enriched["call"] = callsign
            result.append(enriched)
        return result

    def _get_json(self, endpoint: str, parameters: dict[str, object]) -> dict[str, object]:
        if self._cancelled():
            raise SpecialCallProviderError("Actualisation annulée")
        url = f"{self.BASE_URL}/{endpoint}?{urlencode(parameters)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "WSJTX-AutoPilot/0.1",
            },
        )
        try:
            response = self._opener(request, timeout=self.timeout_seconds)
            try:
                raw = response.read()
                if getattr(response, "headers", {}).get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8"))
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as exc:
            raise SpecialCallProviderError(f"afu.tools HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SpecialCallProviderError(f"afu.tools indisponible : {exc}") from exc
        if not isinstance(payload, dict):
            raise SpecialCallProviderError("Réponse afu.tools invalide")
        return payload
