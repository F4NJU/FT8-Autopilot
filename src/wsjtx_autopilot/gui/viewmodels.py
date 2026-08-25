from dataclasses import dataclass
from datetime import datetime

from wsjtx_autopilot.engine.models import Candidate, EngineEvent
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.runtime import WsjtxStatus
from wsjtx_autopilot.wsjtx.models import DecodePacket


@dataclass(frozen=True, slots=True)
class ActivityRow:
    observed_at: datetime
    snr: int
    delta_frequency: int
    mode: str
    message: str
    low_confidence: bool
    activity_tags: tuple[str, ...]
    cq_target: str

    @classmethod
    def from_decode(cls, packet: DecodePacket, observed_at: datetime) -> "ActivityRow":
        parsed = parse_ft8_message(packet.message)
        return cls(
            observed_at,
            packet.snr,
            packet.delta_frequency,
            packet.mode,
            packet.message,
            packet.low_confidence,
            tuple(sorted(tag.name for tag in parsed.activity_tags)) if parsed is not None else (),
            parsed.cq_target or "" if parsed is not None else "",
        )


@dataclass(frozen=True, slots=True)
class CandidateRow:
    station: str
    kind: str
    snr: int
    country: str
    dxcc: str
    continent: str
    cq_target: str
    activities: str
    score: int
    score_detail: str
    reason: str

    @classmethod
    def from_engine_event(cls, event: EngineEvent) -> "CandidateRow | None":
        candidate = event.candidate
        if candidate is None:
            return None
        return cls.from_candidate(candidate, event.reason)

    @classmethod
    def from_candidate(cls, candidate: Candidate, reason: str = "") -> "CandidateRow":
        details = ", ".join(
            f"{component.name} {component.value:+d}"
            for component in candidate.score_breakdown.components
        ) or "base"
        return cls(
            candidate.station,
            candidate.kind.name,
            candidate.event.snr,
            candidate.metadata.country_name,
            candidate.metadata.primary_prefix,
            candidate.metadata.continent,
            candidate.event.parsed.cq_target or "GENERAL",
            ", ".join(sorted(tag.name for tag in candidate.event.parsed.activity_tags)) or "-",
            candidate.score,
            details,
            reason,
        )


@dataclass(frozen=True, slots=True)
class StatusView:
    frequency: str
    mode: str
    dx_call: str
    tx_state: str

    @classmethod
    def from_status(cls, status: WsjtxStatus) -> "StatusView":
        frequency = "-" if status.dial_frequency is None else f"{status.dial_frequency / 1_000_000:.6f} MHz"
        tx_state = "TX" if status.transmitting else "TX ENABLED" if status.tx_enabled else "RX"
        return cls(frequency, status.mode or "-", status.dx_call or "-", tx_state)
