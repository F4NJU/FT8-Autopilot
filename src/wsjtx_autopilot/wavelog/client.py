from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class WavelogApiError(RuntimeError):
    """A safe, token-free Wavelog API v2 error."""


class WavelogV2Client:
    """Minimal read-only client for Wavelog's explicit LoTW confirmations."""

    def __init__(
        self,
        api_url: str,
        api_token: str,
        *,
        timeout_seconds: float = 15.0,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self._base_url = self._normalize_base_url(api_url)
        self._api_token = api_token.strip()
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        raw = value.strip().rstrip("/")
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WavelogApiError("URL API Wavelog v2 invalide")
        path = parsed.path.rstrip("/")
        if not path.endswith("/api/v2"):
            path = f"{path}/index.php/api/v2" if path else "/index.php/api/v2"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def lotw_confirmation_callsigns(self) -> set[str]:
        if not self._api_token:
            raise WavelogApiError("Jeton API Wavelog v2 absent")
        callsigns: set[str] = set()
        page = 1
        while True:
            payload = self._get_json(
                "confirmation",
                {"type": "lotw", "per_page": 1000, "page": page},
            )
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise WavelogApiError("Réponse Wavelog invalide")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                callsign = str(row.get("callsign", "")).strip().upper()
                if callsign:
                    callsigns.add(callsign)
            meta = payload.get("meta")
            current_page = int(meta.get("current_page", page)) if isinstance(meta, dict) else page
            last_page = int(meta.get("last_page", current_page)) if isinstance(meta, dict) else current_page
            if current_page >= last_page or not rows:
                return callsigns
            page += 1
            if page > 10_000:
                raise WavelogApiError("Pagination Wavelog anormalement longue")

    def _get_json(self, endpoint: str, parameters: dict[str, object]) -> dict[str, Any]:
        request = Request(
            f"{self._base_url}/{endpoint}?{urlencode(parameters)}",
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Accept": "application/json",
                "User-Agent": "WSJTX-AutoPilot/1.0",
            },
        )
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
            raw = response.read()
        except HTTPError as exc:
            raise WavelogApiError(f"Wavelog HTTP {exc.code}") from exc
        except URLError as exc:
            raise WavelogApiError("Wavelog inaccessible") from exc
        except OSError as exc:
            raise WavelogApiError("Wavelog inaccessible") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WavelogApiError("Réponse Wavelog non lisible") from exc
        if not isinstance(payload, dict):
            raise WavelogApiError("Réponse Wavelog invalide")
        return payload
