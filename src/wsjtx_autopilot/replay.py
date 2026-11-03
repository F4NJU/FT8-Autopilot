"""Offline, no-transmit replay support for AutoPilot regression scenarios."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.base import Endpoint
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.engine.models import ActionOutcome, IntendedAction
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.models import WsjtxPacket


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """One recorded WSJT-X packet, or a timed idle tick, for a replay."""

    observed_at: datetime
    packet: WsjtxPacket | None = None
    endpoint: Endpoint | None = None
    label: str = ""


class ReplayControl(DryRunControl):
    """Records proposed actions while guaranteeing that no radio command is sent."""

    def __init__(self, outcome: ActionOutcome = ActionOutcome.PROPOSED_ONLY) -> None:
        self.outcome = outcome
        self.actions: list[IntendedAction] = []

    def execute(self, action: IntendedAction, now: datetime) -> ActionOutcome:
        self.actions.append(action)
        return self.outcome


@dataclass(frozen=True, slots=True)
class ReplayResult:
    actions: tuple[IntendedAction, ...]
    diagnostics: dict[str, object]
    qso_state: str
    remote_callsign: str | None


def replay_frames(
    config: AppConfig,
    frames: Iterable[ReplayFrame],
    *,
    outcome: ActionOutcome = ActionOutcome.PROPOSED_ONLY,
) -> ReplayResult:
    """Run a deterministic packet sequence with an always-dry control adapter."""
    control = ReplayControl(outcome)
    runtime = AutopilotRuntime(config, control=control)
    for frame in sorted(frames, key=lambda item: item.observed_at):
        runtime.handle(frame.packet, frame.observed_at, frame.endpoint)
    session = runtime.engine.state.session
    return ReplayResult(
        tuple(control.actions),
        runtime.session_diagnostic_snapshot(),
        session.state.name,
        session.remote_callsign,
    )
