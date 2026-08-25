from dataclasses import dataclass
from enum import IntEnum

from .models import MessageKind


class QsoProgressStage(IntEnum):
    CALL_OR_GRID = 1
    REPORT = 2
    R_REPORT = 3
    ROGER = 4
    SIGNOFF = 5


_STAGES = {
    MessageKind.DIRECTED: QsoProgressStage.CALL_OR_GRID,
    MessageKind.REPORT: QsoProgressStage.REPORT,
    MessageKind.R_REPORT: QsoProgressStage.R_REPORT,
    MessageKind.RRR: QsoProgressStage.ROGER,
    MessageKind.RR73: QsoProgressStage.ROGER,
    MessageKind.SEVENTY_THREE: QsoProgressStage.SIGNOFF,
}


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    relevant: bool
    progressed: bool
    stalled: bool
    stage: QsoProgressStage | None
    no_progress: int


class QsoProgressTracker:
    def __init__(self, max_no_progress_periods: int = 10) -> None:
        if max_no_progress_periods < 1:
            raise ValueError("max_no_progress_periods must be at least 1")
        self.maximum = max_no_progress_periods
        self.stage: QsoProgressStage | None = None
        self.no_progress = 0
        self._last_period_key: object | None = None

    def start(self, stage: QsoProgressStage | None = None, period_key: object | None = None) -> None:
        self.stage = stage
        self.no_progress = 0
        self._last_period_key = period_key

    def observe(self, kind: MessageKind, period_key: object | None = None) -> ProgressUpdate:
        observed = _STAGES.get(kind)
        if observed is None:
            return ProgressUpdate(False, False, False, self.stage, self.no_progress)
        if self.stage is None or observed > self.stage:
            self.stage = observed
            self.no_progress = 0
            self._last_period_key = period_key
            return ProgressUpdate(True, True, False, self.stage, self.no_progress)
        if period_key is not None and period_key == self._last_period_key:
            return ProgressUpdate(True, False, False, self.stage, self.no_progress)
        self._last_period_key = period_key
        self.no_progress += 1
        return ProgressUpdate(True, False, self.no_progress >= self.maximum, self.stage, self.no_progress)

    def reset(self) -> None:
        self.stage = None
        self.no_progress = 0
        self._last_period_key = None


def progress_stage_for(kind: MessageKind) -> QsoProgressStage | None:
    return _STAGES.get(kind)
