import logging
from datetime import datetime

from wsjtx_autopilot.control.base import Endpoint
from wsjtx_autopilot.engine.models import ActionOutcome, IntendedAction, OriginalDecode
from wsjtx_autopilot.wsjtx.models import WsjtxPacket

LOGGER = logging.getLogger(__name__)


class DryRunControl:
    @property
    def actions_used(self) -> int:
        return 0

    @property
    def max_actions(self) -> int | None:
        return None

    def observe(self, packet: WsjtxPacket, endpoint: Endpoint | None) -> None:
        pass

    def execute(self, action: IntendedAction, now: datetime) -> ActionOutcome:
        LOGGER.info("[CONTROL] implementation=%s armed=False station=%s", type(self).__name__, action.station)
        LOGGER.info("[WOULD_ACTION] %s %s (%s)", action.kind.name.lower().replace("_", "-"), action.station, action.reason)
        LOGGER.info("[CONTROL] outcome=%s station=%s", ActionOutcome.PROPOSED_ONLY.name, action.station)
        return ActionOutcome.PROPOSED_ONLY

    def poll(self) -> None:
        pass

    def disarm(self, reason: str = "software kill switch") -> None:
        pass

    def halt_tx(self, instance_id: str | None, reason: str) -> bool:
        LOGGER.warning("[WOULD_ACTION] HALT_TX instance=%s reason=%s", instance_id or "unknown", reason)
        return False

    def retry_final(self, decode: OriginalDecode, reason: str) -> bool:
        LOGGER.info("[WOULD_ACTION] RETRY_FINAL_73 station_decode=%s reason=%s", decode.message, reason)
        return False

    def set_tx_period(self, instance_id: str, tx_first: bool) -> bool:
        LOGGER.info("[WOULD_ACTION] SET_TX_PERIOD instance=%s first=%s", instance_id, tx_first)
        return False

    def set_dial_frequency(self, instance_id: str, frequency_hz: int) -> bool:
        LOGGER.info("[WOULD_ACTION] SET_DIAL_FREQUENCY instance=%s frequency=%d", instance_id, frequency_hz)
        return False
