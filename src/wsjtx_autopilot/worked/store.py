import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path


def normalize_callsign(callsign: str) -> str:
    return callsign.strip().upper()


class WorkedQsoStore:
    """SQLite persistence for one Worked Today record per date/call/band."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worked_qso (
                qso_date_utc TEXT NOT NULL,
                callsign TEXT NOT NULL,
                band TEXT NOT NULL,
                mode TEXT NOT NULL,
                frequency_hz INTEGER NOT NULL,
                logged_at_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                UNIQUE (qso_date_utc, callsign, band)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS worked_qso_date_idx ON worked_qso (qso_date_utc)"
        )
        self._connection.commit()

    def record(
        self,
        qso_date_utc: date,
        callsign: str,
        band: str,
        mode: str,
        frequency_hz: int,
        logged_at_utc: datetime,
        source: str,
    ) -> bool:
        normalized = normalize_callsign(callsign)
        if not normalized:
            raise ValueError("callsign must not be empty")
        logged_at = _as_utc(logged_at_utc).isoformat()
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO worked_qso
                (qso_date_utc, callsign, band, mode, frequency_hz, logged_at_utc, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                qso_date_utc.isoformat(),
                normalized,
                band,
                mode.strip().upper(),
                int(frequency_hz),
                logged_at,
                source,
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def keys_for_date(self, qso_date_utc: date) -> set[tuple[str, str]]:
        rows = self._connection.execute(
            "SELECT callsign, band FROM worked_qso WHERE qso_date_utc = ?",
            (qso_date_utc.isoformat(),),
        )
        return {(str(callsign), str(band)) for callsign, band in rows}

    def count_for_date(self, qso_date_utc: date) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM worked_qso WHERE qso_date_utc = ?",
            (qso_date_utc.isoformat(),),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def source_for(self, qso_date_utc: date, callsign: str, band: str) -> str | None:
        row = self._connection.execute(
            "SELECT source FROM worked_qso WHERE qso_date_utc = ? AND callsign = ? AND band = ?",
            (qso_date_utc.isoformat(), normalize_callsign(callsign), band),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "WorkedQsoStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
