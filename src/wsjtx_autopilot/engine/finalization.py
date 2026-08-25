import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import DecodeEvent, MessageKind, OriginalDecode

LOGGER = logging.getLogger(__name__)
_RETRY_KINDS = frozenset({MessageKind.RRR, MessageKind.RR73})


@dataclass(slots=True)
class FinalizationState:
    remote_callsign: str
    instance_id: str | None
    mode: str
    frequency: int | None
    last_terminal_kind: MessageKind
    source_decode: OriginalDecode | None
    deadline: datetime
    period_seconds: int
    started_at: datetime
    last_terminal_period: tuple[object, ...]
    retry_count: int = 0
    tx_confirmed: bool = False


class FinalizationTracker:
    def __init__(self, hold_periods: int = 1, max_retries: int = 3) -> None:
        self.hold_periods = max(1, hold_periods)
        self.max_retries = max(0, max_retries)
        self.state: FinalizationState | None = None

    @property
    def active(self) -> bool:
        return self.state is not None

    def begin(self, event: DecodeEvent, now: datetime) -> bool:
        if event.parsed.kind not in _RETRY_KINDS:
            return False
        period = event.period or 15
        if self.state is not None and self.state.remote_callsign == event.parsed.sender:
            return False
        self.state = FinalizationState(
            remote_callsign=event.parsed.sender,
            instance_id=event.original.instance_id if event.original is not None else None,
            mode=event.mode,
            frequency=event.frequency,
            last_terminal_kind=event.parsed.kind,
            source_decode=event.original,
            deadline=now + timedelta(seconds=period * (self.hold_periods + 1)),
            period_seconds=period,
            started_at=now,
            last_terminal_period=_period_key(event),
        )
        LOGGER.info(
            "[FINALIZE] started remote=%s type=%s hold_periods=%d",
            event.parsed.sender,
            event.parsed.kind.name,
            self.hold_periods,
        )
        return True

    def confirm_final_tx(self, now: datetime) -> None:
        if self.state is None or self.state.tx_confirmed:
            return
        self.state.tx_confirmed = True
        self.state.deadline = now + timedelta(seconds=self.state.period_seconds * self.hold_periods)
        LOGGER.info("[FINALIZE] final 73 transmitted remote=%s", self.state.remote_callsign)

    def matches_retry(self, event: DecodeEvent) -> bool:
        if self.state is None:
            return False
        event_instance = event.original.instance_id if event.original is not None else None
        return (
            event.parsed.sender == self.state.remote_callsign
            and event.parsed.kind in _RETRY_KINDS
            and event.mode == self.state.mode
            and (self.state.instance_id is None or event_instance == self.state.instance_id)
            and (
                self.state.frequency is None
                or event.frequency is None
                or event.frequency == self.state.frequency
            )
        )

    def same_terminal_period(self, event: DecodeEvent) -> bool:
        return self.state is not None and _period_key(event) == self.state.last_terminal_period

    def advance_terminal(self, event: DecodeEvent, now: datetime) -> None:
        assert self.state is not None
        self.state.last_terminal_kind = event.parsed.kind
        self.state.source_decode = event.original
        self.state.last_terminal_period = _period_key(event)
        self.state.deadline = now + timedelta(seconds=self.state.period_seconds * (self.hold_periods + 1))
        LOGGER.info(
            "[FINALIZE] terminal progression remote=%s type=%s",
            self.state.remote_callsign,
            event.parsed.kind.name,
        )

    def can_retry(self) -> bool:
        return self.state is not None and self.state.retry_count < self.max_retries

    def record_retry(self, event: DecodeEvent, now: datetime) -> int:
        assert self.state is not None
        self.state.retry_count += 1
        self.state.last_terminal_kind = event.parsed.kind
        self.state.source_decode = event.original
        self.state.last_terminal_period = _period_key(event)
        self.state.deadline = now + timedelta(seconds=self.state.period_seconds * self.hold_periods)
        LOGGER.info(
            "[FINALIZE] retry final 73 count=%d/%d remote=%s",
            self.state.retry_count,
            self.max_retries,
            self.state.remote_callsign,
        )
        return self.state.retry_count

    def close(self, reason: str) -> None:
        if self.state is None:
            return
        LOGGER.info("[FINALIZE] closed remote=%s reason=%s", self.state.remote_callsign, reason)
        self.state = None

    def expire(self, now: datetime) -> bool:
        if self.state is None or now < self.state.deadline:
            return False
        self.close("grace period expired")
        return True


def _period_key(event: DecodeEvent) -> tuple[object, ...]:
    if event.original is not None:
        return (event.original.instance_id, event.original.decode_time, event.mode)
    return (event.mode, event.observed_at)
