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
        normalized = callsign.strip().upper()
        exact = self._exact.get(normalized)
        if exact is not None:
            return exact
        for prefix, metadata in self._prefixes:
            if normalized.startswith(prefix):
                return metadata
        return StationMetadata()

    def _load(self, path: Path) -> None:
        current: tuple[str, str, str, str] | None = None
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
            if len(parts) >= 8:
                if current is not None:
                    self._commit(current, aliases)
                current = (parts[0], parts[3].upper(), parts[7].upper(), parts[7].upper())
                aliases = []
        if current is not None:
            self._commit(current, aliases)

    def _commit(self, entity: tuple[str, str, str, str], aliases: list[str]) -> None:
        country, continent, primary, dxcc = entity
        metadata = StationMetadata(dxcc, primary, country, continent)
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
