from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum, auto


class MessageKind(Enum):
    CQ = auto()
    QRZ = auto()
    DIRECTED = auto()
    REPORT = auto()
    R_REPORT = auto()
    RRR = auto()
    RR73 = auto()
    SEVENTY_THREE = auto()


class CqType(Enum):
    GENERAL = auto()
    DIRECTED_CONTINENT = auto()
    DIRECTED_PREFIX = auto()
    ACTIVITY = auto()
    OTHER_DIRECTED = auto()


class ActivityTag(Enum):
    POTA = auto()
    SOTA = auto()
    QRP = auto()


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    raw: str
    kind: MessageKind
    sender: str
    addressee: str | None = None
    grid: str | None = None
    report: int | None = None
    cq_modifier: str | None = None
    cq_type: CqType | None = None
    cq_target: str | None = None
    activity_tags: frozenset[ActivityTag] = frozenset()

    def is_addressed_to(self, callsign: str) -> bool:
        return self.addressee == callsign.upper()


@dataclass(frozen=True, slots=True)
class DecodeEvent:
    parsed: ParsedMessage
    observed_at: datetime
    mode: str
    snr: int
    frequency: int | None = None
    period: int | None = None
    unique_id: str = ""
    original: "OriginalDecode | None" = None


@dataclass(frozen=True, slots=True)
class OriginalDecode:
    instance_id: str
    schema: int
    decode_time: time
    snr: int
    delta_time: float
    delta_frequency: int
    mode: str
    message: str
    low_confidence: bool
    is_new: bool
    source_endpoint: tuple[str, int] | None
    clear_epoch: int = 0
    off_air: bool = False


class CandidateKind(Enum):
    CQ = auto()
    DIRECT_CALLER = auto()


@dataclass(frozen=True, slots=True)
class WorkedCheck:
    band: str | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class StationMetadata:
    dxcc_entity: str = "unknown"
    primary_prefix: str = "unknown"
    country_name: str = "unknown"
    continent: str = "unknown"


@dataclass(frozen=True, slots=True)
class StationProfile:
    callsign: str
    metadata: StationMetadata = StationMetadata()


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    value: int


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    total: int
    components: tuple[ScoreComponent, ...]

    @classmethod
    def from_components(cls, components: list[ScoreComponent]) -> "ScoreBreakdown":
        frozen = tuple(components)
        return cls(sum(component.value for component in frozen), frozen)


@dataclass(frozen=True, slots=True)
class Candidate:
    station: str
    kind: CandidateKind
    event: DecodeEvent
    score: int
    metadata: StationMetadata = StationMetadata()
    score_breakdown: ScoreBreakdown = ScoreBreakdown(0, ())
    force_priority: bool = False


class EngineEventKind(Enum):
    CANDIDATE_ADDED = auto()
    CANDIDATE_REFUSED = auto()
    CANDIDATE_SELECTED = auto()
    PENDING_DIRECT_ADDED = auto()
    PENDING_DIRECT_REMOVED = auto()


class CooldownKind(Enum):
    STATION_RETRY = auto()
    STALLED_QSO = auto()
    TEMPORARY_IGNORE = auto()
    REMOTE_BUSY_OTHER_QSO = auto()
    REMOTE_RETURNED_TO_CQ = auto()

    def is_soft_for_direct_call(self) -> bool:
        return self in {
            CooldownKind.STATION_RETRY,
            CooldownKind.STALLED_QSO,
            CooldownKind.TEMPORARY_IGNORE,
            CooldownKind.REMOTE_BUSY_OTHER_QSO,
            CooldownKind.REMOTE_RETURNED_TO_CQ,
        }


@dataclass(frozen=True, slots=True)
class StationCooldown:
    until: datetime
    kind: CooldownKind
    reason: str


@dataclass(frozen=True, slots=True)
class EngineEvent:
    kind: EngineEventKind
    station: str
    reason: str
    candidate: Candidate | None = None


class ActionKind(Enum):
    CQ_REPLY = auto()
    DIRECT_REPLY = auto()


class ActionOutcome(Enum):
    PROPOSED_ONLY = auto()
    SENT = auto()
    FAILED = auto()
    REJECTED_LOCAL = auto()


@dataclass(frozen=True, slots=True)
class IntendedAction:
    kind: ActionKind
    station: str
    reason: str
    original_decode: OriginalDecode | None = None
    observed_at: datetime | None = None
