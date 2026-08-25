import re

from .models import ActivityTag, CqType, MessageKind, ParsedMessage

_CALLSIGN = re.compile(r"^(?=.{3,11}$)(?=.*[A-Z])(?=.*\d)[A-Z0-9]+(?:/[A-Z0-9]+)?$")
_GRID = re.compile(r"^[A-R]{2}\d{2}(?:[A-X]{2})?$")
_REPORT = re.compile(r"^(R)?([+-])(\d{1,2})$")
_CQ_QUALIFIER = re.compile(r"^[A-Z0-9]{1,8}$")
_CONTINENTS = frozenset({"AF", "AN", "AS", "EU", "NA", "OC", "SA"})
_ACTIVITIES = {tag.name: tag for tag in ActivityTag}


def _is_callsign(value: str) -> bool:
    return _normalize_callsign_token(value) is not None


def _normalize_callsign_token(value: str) -> str | None:
    candidate = value[1:-1] if value.startswith("<") and value.endswith(">") else value
    return candidate if _CALLSIGN.fullmatch(candidate) else None


def parse_ft8_message(text: str) -> ParsedMessage | None:
    """Parse unambiguous common FT8 exchanges; return None for free text."""
    raw = " ".join(text.upper().split())
    parts = raw.split()
    if not parts:
        return None

    if parts[0] in {"CQ", "QRZ"}:
        modifier, station_index = _cq_layout(parts)
        if station_index is None:
            return None
        station = _normalize_callsign_token(parts[station_index])
        if station is None:
            return None
        grid = parts[station_index + 1] if len(parts) == station_index + 2 else None
        if grid is not None and not _GRID.fullmatch(grid):
            return None
        kind = MessageKind.QRZ if parts[0] == "QRZ" else MessageKind.CQ
        cq_type, target, activity_tags = _classify_cq(modifier)
        return ParsedMessage(
            raw,
            kind,
            station,
            grid=grid,
            cq_modifier=modifier,
            cq_type=cq_type,
            cq_target=target,
            activity_tags=activity_tags,
        )

    if len(parts) != 3 or not _is_callsign(parts[0]) or not _is_callsign(parts[1]):
        return None
    addressee = _normalize_callsign_token(parts[0])
    sender = _normalize_callsign_token(parts[1])
    payload = parts[2]
    assert addressee is not None and sender is not None
    report_match = _REPORT.fullmatch(payload)
    if report_match:
        report = int(report_match.group(3)) * (-1 if report_match.group(2) == "-" else 1)
        kind = MessageKind.R_REPORT if report_match.group(1) else MessageKind.REPORT
        return ParsedMessage(raw, kind, sender, addressee, report=report)
    terminal_kinds = {
        "RRR": MessageKind.RRR,
        "RR73": MessageKind.RR73,
        "73": MessageKind.SEVENTY_THREE,
    }
    if payload in terminal_kinds:
        return ParsedMessage(raw, terminal_kinds[payload], sender, addressee)
    if _GRID.fullmatch(payload):
        return ParsedMessage(raw, MessageKind.DIRECTED, sender, addressee, grid=payload)
    return None


def _classify_cq(modifier: str | None) -> tuple[CqType, str | None, frozenset[ActivityTag]]:
    if modifier is None:
        return CqType.GENERAL, None, frozenset()
    activity = _ACTIVITIES.get(modifier)
    if activity is not None:
        return CqType.ACTIVITY, None, frozenset({activity})
    if modifier in _CONTINENTS:
        return CqType.DIRECTED_CONTINENT, modifier, frozenset()
    if modifier == "DX":
        return CqType.OTHER_DIRECTED, modifier, frozenset()
    if modifier.isalnum() and 1 <= len(modifier) <= 4:
        return CqType.DIRECTED_PREFIX, modifier, frozenset()
    return CqType.OTHER_DIRECTED, modifier, frozenset()


def _cq_layout(parts: list[str]) -> tuple[str | None, int | None]:
    if len(parts) == 2:
        return (None, 1) if _is_callsign(parts[1]) else (None, None)
    if len(parts) == 3:
        if _is_callsign(parts[1]) and _GRID.fullmatch(parts[2]):
            return None, 1
        if _CQ_QUALIFIER.fullmatch(parts[1]) and _is_callsign(parts[2]):
            return parts[1], 2
        return None, None
    if (
        len(parts) == 4
        and _CQ_QUALIFIER.fullmatch(parts[1])
        and _is_callsign(parts[2])
        and _GRID.fullmatch(parts[3])
    ):
        return parts[1], 2
    return None, None
