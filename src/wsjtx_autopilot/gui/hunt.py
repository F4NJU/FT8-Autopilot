from __future__ import annotations

from datetime import datetime, timezone
import socket
import ssl
import struct
from threading import Lock
import time
from uuid import uuid4

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.special_calls import (
    AfuToolsProvider,
    SpecialCallActivity,
    SpecialCallCatalog,
    SpecialCallProviderError,
    SpecialCallSnapshot,
    PskReporterSpot,
    band_subscription_topic,
    parse_spot,
    subscription_topic,
)
from wsjtx_autopilot.wavelog import WavelogApiError, WavelogLotwCatalog, WavelogV2Client
from wsjtx_autopilot.engine.space_weather import NoaaSpaceWeatherClient


class SpecialCallPoller(QThread):
    completed = Signal(object)

    def __init__(self, catalog: SpecialCallCatalog, api_key: str, lookback_minutes: int, allowed_bands: list[str], local_grid: str = "") -> None:
        super().__init__()
        self._catalog = catalog
        self._api_key = api_key
        self._lookback_minutes = lookback_minutes
        self._allowed_bands = list(allowed_bands)
        self._local_grid = local_grid

    def run(self) -> None:
        try:
            snapshot = self._catalog.refresh(
                AfuToolsProvider(
                    self._api_key,
                    lookback_minutes=self._lookback_minutes,
                    timeout_seconds=5.0,
                    cancelled=self.isInterruptionRequested,
                    allowed_bands=self._allowed_bands,
                ),
                self._allowed_bands,
                self._local_grid,
            )
        except SpecialCallProviderError as exc:
            snapshot = self._catalog.load_cache(self._allowed_bands, str(exc), self._local_grid)
        except Exception as exc:  # keep any provider defect away from radio control
            snapshot = self._catalog.load_cache(self._allowed_bands, f"Actualisation impossible : {exc}", self._local_grid)
        self.completed.emit(snapshot)


class WavelogLotwPoller(QThread):
    """Refresh LoTW confirmation data away from both GUI and radio threads."""

    completed = Signal(object)

    def __init__(self, catalog: WavelogLotwCatalog, api_url: str, api_token: str) -> None:
        super().__init__()
        self._catalog = catalog
        self._api_url = api_url
        self._api_token = api_token

    def run(self) -> None:
        try:
            # Keep shutdown bounded if the remote Wavelog server is unreachable.
            snapshot = self._catalog.refresh(
                WavelogV2Client(self._api_url, self._api_token, timeout_seconds=8.0)
            )
        except WavelogApiError as exc:
            snapshot = self._catalog.load_cache(str(exc))
        except Exception:  # external service errors must never affect radio control
            snapshot = self._catalog.load_cache("Actualisation Wavelog impossible")
        self.completed.emit(snapshot)


class SpaceWeatherPoller(QThread):
    """Fetch advisory NOAA values outside both GUI and radio-control loops."""

    completed = Signal(object)

    def run(self) -> None:
        self.completed.emit(NoaaSpaceWeatherClient(timeout_seconds=5.0).refresh())


