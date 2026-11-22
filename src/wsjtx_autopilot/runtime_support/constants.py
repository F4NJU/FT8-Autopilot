"""Runtime-wide constants and helpers shared by extracted collaborators."""

_UNKNOWN_TR_PERIOD = 0xFFFFFFFF
_DECODE_MODE_MARKERS = {"~": "FT8", "+": "FT4"}
_AUTOMATION_MODES = frozenset({"FT8", "FT4"})


def _tr_period_seconds(raw_period: int) -> int | None:
    """Normalize the WSJT-X T/R period to whole seconds.

    WSJT-X variants have emitted this field both as seconds (15) and as
    milliseconds (15000).  Round millisecond values up so AutoPilot never
    resumes in the middle of a transmit/receive period.
    """
    if raw_period == _UNKNOWN_TR_PERIOD or raw_period <= 0:
        return None
    if raw_period >= 1000:
        return max(1, (raw_period + 999) // 1000)
    return raw_period
