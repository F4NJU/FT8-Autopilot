import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import StationMetadata


class DxccResolver(Protocol):
    def resolve(self, callsign: str) -> StationMetadata: ...


class UnknownDxccResolver:
    def resolve(self, callsign: str) -> StationMetadata:
        return StationMetadata()


class PrefixDxccResolver:
    """AutoPilot-owned offline DXCC prefix resource.

    Entries are explicit DXCC prefixes rather than country first letters.  Longest
    prefix wins, which keeps separate entities such as EA8 and EA correct.
    """

    _ENTRIES = (
        ("EA8", StationMetadata("EA8", "EA8", "Canary Islands", "AF", 28.3, -16.5)),
        ("EA6", StationMetadata("EA6", "EA6", "Balearic Islands", "EU", 39.6, 2.9)),
        ("KH6", StationMetadata("KH6", "KH6", "Hawaii", "OC", 21.3, -157.8)),
        ("KL7", StationMetadata("KL7", "KL7", "Alaska", "NA", 64.2, -149.5)),
        ("KP4", StationMetadata("KP4", "KP4", "Puerto Rico", "NA", 18.2, -66.5)),
        ("VP8", StationMetadata("VP8", "VP8", "Falkland Islands", "SA", -51.8, -59.5)),
        ("3A", StationMetadata("3A", "3A", "Monaco", "EU", 43.7, 7.4)),
        ("3B8", StationMetadata("3B8", "3B8", "Mauritius", "AF", -20.2, 57.5)),
        ("4X", StationMetadata("4X", "4X", "Israel", "AS", 31.0, 35.0)),
        ("5B", StationMetadata("5B", "5B", "Cyprus", "AS", 35.1, 33.4)),
        ("9A", StationMetadata("9A", "9A", "Croatia", "EU", 45.1, 15.2)),
        ("9H", StationMetadata("9H", "9H", "Malta", "EU", 35.9, 14.5)),
        ("BY", StationMetadata("BY", "BY", "China", "AS", 35.9, 104.2)),
        ("CT", StationMetadata("CT", "CT", "Portugal", "EU", 39.4, -8.2)),
        ("DL", StationMetadata("DL", "DL", "Germany", "EU", 51.2, 10.5)),
        ("E7", StationMetadata("E7", "E7", "Bosnia-Herzegovina", "EU", 44.2, 17.7)),
        ("EA", StationMetadata("EA", "EA", "Spain", "EU", 40.4, -3.7)),
        ("EI", StationMetadata("EI", "EI", "Ireland", "EU", 53.4, -8.2)),
        ("ER", StationMetadata("ER", "ER", "Moldova", "EU", 47.0, 28.8)),
        ("ES", StationMetadata("ES", "ES", "Estonia", "EU", 58.6, 25.0)),
        ("F", StationMetadata("F", "F", "France", "EU", 46.2, 2.2)),
        ("G", StationMetadata("G", "G", "England", "EU", 52.4, -1.5)),
        ("GD", StationMetadata("GD", "GD", "Isle of Man", "EU", 54.2, -4.5)),
        ("GI", StationMetadata("GI", "GI", "Northern Ireland", "EU", 54.6, -6.7)),
        ("GM", StationMetadata("GM", "GM", "Scotland", "EU", 56.5, -4.2)),
        ("GW", StationMetadata("GW", "GW", "Wales", "EU", 52.1, -3.8)),
        ("HB", StationMetadata("HB", "HB", "Switzerland", "EU", 46.8, 8.2)),
        ("HL", StationMetadata("HL", "HL", "South Korea", "AS", 36.5, 127.8)),
        ("HS", StationMetadata("HS", "HS", "Thailand", "AS", 15.9, 100.9)),
        ("I", StationMetadata("I", "I", "Italy", "EU", 42.8, 12.8)),
        ("JA", StationMetadata("JA", "JA", "Japan", "AS", 36.2, 138.3)),
        ("K", StationMetadata("K", "K", "United States", "NA", 39.8, -98.6)),
        ("N", StationMetadata("K", "K", "United States", "NA", 39.8, -98.6)),
        ("W", StationMetadata("K", "K", "United States", "NA", 39.8, -98.6)),
        ("LA", StationMetadata("LA", "LA", "Norway", "EU", 60.5, 8.5)),
        ("LX", StationMetadata("LX", "LX", "Luxembourg", "EU", 49.8, 6.1)),
        ("LY", StationMetadata("LY", "LY", "Lithuania", "EU", 55.2, 23.9)),
        ("LZ", StationMetadata("LZ", "LZ", "Bulgaria", "EU", 42.7, 25.5)),
        ("OE", StationMetadata("OE", "OE", "Austria", "EU", 47.5, 14.6)),
        ("OH", StationMetadata("OH", "OH", "Finland", "EU", 64.5, 26.0)),
        ("OK", StationMetadata("OK", "OK", "Czech Republic", "EU", 49.8, 15.5)),
        ("OM", StationMetadata("OM", "OM", "Slovak Republic", "EU", 48.7, 19.7)),
        ("ON", StationMetadata("ON", "ON", "Belgium", "EU", 50.5, 4.5)),
        ("OZ", StationMetadata("OZ", "OZ", "Denmark", "EU", 56.0, 10.0)),
        ("PA", StationMetadata("PA", "PA", "Netherlands", "EU", 52.1, 5.3)),
        ("PY", StationMetadata("PY", "PY", "Brazil", "SA", -10.8, -52.9)),
        ("S5", StationMetadata("S5", "S5", "Slovenia", "EU", 46.2, 14.9)),
        ("SM", StationMetadata("SM", "SM", "Sweden", "EU", 60.1, 18.6)),
        ("SP", StationMetadata("SP", "SP", "Poland", "EU", 52.1, 19.4)),
        ("SV", StationMetadata("SV", "SV", "Greece", "EU", 39.1, 21.8)),
        ("TA", StationMetadata("TA", "TA", "Turkey", "AS", 39.0, 35.2)),
        ("UA", StationMetadata("UA", "UA", "European Russia", "EU", 55.8, 37.6)),
        ("UK", StationMetadata("UK", "UK", "Ukraine", "EU", 48.4, 31.2)),
        ("UN", StationMetadata("UN", "UN", "Kazakhstan", "AS", 48.0, 67.0)),
        ("VE", StationMetadata("VE", "VE", "Canada", "NA", 56.1, -106.3)),
        ("VA", StationMetadata("VE", "VE", "Canada", "NA", 56.1, -106.3)),
        ("VY", StationMetadata("VE", "VE", "Canada", "NA", 56.1, -106.3)),
        ("VK", StationMetadata("VK", "VK", "Australia", "OC", -25.3, 133.8)),
        ("VU", StationMetadata("VU", "VU", "India", "AS", 20.6, 78.9)),
        ("YB", StationMetadata("YB", "YB", "Indonesia", "OC", -0.8, 113.9)),
        ("YL", StationMetadata("YL", "YL", "Latvia", "EU", 56.9, 24.1)),
        ("YO", StationMetadata("YO", "YO", "Romania", "EU", 45.9, 24.9)),
        ("YU", StationMetadata("YU", "YU", "Serbia", "EU", 44.0, 21.0)),
        ("ZL", StationMetadata("ZL", "ZL", "New Zealand", "OC", -40.9, 174.9)),
        ("ZS", StationMetadata("ZS", "ZS", "South Africa", "AF", -30.6, 22.9)),
    )

    def __init__(self) -> None:
        self._entries = tuple(sorted(self._ENTRIES, key=lambda entry: len(entry[0]), reverse=True))

    def resolve(self, callsign: str) -> StationMetadata:
        for normalized in _lookup_forms(callsign):
            for prefix, metadata in self._entries:
                if normalized.startswith(prefix):
                    return metadata
        return StationMetadata()