class PskReporterMqttPoller(QThread):
    """Subscribe to targeted PSK Reporter FT8 spots without blocking the GUI.

    Exact special-call subscriptions are low-volume.  Band discovery is a
    global feed, so it is deliberately reduced before crossing into Qt: the
    engine receives at most one discovery sample every two seconds.  This
    keeps CAT/SWR polling responsive even during busy FT8 periods.
    """

    spot_received = Signal(object)
    discovery_spot_received = Signal(object)
    state_changed = Signal(str)

    def __init__(self, callsigns: object = (), discovery_bands: object = ()) -> None:
        super().__init__()
        self._lock = Lock()
        self._callsigns = self._normalize_callsigns(callsigns)
        self._discovery_bands = self._normalize_bands(discovery_bands)
        self._generation = 0

    def set_callsigns(self, callsigns: object) -> None:
        normalized = self._normalize_callsigns(callsigns)
        with self._lock:
            if normalized == self._callsigns:
                return
            self._callsigns = normalized
            self._generation += 1

    def set_discovery_bands(self, bands: object) -> None:
        normalized = self._normalize_bands(bands)
        with self._lock:
            if normalized == self._discovery_bands:
                return
            self._discovery_bands = normalized
            self._generation += 1

    @staticmethod
    def _normalize_callsigns(callsigns: object) -> tuple[str, ...]:
        if not isinstance(callsigns, (tuple, list, set, frozenset)):
            return ()
        result = []
        for value in callsigns:
            normalized = str(value).strip().upper()
            try:
                subscription_topic(normalized)
            except ValueError:
                continue
            result.append(normalized)
        return tuple(sorted(set(result)))

    @staticmethod
    def _normalize_bands(bands: object) -> tuple[str, ...]:
        if not isinstance(bands, (tuple, list, set, frozenset)):
            return ()
        result = []
        for value in bands:
            try:
                normalized = str(value).strip().lower()
                band_subscription_topic(normalized)
            except ValueError:
                continue
            result.append(normalized)
        return tuple(sorted(set(result)))

    def _subscription_snapshot(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        with self._lock:
            return self._generation, self._callsigns, self._discovery_bands

    def run(self) -> None:
        from wsjtx_autopilot.special_calls.psk_mqtt import PSK_REPORTER_HOST, PSK_REPORTER_TLS_PORT
        sock: socket.socket | None = None
        try:
            raw = socket.create_connection((PSK_REPORTER_HOST, PSK_REPORTER_TLS_PORT), timeout=10)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=PSK_REPORTER_HOST)
            sock.settimeout(0.25)
            client_id = f"wsjtx-autopilot-{uuid4().hex[:12]}".encode()
            _mqtt_send(sock, 0x10, _mqtt_utf8(b"MQTT") + b"\x04\x02\x00<" + _mqtt_utf8(client_id))
            packet_type, _payload = _mqtt_read(sock, timeout=5)
            if packet_type != 0x20 or len(_payload) != 2 or _payload[1] != 0:
                raise OSError("connexion MQTT refusée")
            generation = -1
            packet_id = 0
            subscribed: set[str] = set()
            last_ping = time.monotonic()
            last_discovery_emit = 0.0
            pending_discovery: PskReporterSpot | None = None
            self.state_changed.emit("MQTT PSK Reporter connecté")
            while not self.isInterruptionRequested():
                current_generation, callsigns, discovery_bands = self._subscription_snapshot()
                if current_generation != generation:
                    generation = current_generation
                    desired = {subscription_topic(callsign) for callsign in callsigns}
                    desired.update(band_subscription_topic(band) for band in discovery_bands)
                    for topic in sorted(subscribed - desired):
                        packet_id = (packet_id % 65535) + 1
                        _mqtt_send(sock, 0xA2, struct.pack("!H", packet_id) + _mqtt_utf8(topic.encode()))
                    for topic in sorted(desired - subscribed):
                        packet_id = (packet_id % 65535) + 1
                        _mqtt_send(sock, 0x82, struct.pack("!H", packet_id) + _mqtt_utf8(topic.encode()) + b"\x00")
                    subscribed = desired
                try:
                    packet_type, payload = _mqtt_read(sock)
                except TimeoutError:
                    packet_type, payload = 0, b""
                if packet_type == 0x30 and len(payload) >= 2:
                    topic_size = struct.unpack("!H", payload[:2])[0]
                    topic = payload[2:2 + topic_size].decode("utf-8", "replace")
                    fields = topic.split("/")
                    expected = fields[5] if len(fields) > 5 else ""
                    spot = parse_spot(payload[2 + topic_size:], expected)
                    if spot is not None:
                        # Do not send every global band-discovery spot to the
                        # special-call view.  That used to flood the GUI and
                        # delay the Hamlib safety polling.
                        if spot.callsign in callsigns:
                            self.spot_received.emit(spot)
                        if spot.band in discovery_bands:
                            pending_discovery = spot
                now = time.monotonic()
                if pending_discovery is not None and now - last_discovery_emit >= 2.0:
                    self.discovery_spot_received.emit(pending_discovery)
                    pending_discovery = None
                    last_discovery_emit = now
                if now - last_ping >= 45:
                    _mqtt_send(sock, 0xC0, b"")
                    last_ping = now
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.state_changed.emit(f"MQTT indisponible : {exc}")
        finally:
            if sock is not None:
                try:
                    _mqtt_send(sock, 0xE0, b"")
                    sock.close()
                except OSError:
                    pass
            self.state_changed.emit("MQTT arrêté")


