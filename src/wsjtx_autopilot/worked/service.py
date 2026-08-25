import logging
from datetime import date, datetime, timezone

from wsjtx_autopilot.engine.models import WorkedCheck
from wsjtx_autopilot.wsjtx.models import QsoLoggedPacket

from .bands import BandResolver
from .store import WorkedQsoStore, normalize_callsign

LOGGER = logging.getLogger(__name__)


class WorkedTodayService:
    def __init__(self, store: WorkedQsoStore, bands: BandResolver | None = None) -> None:
        self.store = store
        self.bands = bands or BandResolver()
        self._cache_date: date | None = None
        self._keys: set[tuple[str, str]] = set()

    def check(self, callsign: str, frequency_hz: int | None, observed_at: datetime) -> WorkedCheck:
        if frequency_hz is None:
            return WorkedCheck(None, False)
        band = self.bands.resolve(frequency_hz)
        if band is None:
            return WorkedCheck(None, False)
        qso_date = _utc_date(observed_at)
        self._load_date(qso_date)
        normalized = normalize_callsign(callsign)
        duplicate = (normalized, band) in self._keys
        if duplicate:
            LOGGER.info("[WORKED] duplicate %s band=%s date=%s", normalized, band, qso_date.isoformat())
        return WorkedCheck(band, duplicate)

    def record_qso_logged(self, packet: QsoLoggedPacket) -> bool:
        band = self.bands.resolve(packet.tx_frequency)
        if band is None:
            LOGGER.warning(
                "[WORKED] QSO not recorded station=%s reason=unknown band frequency=%d",
                packet.dx_call,
                packet.tx_frequency,
            )
            return False
        qso_date = _utc_date(packet.time_off)
        inserted = self.store.record(
            qso_date,
            packet.dx_call,
            band,
            packet.mode,
            packet.tx_frequency,
            _as_utc(packet.time_off),
            "WSJTX_QSOLOGGED",
        )
        if self._cache_date == qso_date:
            self._keys.add((normalize_callsign(packet.dx_call), band))
        if inserted:
            LOGGER.info(
                "[WORKED] recorded %s band=%s date=%s source=WSJTX_QSOLOGGED",
                normalize_callsign(packet.dx_call),
                band,
                qso_date.isoformat(),
            )
        return inserted

    def count(self, qso_date: date) -> int:
        return self.store.count_for_date(qso_date)

    def refresh(self) -> None:
        self._cache_date = None
        self._keys.clear()

    def _load_date(self, qso_date: date) -> None:
        if self._cache_date != qso_date:
            self._keys = self.store.keys_for_date(qso_date)
            self._cache_date = qso_date


def _utc_date(value: datetime) -> date:
    return _as_utc(value).date()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
