import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from .bands import BandResolver
from .store import WorkedQsoStore

LOGGER = logging.getLogger(__name__)
_EOR = re.compile(r"<EOR\s*>", re.IGNORECASE)
_FIELD = re.compile(r"<([A-Z0-9_]+):(\d+)(?::[^>]*)?>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ImportResult:
    records_seen: int
    records_added: int
    records_existing: int


def import_adif(
    path: Path,
    store: WorkedQsoStore,
    bands: BandResolver | None = None,
    source: str = "WSJTX_ADIF",
) -> ImportResult:
    resolver = bands or BandResolver()
    text = path.read_text(encoding="utf-8", errors="replace")
    seen = 0
    added = 0
    existing = 0
    for raw_record in _EOR.split(text):
        fields = _parse_fields(raw_record)
        if not fields or "CALL" not in fields or "QSO_DATE" not in fields:
            continue
        seen += 1
        try:
            qso_date = datetime.strptime(fields["QSO_DATE"].strip(), "%Y%m%d").date()
        except ValueError:
            LOGGER.warning("[WORKED] skipped ADIF record with invalid QSO_DATE=%r", fields["QSO_DATE"])
            continue

        frequency_hz = _frequency_hz(fields.get("FREQ"))
        band = resolver.normalize(fields.get("BAND", ""))
        if band is None and frequency_hz is not None:
            band = resolver.resolve(frequency_hz)
        if band is None:
            LOGGER.warning("[WORKED] skipped ADIF station=%s reason=unknown band", fields["CALL"].strip())
            continue

        logged_at = datetime.combine(qso_date, _adif_time(fields.get("TIME_ON") or fields.get("TIME_OFF")), timezone.utc)
        if store.record(
            qso_date,
            fields["CALL"],
            band,
            fields.get("SUBMODE") or fields.get("MODE", ""),
            frequency_hz or 0,
            logged_at,
            source,
        ):
            added += 1
        else:
            existing += 1
    LOGGER.info(
        "[ADIF] source=%s path=%s imported=%d existing=%d records=%d",
        source,
        path,
        added,
        existing,
        seen,
    )
    return ImportResult(seen, added, existing)


def detect_wsjtx_log(local_app_data: Path | None = None) -> tuple[Path | None, list[Path]]:
    """Return an unambiguous Windows WSJT-X log path and all candidates."""
    root = local_app_data
    if root is None:
        value = os.environ.get("LOCALAPPDATA")
        root = Path(value) if value else None
    if root is None or not root.is_dir():
        return None, []
    candidates = sorted(
        {path for directory in root.glob("WSJT-X*") if directory.is_dir() for path in [directory / "wsjtx_log.adi"] if path.is_file()}
    )
    return (candidates[0] if len(candidates) == 1 else None), candidates


def _parse_fields(record: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in _FIELD.finditer(record):
        length = int(match.group(2))
        value = record[match.end() : match.end() + length]
        if len(value) == length:
            fields[match.group(1).upper()] = value
    return fields


def _frequency_hz(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return round(float(value.strip()) * 1_000_000)
    except ValueError:
        return None


def _adif_time(value: str | None) -> time:
    if not value:
        return time()
    digits = value.strip().split(".", 1)[0]
    for pattern in ("%H%M%S", "%H%M"):
        try:
            return datetime.strptime(digits, pattern).time()
        except ValueError:
            pass
    return time()
