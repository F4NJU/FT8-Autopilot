"""Optional Discord Rich Presence publisher for current FT8 activity."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from pypresence import Presence


LOGGER = logging.getLogger(__name__)
_RECONNECT_SECONDS = 15.0


class PresenceClient(Protocol):
    def connect(self) -> object: ...
    def update(self, **fields: object) -> object: ...
    def clear(self, pid: int = ...) -> object: ...
    def close(self) -> object: ...


@dataclass(frozen=True, slots=True)
class DiscordActivity:
    details: str
    state: str


def _presence_client(application_id: str) -> PresenceClient:
    return Presence(application_id, connection_timeout=1, response_timeout=2)


class DiscordPresencePublisher:
    """Publish activity off the radio worker thread and reconnect quietly."""

    def __init__(
        self,
        enabled: bool,
        application_id: str,
        client_factory: Callable[[str], PresenceClient] = _presence_client,
    ) -> None:
        self.enabled = enabled
        self.application_id = application_id.strip()
        self._client_factory = client_factory
        self._condition = threading.Condition()
        self._pending: DiscordActivity | None = None
        self._last_requested: DiscordActivity | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    def publish(
        self,
        mode: str | None,
        band: str | None,
        remote_callsign: str | None,
        connected: bool,
    ) -> None:
        if not self.enabled or not self.application_id:
            return
        activity = self._activity(mode, band, remote_callsign, connected)
        with self._condition:
            if self._stopping or activity == self._last_requested:
                return
            self._last_requested = activity
            self._pending = activity
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="discord-rich-presence",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    @staticmethod
    def _activity(
        mode: str | None,
        band: str | None,
        remote_callsign: str | None,
        connected: bool,
    ) -> DiscordActivity:
        mode_label = (mode or "WSJT-X").upper()
        band_label = (band or "unknown band").lower()
        details = f"{mode_label} - {band_label}"
        remote = (remote_callsign or "").strip().upper()
        if remote:
            state = f"Working {remote}"
        elif connected:
            state = f"Listening on {band_label}"
        else:
            state = "Waiting for WSJT-X"
        return DiscordActivity(details, state)

    def _run(self) -> None:
        client: PresenceClient | None = None
        last_sent: DiscordActivity | None = None
        retry_at = 0.0
        try:
            while True:
                with self._condition:
                    while not self._stopping:
                        now = time.monotonic()
                        retry_wait = max(0.0, retry_at - now)
                        if self._pending is not None and retry_wait <= 0:
                            break
                        self._condition.wait(timeout=retry_wait or None)
                    if self._stopping:
                        break
                    activity = self._pending
                    self._pending = None
                if activity is None:
                    continue
                if client is None:
                    try:
                        client = self._client_factory(self.application_id)
                        client.connect()
                        LOGGER.info("[DISCORD] Rich Presence connected application_id=%s", self.application_id)
                    except Exception as exc:
                        LOGGER.warning("[DISCORD] Rich Presence unavailable: %s", exc)
                        client = None
                        retry_at = time.monotonic() + _RECONNECT_SECONDS
                        with self._condition:
                            self._pending = self._pending or activity
                        continue
                if activity == last_sent:
                    continue
                try:
                    client.update(
                        pid=os.getpid(),
                        details=activity.details,
                        state=activity.state,
                    )
                except Exception as exc:
                    LOGGER.warning("[DISCORD] Rich Presence update failed: %s", exc)
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = None
                    retry_at = time.monotonic() + _RECONNECT_SECONDS
                    with self._condition:
                        self._pending = self._pending or activity
                    continue
                last_sent = activity
                retry_at = 0.0
                LOGGER.info("[DISCORD] activity details=%s state=%s", activity.details, activity.state)
        finally:
            if client is not None:
                try:
                    client.clear(pid=os.getpid())
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass

