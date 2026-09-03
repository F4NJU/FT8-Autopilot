import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

from .models import DecodeEvent, MessageKind
from .progress import QsoProgressStage, QsoProgressTracker, progress_stage_for

LOGGER = logging.getLogger(__name__)


class QsoState(Enum):
    IDLE = auto()
    CALLING_CQ = auto()
    CALLING_STATION = auto()
    DIRECT_CALL_RECEIVED = auto()
    DIRECT_REPLY_SENT = auto()
    DIRECT_REPLY_UNCONFIRMED = auto()
    REPLY_UNCONFIRMED = auto()
    WAITING_REPORT = auto()
    WAITING_R_REPORT = auto()
    QSO_ACTIVE = auto()
    WAITING_73 = auto()
    WAITING_FINAL_TX = auto()
    COMPLETE = auto()
    ABORTED = auto()


@dataclass(slots=True)
class QsoSession:
    local_callsign: str
    qso_id: int | None = None
    started_at: datetime | None = None
    state: QsoState = QsoState.IDLE
    remote_callsign: str | None = None
    mode: str | None = None
    frequency: int | None = None
    period: int | None = None
    last_message: str | None = None
    last_activity: datetime | None = None
    retries: int = 0
    report_received: int | None = None
    report_sent: int | None = None
    completed: bool = False
    instance_id: str | None = None
    progress_stage: QsoProgressStage | None = None
    no_progress_periods: int = 0
    reply_confirmed: bool = False
    reply_sent_at: datetime | None = None
    remote_cq_count: int = 0
    last_remote_cq_period: tuple[object, ...] | None = None


class QsoStateMachine:
    def __init__(
        self,
        local_callsign: str,
        timeout_seconds: float,
        max_retries: int,
        max_no_progress_periods: int = 10,
    ) -> None:
        self.session = QsoSession(local_callsign.upper())
        self.timeout = timedelta(seconds=timeout_seconds)
        self.max_retries = max_retries
        self.progress = QsoProgressTracker(max_no_progress_periods)
        self._next_qso_id = 1

    def _transition(self, state: QsoState, reason: str) -> None:
        previous = self.session.state
        self.session.state = state
        LOGGER.info(
            "[QSO id=%s] state %s -> %s reason=%s remote=%s",
            self.session.qso_id or "-",
            previous.name,
            state.name,
            reason,
            self.session.remote_callsign or "-",
        )

    def _begin(self, event: DecodeEvent) -> None:
        self.session.qso_id = self._next_qso_id
        self._next_qso_id += 1
        self.session.started_at = event.observed_at

    def start_station(
        self,
        event: DecodeEvent,
        now: datetime | None = None,
    ) -> None:
        if self.session.state is not QsoState.IDLE:
            raise RuntimeError("cannot start a second QSO")
        self._begin(event)
        self.session.remote_callsign = event.parsed.sender
        self.session.mode = event.mode
        self.session.frequency = event.frequency
        self.session.period = event.period
        self.session.last_message = event.parsed.raw
        self.session.last_activity = event.observed_at
        self.session.retries = 0
        self.session.completed = False
        self.session.instance_id = event.original.instance_id if event.original is not None else None
        self.session.reply_sent_at = now or event.observed_at
        self.session.reply_confirmed = False
        self.progress.start(QsoProgressStage.CALL_OR_GRID)
        self._sync_progress()
        self._transition(QsoState.CALLING_STATION, "reply sent")

    def start_from_observed_exchange(
        self,
        event: DecodeEvent,
    ) -> None:
        """Engage a session from an unambiguous message addressed to us."""
        if self.session.state is not QsoState.IDLE:
            raise RuntimeError("cannot start a second QSO")
        self._begin(event)
        self.session.remote_callsign = event.parsed.sender
        self.session.mode = event.mode
        self.session.frequency = event.frequency
        self.session.period = event.period
        self.session.last_message = event.parsed.raw
        self.session.last_activity = event.observed_at
        self.session.report_received = event.parsed.report
        self.session.instance_id = event.original.instance_id if event.original is not None else None
        self.progress.start(progress_stage_for(event.parsed.kind), _period_key(event))
        self._sync_progress()
        self._transition(QsoState.DIRECT_CALL_RECEIVED, "direct exchange observed")

    def mark_direct_reply_sent(self, now: datetime) -> None:
        if self.session.state is not QsoState.DIRECT_CALL_RECEIVED:
            raise RuntimeError("direct Reply requires an observed direct call")
        self.session.last_activity = now
        self.session.reply_sent_at = now
        self.session.reply_confirmed = False
        self._transition(QsoState.DIRECT_REPLY_SENT, "direct Reply sent; awaiting WSJT-X Status")

    def confirm_direct_reply(self, now: datetime) -> None:
        if self.session.state is not QsoState.DIRECT_REPLY_SENT:
            return
        self.session.last_activity = now
        self.session.reply_confirmed = True
        self._transition(QsoState.CALLING_STATION, "direct Reply confirmed by coherent Status")

    def confirm_reply(self, now: datetime) -> None:
        if self.session.state is not QsoState.CALLING_STATION or self.session.reply_confirmed:
            return
        self.session.last_activity = now
        self.session.reply_confirmed = True
        LOGGER.info("[ENGINE] Reply confirmed remote=%s", self.session.remote_callsign)

    def expire_reply_confirmation(self, now: datetime, timeout_seconds: float) -> bool:
        if self.session.state is not QsoState.CALLING_STATION or self.session.reply_confirmed:
            return False
        if self.session.reply_sent_at is None or now - self.session.reply_sent_at <= timedelta(seconds=timeout_seconds):
            return False
        self._transition(QsoState.REPLY_UNCONFIRMED, "no coherent WSJT-X Status or remote Decode observed")
        self.reset()
        return True

    def cancel_direct_reply(self, reason: str) -> None:
        if self.session.state is QsoState.DIRECT_CALL_RECEIVED:
            self._transition(QsoState.ABORTED, f"direct Reply {reason}")
            self.reset()

    def expire_direct_reply(self, now: datetime, timeout_seconds: float) -> bool:
        if self.session.state is not QsoState.DIRECT_REPLY_SENT:
            return False
        if self.session.last_activity is None or now - self.session.last_activity <= timedelta(seconds=timeout_seconds):
            return False
        self._transition(QsoState.DIRECT_REPLY_UNCONFIRMED, "no coherent WSJT-X Status observed")
        self.reset()
        return True

    def observe(self, event: DecodeEvent) -> bool:
        """Advance an active QSO; return False when the decode is unrelated."""
        if self.session.state is QsoState.IDLE:
            return False
        message = event.parsed
        if message.sender != self.session.remote_callsign:
            LOGGER.info("Rejected caller %s: QSO with %s is active", message.sender, self.session.remote_callsign)
            return False
        if not message.is_addressed_to(self.session.local_callsign):
            return False
        self.session.last_message = message.raw
        self.session.last_activity = event.observed_at
        if self.session.state in {QsoState.CALLING_STATION, QsoState.DIRECT_REPLY_SENT}:
            self.session.reply_confirmed = True
        progress = self.progress.observe(message.kind, _period_key(event))
        self._sync_progress()
        if progress.relevant and not progress.progressed:
            LOGGER.info(
                "[WATCHDOG] qso_id=%s remote=%s repeat=%s no_progress=%d/%d",
                self.session.qso_id,
                self.session.remote_callsign,
                progress.stage.name if progress.stage is not None else "UNKNOWN",
                progress.no_progress,
                self.progress.maximum,
            )
        if progress.progressed:
            self.session.remote_cq_count = 0
        if message.kind is MessageKind.REPORT:
            self.session.report_received = message.report
            if self.session.state is not QsoState.DIRECT_CALL_RECEIVED:
                self._transition(QsoState.QSO_ACTIVE, "signal report received")
        elif message.kind is MessageKind.R_REPORT:
            self.session.report_received = message.report
            self._transition(QsoState.QSO_ACTIVE, "acknowledged report received")
        elif message.kind in {MessageKind.RRR, MessageKind.RR73}:
            self._transition(QsoState.WAITING_FINAL_TX, "remote terminal received; awaiting local 73")
        elif message.kind is MessageKind.SEVENTY_THREE:
            self.session.completed = True
            self._transition(QsoState.COMPLETE, "QSO completion received")
        return True

    def matches_remote(self, station: str, instance_id: str | None = None) -> bool:
        if self.session.state is QsoState.IDLE or self.session.remote_callsign is None:
            return False
        if self.session.remote_callsign.upper() != station.upper():
            return False
        return instance_id is None or self.session.instance_id is None or self.session.instance_id == instance_id

    def mark_complete(self, reason: str) -> None:
        if self.session.state is QsoState.IDLE:
            return
        self.session.completed = True
        if self.session.state is not QsoState.COMPLETE:
            self._transition(QsoState.COMPLETE, reason)

    def touch(self, now: datetime) -> None:
        if self.session.state is not QsoState.IDLE:
            self.session.last_activity = now

    def abort(self, reason: str) -> None:
        if self.session.state is QsoState.IDLE:
            return
        self._transition(QsoState.ABORTED, reason)
        self.reset()

    def expire(self, now: datetime) -> bool:
        if self.session.state in {QsoState.IDLE, QsoState.ABORTED}:
            return False
        if self.session.last_activity is None or now - self.session.last_activity <= self.timeout:
            return False
        self.session.retries += 1
        reason = "retry limit reached" if self.session.retries >= self.max_retries else "QSO inactivity timeout"
        self._transition(QsoState.ABORTED, reason)
        self.reset()
        return True

    def reset(self) -> None:
        local = self.session.local_callsign
        previous = self.session.state
        qso_id = self.session.qso_id
        remote = self.session.remote_callsign
        duration = (
            (self.session.last_activity - self.session.started_at).total_seconds()
            if self.session.started_at is not None and self.session.last_activity is not None
            else 0.0
        )
        self.progress.reset()
        self.session = QsoSession(local)
        LOGGER.info(
            "[QSO id=%s] state %s -> IDLE reason=session reset remote=%s duration_seconds=%.1f",
            qso_id or "-",
            previous.name,
            remote or "-",
            duration,
        )

    def _sync_progress(self) -> None:
        self.session.progress_stage = self.progress.stage
        self.session.no_progress_periods = self.progress.no_progress


def _period_key(event: DecodeEvent) -> tuple[object, ...]:
    if event.original is not None:
        return (event.original.instance_id, event.original.decode_time, event.mode)
    return (event.mode, event.observed_at)