def _mqtt_utf8(value: bytes) -> bytes:
    return struct.pack("!H", len(value)) + value


def _mqtt_send(sock: socket.socket, packet_type: int, payload: bytes) -> None:
    size = len(payload)
    encoded = bytearray()
    while True:
        digit = size % 128
        size //= 128
        encoded.append(digit | (0x80 if size else 0))
        if not size:
            break
    sock.sendall(bytes((packet_type,)) + bytes(encoded) + payload)


def _mqtt_read(sock: socket.socket, timeout: float | None = None) -> tuple[int, bytes]:
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        first = sock.recv(1)
    except socket.timeout as exc:
        raise TimeoutError from exc
    if not first:
        raise OSError("connexion MQTT fermée")
    multiplier, size = 1, 0
    while True:
        byte = sock.recv(1)
        if not byte:
            raise OSError("connexion MQTT fermée")
        size += (byte[0] & 127) * multiplier
        if not byte[0] & 128:
            break
        multiplier *= 128
    data = bytearray()
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except socket.timeout as exc:
            raise TimeoutError from exc
        if not chunk:
            raise OSError("connexion MQTT fermée")
        data.extend(chunk)
    return first[0] & 0xF0, bytes(data)


class HuntPanel(QWidget):
    refresh_requested = Signal()
    target_requested = Signal(str)
    hunt_all_requested = Signal(object)
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._snapshot = SpecialCallSnapshot(datetime.now(timezone.utc))
        self._locally_decoded: dict[str, datetime] = {}
        self._remote_spots: dict[str, PskReporterSpot] = {}
        self._mqtt_state = "MQTT désactivé"

        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        title = QLabel("INDICATIFS SPÉCIAUX ACTIFS EN FT8")
        title.setObjectName("panelTitle")
        self.filter_country = QComboBox()
        self.filter_country.addItem("Tous les pays", "all")
        self.filter_country.addItem("France uniquement", "france")
        self.filter_country.currentIndexChanged.connect(self._render)
        self.refresh_button = QPushButton("ACTUALISER")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.hunt_button = QPushButton("HUNT TOUS")
        self.hunt_button.clicked.connect(self._start_all)
        self.stop_button = QPushButton("STOP HUNT")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.filter_country)
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.hunt_button)
        toolbar.addWidget(self.stop_button)
        layout.addLayout(toolbar)

        self.status = QLabel("En attente de la première actualisation.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["CALL", "PAYS", "STATUT", "BANDE", "CHEMIN", "HEURES UTC", "DERNIÈRE ACTIVITÉ", "FIN"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for column in range(8):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._select_row)
        layout.addWidget(self.table, 1)

        self.details = QLabel(
            "Double-cliquer une station pour la surveiller. Aucun appel n'est émis avant un Decode WSJT-X local récent."
        )
        self.details.setWordWrap(True)
        layout.addWidget(self.details)

    def set_snapshot(self, snapshot: SpecialCallSnapshot) -> None:
        self._snapshot = snapshot
        self._render()
        stamp = snapshot.generated_at.astimezone(timezone.utc).strftime("%H:%M UTC")
        stale = " — CACHE" if snapshot.stale else ""
        warning = f" — {snapshot.warning}" if snapshot.warning else ""
        self.status.setText(
            f"{len(snapshot.activities)} station(s) FT8 vérifiée(s) — mise à jour {stamp}{stale}{warning}"
            + (f" — {snapshot.attribution}" if snapshot.attribution else "")
        )

    def note_local_decode(self, callsign: str, observed_at: datetime) -> None:
        normalized = callsign.strip().upper()
        if normalized and any(item.callsign == normalized for item in self._snapshot.activities):
            self._locally_decoded[normalized] = observed_at
            self._render()

    def note_remote_spot(self, spot: PskReporterSpot) -> None:
        if not any(item.callsign == spot.callsign for item in self._snapshot.activities):
            return
        previous = self._remote_spots.get(spot.callsign)
        if previous is not None and previous.observed_at >= spot.observed_at:
            return
        self._remote_spots[spot.callsign] = spot
        self._render()

    def set_mqtt_state(self, text: str) -> None:
        self._mqtt_state = text
        self._render()

    def activities(self) -> tuple[SpecialCallActivity, ...]:
        return self._filtered()

    def all_callsigns(self) -> tuple[str, ...]:
        return tuple(item.callsign for item in self._snapshot.activities)

    def set_hunting(self, hunting: bool, target: str = "") -> None:
        self.hunt_button.setEnabled(not hunting and bool(self._filtered()))
        self.stop_button.setEnabled(hunting)
        if hunting:
            self.details.setText(
                f"Hunt actif{f' — cible {target}' if target else ''}. Appel uniquement après Decode local ; appels directs prioritaires."
            )
        else:
            self.details.setText(
                "Double-cliquer une station pour la surveiller. Aucun appel n'est émis avant un Decode WSJT-X local récent."
            )

    def _filtered(self) -> tuple[SpecialCallActivity, ...]:
        activities = self._snapshot.activities
        if self.filter_country.currentData() == "france":
            activities = tuple(item for item in activities if item.is_french)
        return activities

    def _render(self) -> None:
        activities = self._filtered()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for activity in activities:
            row = self.table.rowCount()
            self.table.insertRow(row)
            local = activity.callsign in self._locally_decoded
            spot = self._remote_spots.get(activity.callsign)
            if local:
                status = "DÉCODÉ ICI"
            elif spot is not None:
                age = max(0, int((datetime.now(timezone.utc) - spot.observed_at).total_seconds() // 60))
                status = f"VU MQTT {spot.band} · {age} min"
            else:
                status = "FT8 CONFIRMÉ"
            values = (
                activity.callsign,
                activity.country or activity.dxcc or "-",
                status,
                activity.best_band or ", ".join(activity.bands) or "-",
                f"{activity.path_score:03d} / {activity.confidence}",
                ", ".join(f"{hour:02d}h" for hour in activity.active_hours_utc) or "-",
                activity.last_ft8_seen.astimezone(timezone.utc).strftime("%d/%m %H:%M") if activity.last_ft8_seen else "-",
                activity.end.astimezone(timezone.utc).strftime("%d/%m/%Y") if activity.end else "-",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, activity.callsign)
                item.setToolTip("\n".join(activity.evidence) or "Activité FT8 vérifiée")
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.hunt_button.setEnabled(bool(activities) and not self.stop_button.isEnabled())
        self.details.setToolTip(self._mqtt_state)

    def _select_row(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is None:
            return
        callsign = str(item.data(Qt.ItemDataRole.UserRole) or item.text()).upper()
        self.target_requested.emit(callsign)

    def _start_all(self) -> None:
        activities = self._filtered()
        if not activities:
            return
        self.hunt_all_requested.emit(activities)

    def _stop(self) -> None:
        self.stop_requested.emit()