class FallbackDxccResolver:
    """Prefer the full CTY resolver while retaining an offline prefix fallback."""

    def __init__(self, primary: DxccResolver, fallback: DxccResolver | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or PrefixDxccResolver()

    def resolve(self, callsign: str) -> StationMetadata:
        metadata = self.primary.resolve(callsign)
        return metadata if metadata.country_name != "unknown" else self.fallback.resolve(callsign)


class StaticDxccResolver:
    """Deterministic resolver useful for tests and embedded deployments."""

    def __init__(self, entries: dict[str, StationMetadata]) -> None:
        self._entries = {key.strip().upper(): value for key, value in entries.items()}

    def resolve(self, callsign: str) -> StationMetadata:
        return self._entries.get(callsign.strip().upper(), StationMetadata())


@dataclass(frozen=True, slots=True)
class _CtyEntity:
    metadata: StationMetadata
    prefixes: tuple[str, ...]


class CtyDatResolver:
    """Offline resolver for the common country-files.com CTY.DAT format."""

    _MODIFIER = re.compile(r"[\(\[\{<~].*$")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._exact: dict[str, StationMetadata] = {}
        self._prefixes: list[tuple[str, StationMetadata]] = []
        if path.is_file():
            self._load(path)

    def resolve(self, callsign: str) -> StationMetadata:
        for normalized in _lookup_forms(callsign):
            exact = self._exact.get(normalized)
            if exact is not None:
                return exact
            for prefix, metadata in self._prefixes:
                if normalized.startswith(prefix):
                    return metadata
        return StationMetadata()

    def _load(self, path: Path) -> None:
        current: tuple[str, str, str, str, float | None, float | None] | None = None
        aliases: list[str] = []
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw_line.strip():
                continue
            if raw_line[:1].isspace() and current is not None:
                aliases.extend(raw_line.strip().rstrip(";").split(","))
                if ";" in raw_line:
                    self._commit(current, aliases)
                    current = None
                    aliases = []
                continue
            parts = [part.strip() for part in raw_line.split(":")]
            if len(parts) >= 9:
                if current is not None:
                    self._commit(current, aliases)
                latitude = _as_float(parts[4])
                longitude = _as_float(parts[5])
                current = (parts[0], parts[3].upper(), parts[7].upper(), parts[8].upper(), latitude, longitude)
                aliases = []
        if current is not None:
            self._commit(current, aliases)

    def _commit(self, entity: tuple[str, str, str, str, float | None, float | None], aliases: list[str]) -> None:
        country, continent, primary, dxcc, latitude, longitude = entity
        metadata = StationMetadata(dxcc, primary, country, continent, latitude, longitude)
        prefixes = [primary, *aliases]
        for raw_prefix in prefixes:
            value = self._MODIFIER.sub("", raw_prefix.strip()).strip()
            if not value:
                continue
            exact = value.startswith("=")
            value = value.lstrip("=").upper()
            if exact:
                self._exact[value] = metadata
            else:
                self._prefixes.append((value, metadata))
        self._prefixes.sort(key=lambda item: len(item[0]), reverse=True)


def _lookup_forms(callsign: str) -> tuple[str, ...]:
    """Keep the transmitted callsign first, then try a conventional base call."""
    normalized = callsign.strip().upper()
    if "/" not in normalized:
        return (normalized,)
    parts = tuple(part for part in normalized.split("/") if part)
    base_calls = tuple(part for part in parts if any(char.isdigit() for char in part))
    return tuple(dict.fromkeys((normalized, *base_calls, *parts)))


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None
