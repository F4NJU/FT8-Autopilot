from dataclasses import dataclass, field
from datetime import datetime

from wsjtx_autopilot.config import ActivityPolicy, DirectCallPolicy

from .dxcc import DxccResolver, UnknownDxccResolver
from .propagation import reply_path_probability
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
    prioritize_special_calls: bool = False
    rare_stations_only: bool = False
    special_calls: set[str] = field(default_factory=set)
    special_call_priority_bonus: int = 5_000
    # Kept under its historical settings key for backwards compatibility.
    # This is deliberately a priority mode, not a transmission blocklist:
    # when no new LoTW DXCC is decoded locally, normal CQ traffic continues.
    unconfirmed_dxcc_only: bool = False
    lotw_confirmed_dxcc_entities: set[str] | None = None
    unconfirmed_dxcc_priority_bonus: int = 4_000
    prioritize_favorable_path: bool = False
    local_grid: str = ""
    solar_flux: float | None = None
    kp: float | None = None
    favorable_path_bonus: int = 250

    def normalize(self) -> None:
        self.preferred_continents = {value.strip().upper() for value in self.preferred_continents}
        self.preferred_dxcc = {value.strip().upper() for value in self.preferred_dxcc}
        self.blacklist = {value.strip().upper() for value in self.blacklist}
        self.special_calls = {value.strip().upper() for value in self.special_calls if value.strip()}
        if self.lotw_confirmed_dxcc_entities is not None:
            self.lotw_confirmed_dxcc_entities = {
                value.strip().upper()
                for value in self.lotw_confirmed_dxcc_entities
                if value.strip()
            }


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

    def update_special_calls(self, callsigns: set[str]) -> None:
        self.preferences.special_calls = {value.strip().upper() for value in callsigns if value.strip()}

    def update_lotw_confirmed_dxcc(self, entities: set[str] | None) -> None:
        self.preferences.lotw_confirmed_dxcc_entities = None if entities is None else {
            value.strip().upper() for value in entities if value.strip()
        }

    def update_local_grid(self, grid: str | None) -> None:
        self.preferences.local_grid = str(grid or "").strip().upper()

    def update_space_weather(self, solar_flux: float | None, kp: float | None) -> None:
        self.preferences.solar_flux = solar_flux
        self.preferences.kp = kp

    def is_unconfirmed_lotw_dxcc(self, station: str) -> bool:
        """Return whether a station belongs to a known, not-yet-confirmed entity."""
        confirmed = self.preferences.lotw_confirmed_dxcc_entities
        if not self.preferences.unconfirmed_dxcc_only or confirmed is None:
            return False
        metadata = self.dxcc_resolver.resolve(station.strip().upper())
        entity = metadata.primary_prefix.strip().upper()
        return bool(entity and entity not in confirmed)

    def ignore_station(self, station: str, until: datetime) -> None:
        self._ignored_until[station.strip().upper()] = until

    def evaluate(
        self,
        event: DecodeEvent,
        kind: CandidateKind,
        worked: WorkedCheck | None = None,
        evaluated_at: datetime | None = None,
    ) -> ScoringResult:
        station = event.parsed.sender.strip().upper()
        metadata = self.dxcc_resolver.resolve(station)
        empty = ScoreBreakdown(0, ())
        ignored_until = self._ignored_until.get(station)
        if ignored_until is not None:
            if (evaluated_at or event.observed_at) < ignored_until:
                return ScoringResult(False, f"ignored until {ignored_until.isoformat()}", metadata, empty)
            del self._ignored_until[station]
        if station in self.preferences.blacklist:
            return ScoringResult(False, "blacklisted", metadata, empty)
        if self.preferences.rare_stations_only and station not in self.preferences.special_calls:
            return ScoringResult(False, "rare-only mode", metadata, empty)
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
                reason = "worked previously" if worked.scope == "history" else "worked today"
                return ScoringResult(False, f"{reason} band={worked.band}", metadata, empty)

        components: list[ScoreComponent] = []
        force_priority = False
        if kind is CandidateKind.DIRECT_CALLER and self.preferences.direct_call_policy is DirectCallPolicy.ALWAYS_PRIORITY:
            components.append(ScoreComponent("DIRECT_CALL", self.preferences.direct_call_bonus))
            force_priority = True
        if self.preferences.prioritize_special_calls and station in self.preferences.special_calls:
            components.append(ScoreComponent("SPECIAL_CALL", self.preferences.special_call_priority_bonus))
            force_priority = True
        if self.preferences.unconfirmed_dxcc_only:
            confirmed = self.preferences.lotw_confirmed_dxcc_entities
            entity = metadata.primary_prefix.strip().upper()
            if confirmed is not None and entity and entity not in confirmed:
                # A fresh LoTW DXCC wins the current decode batch, while an
                # ordinary CQ remains eligible as the fallback.
                components.append(ScoreComponent("NEW_LOTW_DXCC", self.preferences.unconfirmed_dxcc_priority_bonus))
        if self.preferences.prioritize_favorable_path and kind is CandidateKind.CQ and event.frequency is not None:
            from wsjtx_autopilot.worked.bands import BandResolver

            probability = reply_path_probability(
                self.preferences.local_grid,
                event.parsed.grid or "",
                BandResolver().resolve(event.frequency) or "",
                event.observed_at,
                self.preferences.solar_flux,
                self.preferences.kp,
            )
            if probability:
                components.append(
                    ScoreComponent(
                        "FAVORABLE_PATH",
                        round(probability * self.preferences.favorable_path_bonus / 100),
                    )
                )
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
