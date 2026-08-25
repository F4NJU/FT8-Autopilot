from dataclasses import dataclass

from .dxcc import DxccResolver, UnknownDxccResolver
from .models import CqType, ParsedMessage, StationProfile


@dataclass(frozen=True, slots=True)
class CqEligibilityResult:
    accepted: bool
    reason: str


class CqEligibility:
    def __init__(
        self,
        station_profile: StationProfile,
        resolver: DxccResolver | None = None,
        respond_to_cq_dx: bool = False,
    ) -> None:
        self.station_profile = station_profile
        self.resolver = resolver or UnknownDxccResolver()
        self.respond_to_cq_dx = respond_to_cq_dx

    def evaluate(self, message: ParsedMessage) -> CqEligibilityResult:
        if message.cq_type in {None, CqType.GENERAL, CqType.ACTIVITY}:
            return CqEligibilityResult(True, "CQ eligible")
        target = message.cq_target or message.cq_modifier or "unknown"
        local = self.station_profile.metadata
        if message.cq_type is CqType.DIRECTED_CONTINENT:
            continent = local.continent.upper()
            if continent == "UNKNOWN":
                return CqEligibilityResult(False, f"directed CQ target={target} local=unknown")
            if continent != target:
                return CqEligibilityResult(False, f"directed CQ target={target} local={continent}")
            return CqEligibilityResult(True, f"directed CQ target={target} local={continent}")
        if target == "DX":
            reason = "CQ DX enabled" if self.respond_to_cq_dx else "CQ DX disabled by policy"
            return CqEligibilityResult(self.respond_to_cq_dx, reason)
        if message.cq_type is CqType.DIRECTED_PREFIX:
            target_metadata = self.resolver.resolve(target)
            local_entity = local.dxcc_entity.upper()
            target_entity = target_metadata.dxcc_entity.upper()
            if "UNKNOWN" in {local_entity, target_entity}:
                return CqEligibilityResult(False, f"unknown directed CQ target={target}")
            if local_entity != target_entity:
                return CqEligibilityResult(False, f"directed CQ target={target} local={local.primary_prefix.upper()}")
            return CqEligibilityResult(True, f"directed CQ target={target} local={local.primary_prefix.upper()}")
        return CqEligibilityResult(False, f"unknown directed CQ target={target}")
