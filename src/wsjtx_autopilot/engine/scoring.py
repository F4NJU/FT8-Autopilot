from dataclasses import dataclass, field
from datetime import datetime

from wsjtx_autopilot.config import ActivityPolicy, DirectCallPolicy

from .dxcc import DxccResolver, UnknownDxccResolver
from .models import (
    ActivityTag,
    CandidateKind,
    DecodeEvent,
    ScoreBreakdown,
    ScoreComponent,
    StationMetadata,
    WorkedCheck,
)


@dataclass(slots=True)
class ScoringPreferences:
    preferred_continents: set[str] = field(default_factory=set)
    preferred_dxcc: set[str] = field(default_factory=set)
    direct_call_policy: DirectCallPolicy = DirectCallPolicy.ALWAYS_PRIORITY
    allow_dupes: bool = False
    allow_direct_call_dupes: bool = False
    minimum_snr: int | None = None
    favor_strong_signals: bool = True
    direct_call_bonus: int = 10_000
    preferred_dxcc_bonus: int = 1_000
    preferred_continent_bonus: int = 500
    signal_bonus_max: int = 50
    blacklist: set[str] = field(default_factory=set)
    pota_policy: ActivityPolicy = ActivityPolicy.NORMAL
    sota_policy: ActivityPolicy = ActivityPolicy.NORMAL
    qrp_policy: ActivityPolicy = ActivityPolicy.NORMAL
    activity_priority_bonus: int = 750

    def normalize(self) -> None:
        self.preferred_continents = {value.strip().upper() for value in self.preferred_continents}
        self.preferred_dxcc = {value.strip().upper() for value in self.preferred_dxcc}
        self.blacklist = {value.strip().upper() for value in self.blacklist}


@dataclass(frozen=True, slots=True)
class ScoringResult:
    accepted: bool
    reason: str
    metadata: StationMetadata
    breakdown: ScoreBreakdown
    force_priority: bool = False


class CandidateScorer:
    def __init__(
        self,
        preferences: ScoringPreferences | None = None,
        dxcc_resolver: DxccResolver | None = None,
    ) -> None:
        self.preferences = preferences or ScoringPreferences()
        self.preferences.normalize()
        self.dxcc_resolver = dxcc_resolver or UnknownDxccResolver()
        self._ignored_until: dict[str, datetime] = {}

    def update_preferences(self, preferences: ScoringPreferences) -> None:
        preferences.normalize()
        self.preferences = preferences

    def ignore_station(self, station: str, until: datetime) -> None:
        self._ignored_until[station.strip().upper()] = until

    def evaluate(
        self,
        event: DecodeEvent,
        kind: CandidateKind,
        worked: WorkedCheck | None = None,
    ) -> ScoringResult:
        station = event.parsed.sender.strip().upper()
        metadata = self.dxcc_resolver.resolve(station)
        empty = ScoreBreakdown(0, ())
        ignored_until = self._ignored_until.get(station)
        if ignored_until is not None:
            if event.observed_at < ignored_until:
                return ScoringResult(False, f"ignored until {ignored_until.isoformat()}", metadata, empty)
            del self._ignored_until[station]
        if station in self.preferences.blacklist:
            return ScoringResult(False, "blacklisted", metadata, empty)
        if self.preferences.minimum_snr is not None and event.snr < self.preferences.minimum_snr:
            return ScoringResult(False, f"SNR below minimum ({event.snr} < {self.preferences.minimum_snr})", metadata, empty)
        if kind is CandidateKind.DIRECT_CALLER and self.preferences.direct_call_policy is DirectCallPolicy.IGNORE:
            return ScoringResult(False, "direct calls ignored", metadata, empty)
        activity_policies = {
            ActivityTag.POTA: self.preferences.pota_policy,
            ActivityTag.SOTA: self.preferences.sota_policy,
            ActivityTag.QRP: self.preferences.qrp_policy,
        }
        for activity in event.parsed.activity_tags:
            if activity_policies[activity] is ActivityPolicy.IGNORE:
                return ScoringResult(False, f"{activity.name} activity ignored", metadata, empty)
        if worked is not None and worked.duplicate:
            direct_override = kind is CandidateKind.DIRECT_CALLER and self.preferences.allow_direct_call_dupes
            if not self.preferences.allow_dupes and not direct_override:
                return ScoringResult(False, f"worked today band={worked.band}", metadata, empty)

        components: list[ScoreComponent] = []
        force_priority = False
        if kind is CandidateKind.DIRECT_CALLER and self.preferences.direct_call_policy is DirectCallPolicy.ALWAYS_PRIORITY:
            components.append(ScoreComponent("DIRECT_CALL", self.preferences.direct_call_bonus))
            force_priority = True
        if metadata.primary_prefix.upper() in self.preferences.preferred_dxcc:
            components.append(ScoreComponent(f"PREFERRED_DXCC_{metadata.primary_prefix.upper()}", self.preferences.preferred_dxcc_bonus))
        if metadata.continent.upper() in self.preferences.preferred_continents:
            components.append(ScoreComponent(f"PREFERRED_{metadata.continent.upper()}", self.preferences.preferred_continent_bonus))
        for activity in sorted(event.parsed.activity_tags, key=lambda tag: tag.name):
            if activity_policies[activity] is ActivityPolicy.PRIORITY:
                components.append(ScoreComponent(f"PREFERRED_{activity.name}", self.preferences.activity_priority_bonus))
        if self.preferences.favor_strong_signals:
            signal = max(0, min(self.preferences.signal_bonus_max, event.snr + 30))
            if signal:
                components.append(ScoreComponent("SIGNAL", signal))
        breakdown = ScoreBreakdown.from_components(components)
        return ScoringResult(True, "accepted", metadata, breakdown, force_priority)
