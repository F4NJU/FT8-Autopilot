import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.base import ControlAdapter, Endpoint
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.adaptive import AdaptiveState, AttemptOutcome, AttemptRecord, BandOpportunityTracker, DEFAULT_BAND_PROFILES, StagnationTracker
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, FallbackDxccResolver, PrefixDxccResolver
from wsjtx_autopilot.engine.finalization import FinalizationTracker
from wsjtx_autopilot.engine.location import StationLocatorCache, distance_and_bearing, grid_to_location
from wsjtx_autopilot.engine.models import ActionOutcome, Candidate, CooldownKind, DecodeEvent, IntendedAction, MessageKind, OriginalDecode, StationMetadata, StationProfile
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.progress import QsoProgressStage
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.ftx1 import FTX1BandDriveController, FTX1CatController
from wsjtx_autopilot.hamlib import HamlibFtx1Controller
from wsjtx_autopilot.logging_setup import current_logging_session
from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    QsoLoggedPacket,
    StatusPacket,
    TxAudioAttenuationStatePacket,
    WsjtxPacket,
)
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.bands import BandResolver

LOGGER = logging.getLogger(__name__)
_DECODE_MODE_MARKERS = {"~": "FT8", "+": "FT4"}
_AUTOMATION_MODES = frozenset({"FT8", "FT4"})
_ATTENUATION_QUERY_INTERVAL = timedelta(seconds=2)
_UNKNOWN_TR_PERIOD = 0xFFFFFFFF
_SWR_TUNE_TIMEOUT = timedelta(seconds=15)
_SWR_TUNE_RF_START_TIMEOUT = timedelta(seconds=3)
_SWR_TUNE_POLL_INTERVAL = timedelta(seconds=0.5)
_SWR_TUNE_MIN_SAMPLES = 2
_SWR_RX_CONFIRM_TIMEOUT = timedelta(seconds=5)
_WATCHDOG_HALT_CONFIRM_TIMEOUT = timedelta(seconds=2)
# Automatic tuner/band recovery is intentionally parked for now.  Keep the
# implementation below so it can be revisited without reintroducing it into
# the live safety path.
_SWR_AUTOMATIC_RECOVERY_ENABLED = False


def _tr_period_seconds(raw_period: int) -> int | None:
    """Normalize the WSJT-X T/R period to whole seconds.

    WSJT-X variants have emitted this field both as seconds (15) and as
    milliseconds (15000).  Round millisecond values up so AutoPilot never
    resumes in the middle of a transmit/receive period.
    """
    if raw_period == _UNKNOWN_TR_PERIOD or raw_period <= 0:
        return None
    if raw_period >= 1000:
        return max(1, (raw_period + 999) // 1000)
    return raw_period


@dataclass(slots=True)
class WsjtxStatus:
    dial_frequency: int | None = None
    mode: str | None = None
    dx_call: str = ""
    tx_enabled: bool = False
    transmitting: bool = False
    tx_df: int | None = None
    tx_message: str = ""
    period: int | None = None
    instance_id: str = ""
    local_grid: str = ""


@dataclass(slots=True)
class PendingDialFrequency:
    instance_id: str
    frequency_hz: int
    band: str
    deadline: datetime
    previous_band: str | None
    source: str = "adaptive"


@dataclass(frozen=True, slots=True)
class HaltTxResult:
    radio_ok: bool
    wsjtx_ok: bool
    observed_ptt: bool | None = None


@dataclass(slots=True)
class PendingWatchdogHalt:
    instance_id: str
    remote: str
    deadline: datetime


@dataclass(slots=True)
class SwrRecovery:
    phase: str
    origin_band: str | None
    current_band: str | None
    rx_deadline: datetime | None = None
    original_power_fraction: float | None = None
    tune_started_at: datetime | None = None
    next_poll_at: datetime | None = None
    seen_rf: bool = False
    swr_samples: list[float] | None = None
    tune_attempts: int = 0

    def __post_init__(self) -> None:
        if self.swr_samples is None:
            self.swr_samples = []


class AutopilotRuntime:
    """Connect parsed WSJT-X packets to the dry-run decision engine."""

    def __init__(
        self,
        config: AppConfig,
        engine: DecisionEngine | None = None,
        control: ControlAdapter | None = None,
        worked_service: WorkedTodayService | None = None,
        ftx1_band_drive: FTX1BandDriveController | None = None,
        ftx1_band_profiles_changed: Callable[[dict[str, dict[str, int]]], None] | None = None,
    ) -> None:
        self.config = config
        self.worked_service = worked_service
        if engine is None:
            resolver = FallbackDxccResolver(CtyDatResolver(config.cty_dat_path)) if config.cty_dat_path else PrefixDxccResolver()
            local_profile = StationProfile(config.local_callsign, resolver.resolve(config.local_callsign))
            engine = DecisionEngine(
                config,
                worked_service.check if worked_service is not None else None,
                cq_eligibility=CqEligibility(local_profile, resolver, config.respond_to_cq_dx),
            )
        self.engine = engine
        self.control = control or DryRunControl()
        self.status = WsjtxStatus()
        self.station_locator_cache = StationLocatorCache()
        self._last_reported_band: str | None = None
        self._last_runtime_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        self.current_tx_audio_attenuation: int | None = None
        self.pending_tx_audio_attenuation: int | None = None
        self._pending_ftx1_profile_save_band: str | None = None
        self.tx_audio_attenuation_last_update: datetime | None = None
        self._tx_audio_attenuation_deadline: datetime | None = None
        self._attenuation_query_instance_id = ""
        self._last_attenuation_query_at: datetime | None = None
        self._hamlib_next_meter_at: datetime | None = None
        self._hamlib_tx_started_at: datetime | None = None
        self._hamlib_tx_active = False
        self._hamlib_alc_samples: list[float] = []
        self._hamlib_power_samples: list[float] = []
        self._hamlib_zero_power_samples = 0
        self._hamlib_high_swr_samples = 0
        self._hamlib_swr_fault_latched = False
        self._swr_recovery: SwrRecovery | None = None
        self._swr_excluded_bands: set[str] = set()
        self.antenna_safety_level = "ok"
        self.antenna_safety_events: list[str] = []
        self._pending_alc_profile_band: str | None = None
        self.last_hamlib_alc: float | None = None
        self.last_hamlib_swr: float | None = None
        self.last_hamlib_power_watts: float | None = None
        self.last_hamlib_requested_power_watts: float | None = None
        self.last_qso_notice = ""
        self._watchdog_halt_failed = False
        self._pending_watchdog_halt: PendingWatchdogHalt | None = None
        self.finalization = FinalizationTracker(
            config.finalization_hold_periods,
            config.max_final_retries,
            config.final_tx_timeout_periods,
        )
        self._clear_epochs: dict[str, int] = {}
        self._pending_dial_frequency: PendingDialFrequency | None = None
        self._pending_ftx1_band_change: PendingDialFrequency | None = None
        self._pending_ftx1_profile_apply: PendingDialFrequency | None = None
        self._blocked_band_drive_change: PendingDialFrequency | None = None
        self.ftx1_band_drive = ftx1_band_drive
        direct_ftx1_allowed = config.ftx1_cat2_confirmed_ftx1 and not config.ftx1_hamlib_enabled
        if self.ftx1_band_drive is None and config.ftx1_cat2_enabled and (config.ftx1_hamlib_enabled or direct_ftx1_allowed):
            vendor_cat = None if config.ftx1_hamlib_enabled else FTX1CatController(
                config.ftx1_cat2_port, config.ftx1_cat2_baudrate, config.ftx1_cat2_timeout_seconds
            )
            radio = (
                HamlibFtx1Controller(
                    config.ftx1_cat2_port,
                    config.ftx1_cat2_baudrate,
                    config.ftx1_cat2_timeout_seconds,
                    config.ftx1_hamlib_rigctld_path,
                    config.ftx1_hamlib_tcp_port,
                    None,
                    rig_model=config.hamlib_rig_model,
                )
                if config.ftx1_hamlib_enabled
                else vendor_cat
            )
            self.ftx1_band_drive = FTX1BandDriveController(
                radio,
                config.ftx1_band_profiles,
                ftx1_band_profiles_changed,
                self._request_ftx1_profile_attenuation,
                lambda: self.status.transmitting,
            )
        elif self.ftx1_band_drive is not None:
            # Runtime owns the RX gate and AP1 confirmation path for injected test/custom controllers too.
            self.ftx1_band_drive._set_tx_audio_attenuation = self._request_ftx1_profile_attenuation
            self.ftx1_band_drive._is_transmitting = lambda: self.status.transmitting
        self.ap1_controls_available = False
        self.wsjtx_revision = ""
        self.current_tx_first: bool | None = None
        self.requested_tx_period: bool | None = None
        self.adaptive_state = AdaptiveState.NORMAL
        self._adaptive_pending_origin: AdaptiveState | None = None
        self.stagnation = StagnationTracker(
            config.stagnation_attempt_window,
            config.stagnation_min_failed_attempts,
            config.stagnation_max_unique_calls,
        )
        self.opportunities = BandOpportunityTracker()
        self.last_band_change_at: datetime | None = None
        self._band_resume_at: datetime | None = None
        self._band_resume_band: str | None = None
        self._forced_band_hop_dwell_started_at: datetime | None = None
        self._forced_band_hop_dwell_band: str | None = None
        self._forced_band_hop_pending_band: str | None = None
        self._forced_band_hop_config_warning_logged = False
        if isinstance(self.control, WsjtxUdpControl):
            self.control.set_disarm_context(self._disarm_context)
        session = current_logging_session()
        if session is not None:
            session.context.feature_flags = {
                "autocall": config.autocall_enabled,
                "direct_reply_patch": config.wsjtx_direct_reply_patched,
            }

    def initialize_ftx1_cat2(self) -> bool:
        """Open and identify the configured CAT-2 port during backend startup."""
        LOGGER.info(
            "[FTX1] CAT-2 config enabled=%s port=%s baud=%d hamlib=%s",
            self.config.ftx1_cat2_enabled,
            self.config.ftx1_cat2_port,
            self.config.ftx1_cat2_baudrate,
            self.config.ftx1_hamlib_enabled,
        )
        if not self.config.ftx1_cat2_enabled:
            return False
        if not self.config.ftx1_hamlib_enabled and not self.config.ftx1_cat2_confirmed_ftx1:
            return False
        if self.ftx1_band_drive is None:
            LOGGER.error("[FTX1] CAT-2 open failed: controller is unavailable")
            return False
        if not self.ftx1_band_drive.cat.identify():
            return False
        detected_model = getattr(
            self.ftx1_band_drive.cat,
            "detected_radio_model",
            "Yaesu FTX-1" if not self.config.ftx1_hamlib_enabled else "Hamlib generic",
        )
        LOGGER.info(
            "[RADIO] control ready source=%s backend_model=%s detected=%s",
            "Hamlib" if self.config.ftx1_hamlib_enabled else "direct FTX-1",
            self.config.hamlib_rig_model if self.config.ftx1_hamlib_enabled else "direct",
            detected_model,
        )
        return True

    def working_location_detail(self, callsign: str, metadata: StationMetadata) -> str:
        """Return observed-grid distance first, then an approximate DXCC reference."""
        local = grid_to_location(self.status.local_grid)
        if local is None:
            return ""
        observed = self.station_locator_cache.get(callsign)
        if observed is not None:
            result = distance_and_bearing(local, observed)
            if result is not None:
                distance, bearing = result
                return f"{distance} km {bearing}°"
        result = distance_and_bearing(local, metadata)
        if result is None:
            return ""
        distance, bearing = result
        return f"~{distance} km ~{bearing}°"

    def arm_control(self) -> bool:
        """Arm the live UDP control adapter on an explicit GUI request."""
        LOGGER.info("[RUNTIME] ARM request received")
        if not isinstance(self.control, WsjtxUdpControl):
            LOGGER.error("[RUNTIME] ARM request rejected: control is not WsjtxUdpControl")
            return False
        if (
            self.config.ftx1_cat2_enabled
            and self.config.ftx1_hamlib_enabled
            and (self.config.ftx1_alc_control_enabled or self.config.ftx1_swr_protection_enabled)
        ):
            radio_ready = bool(
                self.ftx1_band_drive is not None
                and self.ftx1_band_drive.cat.identify()
            )
            if not radio_ready:
                # A transient CAT timeout must not lock the whole WSJT-X
                # automation in DISARM. Meter reads retry Hamlib while TX is
                # active; SWR protection can therefore recover as soon as the
                # radio answers again.
                LOGGER.warning(
                    "[HAMLIB] radio source unavailable during ARM; continuing armed with CAT monitoring pending"
                )
        if self._hamlib_swr_fault_latched:
            LOGGER.info("[HAMLIB] SWR safety latch reset by explicit ARM request")
            self._hamlib_swr_fault_latched = False
            self._hamlib_high_swr_samples = 0
            self.antenna_safety_level = "ok"
            self._swr_recovery = None
            self._antenna_event(
                self._last_runtime_at or datetime.now(timezone.utc),
                "Protection SWR réarmée par l'opérateur",
            )
        armed = self.control.arm()
        if armed:
            self._pending_watchdog_halt = None
            self._watchdog_halt_failed = False
            if self.last_qso_notice.startswith("SWR SAFETY"):
                self.last_qso_notice = ""
            band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            self.opportunities.reset(band, self.status.mode, self._last_runtime_at or datetime.now(timezone.utc))
        return armed

    def _disarm_context(self) -> dict[str, object]:
        heartbeat_age_seconds: float | None = None
        if self._last_runtime_at is not None and self._last_heartbeat_at is not None:
            heartbeat_age_seconds = round((self._last_runtime_at - self._last_heartbeat_at).total_seconds(), 1)
        return {
            "wsjtx_online": bool(self.status.instance_id),
            "heartbeat_age_seconds": heartbeat_age_seconds if heartbeat_age_seconds is not None else "unknown",
            "transmitting": self.status.transmitting,
            "learning_state": "DISABLED",
            "exception": "none",
        }

    def _request_halt_tx(self, instance_id: str | None, reason: str) -> HaltTxResult:
        """Request hardware PTT OFF and cancel WSJT-X's TX scheduler."""
        if isinstance(self.control, DryRunControl):
            wsjtx_ok = self.control.halt_tx(instance_id, reason)
            return HaltTxResult(True, wsjtx_ok, False)
        cat = self.ftx1_band_drive.cat if self.ftx1_band_drive is not None else None
        stop_transmit = getattr(cat, "stop_transmit", None)
        read_ptt = getattr(cat, "read_ptt", None)
        radio_ok = True
        observed_ptt: bool | None = None
        if callable(stop_transmit):
            radio_ok = bool(stop_transmit())
            observed_ptt = read_ptt() if radio_ok and callable(read_ptt) else None
            if observed_ptt is True:
                radio_ok = False
            LOGGER.warning(
                "[HAMLIB] PTT OFF reason=%s command_ok=%s observed_ptt=%s",
                reason,
                radio_ok,
                observed_ptt,
            )
        wsjtx_ok = self.control.halt_tx(instance_id, reason)
        return HaltTxResult(radio_ok, wsjtx_ok, observed_ptt)

    def _halt_tx(self, instance_id: str | None, reason: str) -> bool:
        """Drop hardware PTT through Hamlib, then cancel WSJT-X's TX scheduler."""
        result = self._request_halt_tx(instance_id, reason)
        if not result.radio_ok and result.wsjtx_ok and not self.status.transmitting:
            LOGGER.warning(
                "[HAMLIB] PTT OFF confirmation unavailable, but WSJT-X is already in RX; "
                "continuing without disarming"
            )
            return True
        return result.radio_ok and result.wsjtx_ok

    def handle(
        self,
        packet: WsjtxPacket | None,
        now: datetime,
        endpoint: Endpoint | None = None,
    ) -> IntendedAction | None:
        self._last_runtime_at = now
        self.control.poll()
        if packet is not None:
            self.control.observe(packet, endpoint)
        handled_action: IntendedAction | None = None
        if isinstance(packet, HeartbeatPacket):
            self._last_heartbeat_at = now
            source = f"{endpoint[0]}:{endpoint[1]}" if endpoint is not None else "unknown"
            LOGGER.info(
                "[WSJTX] heartbeat instance=%s schema=%d version=%s endpoint=%s",
                packet.header.instance_id,
                packet.header.schema,
                packet.version,
                source,
            )
            self.wsjtx_revision = packet.revision
            self.ap1_controls_available = "AP1" in packet.revision.upper()
            LOGGER.info("[WSJTX] revision=%s", packet.revision)
            LOGGER.info("[WSJTX] AP1 controls available=%s", str(self.ap1_controls_available).lower())
            if self._attenuation_query_instance_id and self._attenuation_query_instance_id != packet.header.instance_id:
                self.invalidate_tx_audio_attenuation_state("WSJT-X instance changed")
            if self.ap1_controls_available:
                self._query_tx_audio_attenuation_if_needed(packet.header.instance_id, now)
            else:
                self.invalidate_tx_audio_attenuation_state("AP1 is unavailable")
            session = current_logging_session()
            if session is not None:
                session.context.wsjtx_version = packet.version
                session.context.wsjtx_schema = packet.header.schema
                session.context.wsjtx_instance = packet.header.instance_id
                session.context.wsjtx_endpoint = source
                session.context.wsjtx_revision = packet.revision
                session.context.ap1_controls_available = self.ap1_controls_available
        elif isinstance(packet, TxAudioAttenuationStatePacket):
            self._observe_tx_audio_attenuation(packet.attenuation, now)
            self._finish_pending_ftx1_profile_apply(now)
            self._complete_pending_ftx1_band_change(now)
        elif isinstance(packet, StatusPacket):
            was_transmitting = self.status.transmitting
            period = _tr_period_seconds(packet.tr_period)
            self.status = WsjtxStatus(
                instance_id=packet.header.instance_id,
                dial_frequency=packet.dial_frequency,
                mode=packet.mode,
                dx_call=packet.dx_call,
                tx_enabled=packet.tx_enabled,
                transmitting=packet.transmitting,
                tx_df=packet.tx_df,
                tx_message=packet.tx_message,
                period=period,
                local_grid=packet.de_grid,
            )
            self.station_locator_cache.note(packet.dx_call, packet.dx_grid)
            band = BandResolver().resolve(packet.dial_frequency) or "unknown"
            self.opportunities.ensure_context(band, packet.mode or "unknown", now)
            self.engine.invalidate_pending_context(packet.header.instance_id, band, packet.mode or "unknown")
            session = current_logging_session()
            if session is not None:
                session.context.band = band
                session.context.mode = packet.mode or "unknown"
            self._handle_wsjtx_tx_watchdog(packet, now)
            self._observe_finalization_status(packet, now, was_transmitting)
            self._observe_adaptive_status(packet, now)
            self._confirm_dial_frequency(packet, now)
            self._observe_manual_band_change(packet, now)
            self._observe_forced_band_hop_status(now)
            if was_transmitting != packet.transmitting:
                LOGGER.info("[WSJTX] transmitting=%s -> %s", was_transmitting, packet.transmitting)
            LOGGER.info(
                "[STATUS] %d Hz %s DX=%s TX=%s transmitting=%s TX_DF=%d engine_state=%s",
                packet.dial_frequency,
                packet.mode,
                packet.dx_call or "-",
                "on" if packet.tx_enabled else "off",
                "yes" if packet.transmitting else "no",
                packet.tx_df,
                self.engine.state.session.state.name,
            )
            if self._status_confirms_reply(packet):
                if self.engine.state.session.state is QsoState.DIRECT_REPLY_SENT:
                    self.engine.state.confirm_direct_reply(now)
                    self.engine.confirm_direct_reply(packet.dx_call)
                else:
                    self.engine.state.confirm_reply(now)
                LOGGER.info("[ENGINE] QSO active remote=%s", self.engine.state.session.remote_callsign)
        elif isinstance(packet, DecodePacket):
            self._handle_decode(packet, now, endpoint)
        elif isinstance(packet, ClearPacket):
            instance_id = packet.header.instance_id
            self._clear_epochs[instance_id] = self._clear_epochs.get(instance_id, 0) + 1
            self.engine.invalidate_instance_decodes(instance_id)
            LOGGER.info("[WSJTX] Clear instance=%s window=%s", instance_id, packet.window)
        elif isinstance(packet, QsoLoggedPacket):
            LOGGER.info(
                "[QSO id=%s] QSOLogged station=%s frequency=%d mode=%s instance=%s",
                self.engine.state.session.qso_id or "-",
                packet.dx_call,
                packet.tx_frequency,
                packet.mode,
                packet.header.instance_id,
            )
            if self.worked_service is not None:
                self.worked_service.record_qso_logged(packet)
            if BandResolver().resolve(packet.tx_frequency) == self.opportunities.band:
                self.opportunities.mark_worked(packet.dx_call)
            if self.engine.state.matches_remote(packet.dx_call, packet.header.instance_id):
                self._adaptive_success()
            if self.finalization.note_qso_logged(packet.dx_call, packet.header.instance_id):
                LOGGER.info(
                    "[FINALIZE] QSOLogged retained remote=%s waiting local TX",
                    packet.dx_call,
                )
            elif (
                self.status.transmitting
                and self.engine.state.matches_remote(packet.dx_call, packet.header.instance_id)
                and self.finalization.begin_qso_logged_hold(
                    packet.dx_call,
                    packet.header.instance_id,
                    self.status.mode or packet.mode,
                    self.status.dial_frequency,
                    self.status.period or 15,
                    now,
                )
            ):
                LOGGER.info(
                    "[FINALIZE] QSOLogged retained remote=%s current transmission still active",
                    packet.dx_call,
                )
            elif self.engine.complete_qso(packet.dx_call, packet.header.instance_id, "QSOLogged received"):
                self._log_ready_for_next_qso()

        self._poll_watchdog_halt_confirmation(now)

        remote_before_expiry = self.engine.state.session.remote_callsign
        if self.engine.state.expire_direct_reply(
            now,
            self.config.direct_reply_confirmation_timeout_seconds,
        ):
            self.engine.retain_unconfirmed_direct_reply(remote_before_expiry, now)
            self.note_adaptive_failure(remote_before_expiry, AttemptOutcome.NO_RESPONSE, now)
        remote_before_expiry = self.engine.state.session.remote_callsign
        if self.engine.state.expire_reply_confirmation(
            now,
            self.config.direct_reply_confirmation_timeout_seconds,
        ):
            self.note_adaptive_failure(remote_before_expiry, AttemptOutcome.NO_RESPONSE, now)
        self._maintain_qso_lifecycle(now)
        self._poll_hamlib_meters(now)
        self._run_swr_recovery(now)
        final_state = self.finalization.state
        if self.finalization.expire(now) and final_state is not None and not final_state.tx_completed:
            LOGGER.error(
                "[FINALIZE] local terminal TX missing remote=%s wsjtx_tx_enabled=%s engine_state=%s qso_logged=%s",
                final_state.remote_callsign,
                self.status.tx_enabled,
                self.engine.state.session.state.name,
                final_state.qso_logged,
            )
            if self.engine.complete_qso(
                final_state.remote_callsign,
                final_state.instance_id,
                "final TX timeout",
            ):
                self._log_ready_for_next_qso()
        self._expire_dial_frequency_request(now)
        self._expire_tx_audio_attenuation_request(now)
        self._query_tx_audio_attenuation_if_needed(self._attenuation_query_instance_id, now)
        self._complete_pending_ftx1_band_change(now)
        self._run_forced_band_hop(now)
        if (
            self._pending_watchdog_halt is None
            and self._pending_dial_frequency is None
            and self._pending_ftx1_band_change is None
            and self._pending_ftx1_profile_apply is None
        ):
            self._run_adaptive(now)
        if (
            self._pending_watchdog_halt is not None
            or self._pending_dial_frequency is not None
            or self._pending_ftx1_band_change is not None
            or self._pending_ftx1_profile_apply is not None
            or self.pending_tx_audio_attenuation is not None
            or self._blocked_band_drive_change is not None
            or self._swr_recovery is not None
        ):
            action = None
        elif self.finalization.active:
            LOGGER.debug("[FINALIZE] candidate selection held until final TX and grace complete")
            action = None
        elif self._band_resume_at is not None and now < self._band_resume_at:
            action = None
        else:
            if self._band_resume_band is not None:
                LOGGER.info("[BAND-HOP] Resuming Autopilot on %s", self._band_resume_band)
                self._band_resume_band = None
            self._band_resume_at = None
            action = self.engine.decide(now)
        if action is not None:
            LOGGER.info(
                "[RUNTIME] requesting control action station=%s kind=%s implementation=%s armed=%s",
                action.station,
                action.kind.name,
                type(self.control).__name__,
                getattr(self.control, "armed", False),
            )
            self._execute_action(action, now)
        self._update_diagnostic_context(now)
        session = self.engine.state.session
        LOGGER.debug("[ENGINE] state=%s remote=%s", session.state.name, session.remote_callsign or "-")
        return action or handled_action

    def _handle_decode(
        self,
        packet: DecodePacket,
        now: datetime,
        endpoint: Endpoint | None,
    ) -> Candidate | None:
        LOGGER.info(
            "[DECODE] %+d %+.1f %d %s",
            packet.snr,
            packet.delta_time,
            packet.delta_frequency,
            packet.message,
        )
        mode = self._automation_mode(packet.mode)
        if packet.is_new and mode is not None:
            self.engine.note_decode_arrival(
                (packet.header.instance_id, packet.decode_time, mode),
                now,
            )
        if not packet.is_new:
            LOGGER.info("[ENGINE] type=ignored reason=replayed decode text=%s", packet.message)
            return None
        if packet.low_confidence:
            LOGGER.info("[ENGINE] type=ignored reason=low confidence text=%s", packet.message)
            self._log_rejected_direct_packet(packet, "LOW_CONFIDENCE")
            return None
        if packet.off_air:
            LOGGER.info("[ENGINE] type=ignored reason=off-air Decode text=%s", packet.message)
            self._log_rejected_direct_packet(packet, "OFF_AIR")
            return None

        if mode is None:
            LOGGER.info(
                "[ENGINE] type=ignored reason=unsupported mode decode_mode=%r status_mode=%r text=%s",
                packet.mode,
                self.status.mode,
                packet.message,
            )
            return None
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency is not None else None
        parsed = parse_ft8_message(packet.message)
        if parsed is None:
            LOGGER.info("[ENGINE] type=ignored reason=ambiguous/free-text text=%s", packet.message)
            return None

        if parsed.kind in {MessageKind.CQ, MessageKind.QRZ}:
            detected_type = parsed.kind.name
        elif parsed.is_addressed_to(self.config.local_callsign):
            detected_type = "direct call"
        else:
            detected_type = "exchange"
        LOGGER.info(
            "[ENGINE] parsed type=%s sender=%s text=%s decode_mode=%r mode=%s",
            detected_type,
            parsed.sender,
            parsed.raw,
            packet.mode,
            mode,
        )
        identity = (
            f"{packet.header.instance_id}|{packet.decode_time.isoformat()}|"
            f"{packet.delta_frequency}|{packet.message}"
        )
        event = DecodeEvent(
            parsed=parsed,
            observed_at=now,
            mode=mode,
            snr=packet.snr,
            frequency=self.status.dial_frequency,
            period=self.status.period,
            unique_id=identity,
            original=OriginalDecode(
                instance_id=packet.header.instance_id,
                schema=packet.header.schema,
                decode_time=packet.decode_time,
                snr=packet.snr,
                delta_time=packet.delta_time,
                delta_frequency=packet.delta_frequency,
                mode=packet.mode,
                message=packet.message,
                low_confidence=packet.low_confidence,
                is_new=packet.is_new,
                source_endpoint=endpoint,
                clear_epoch=self._clear_epochs.get(packet.header.instance_id, 0),
                off_air=packet.off_air,
            ),
        )
        if parsed.kind is MessageKind.SEVENTY_THREE and self.finalization.state is not None:
            if parsed.sender == self.finalization.state.remote_callsign:
                self.finalization.close("remote 73 received")
                self.last_qso_notice = ""
        if parsed.kind in {MessageKind.RRR, MessageKind.RR73} and parsed.is_addressed_to(self.config.local_callsign):
            final_state = self.finalization.state
            session = self.engine.state.session
            terminal_progress = (
                final_state is not None
                and final_state.last_terminal_kind is MessageKind.RRR
                and parsed.kind is MessageKind.RR73
                and session.state is not QsoState.IDLE
                and session.remote_callsign == parsed.sender
            )
            if terminal_progress:
                self.finalization.advance_terminal(event, now)
            elif self.finalization.matches_retry(event):
                session = self.engine.state.session
                if self.finalization.same_terminal_period(event):
                    LOGGER.info(
                        "[FINALIZE] duplicate terminal Decode ignored remote=%s type=%s",
                        parsed.sender,
                        parsed.kind.name,
                    )
                    return None
                new_qso_active = (
                    session.state is not QsoState.IDLE
                    and (
                        session.remote_callsign not in {None, parsed.sender}
                        or (
                            final_state is not None
                            and session.reply_sent_at is not None
                            and session.reply_sent_at > final_state.started_at
                        )
                    )
                )
                if new_qso_active:
                    LOGGER.info(
                        "[FINALIZE] old terminal retry ignored remote=%s active_remote=%s state=%s",
                        parsed.sender,
                        session.remote_callsign,
                        session.state.name,
                    )
                    return None
                LOGGER.info(
                    "[FINALIZE] repeated terminal message remote=%s type=%s",
                    parsed.sender,
                    parsed.kind.name,
                )
                if not self.finalization.can_retry():
                    LOGGER.info("[FINALIZE] retry limit reached remote=%s", parsed.sender)
                    self.finalization.close("retry limit reached")
                    return
                if event.original is not None and self.control.retry_final(event.original, "repeated terminal message"):
                    count = self.finalization.record_retry(event, now)
                    self.last_qso_notice = (
                        f"{parsed.sender} repeats {parsed.kind.name}; retransmission 73 "
                        f"{count}/{self.config.max_final_retries}"
                    )
                    if count >= self.config.max_final_retries:
                        LOGGER.info("[FINALIZE] retry limit reached remote=%s", parsed.sender)
                return None
            if not terminal_progress and session.state is not QsoState.IDLE and session.remote_callsign == parsed.sender:
                if self.finalization.begin(event, now):
                    self.last_qso_notice = f"QSO complete - final confirmation {parsed.sender}"
        remote_cq = self.engine.remote_cq_during_attempt(event, now)
        if remote_cq is not None:
            session = self.engine.state.session
            if remote_cq == "tolerated":
                self.last_qso_notice = (
                    f"Remote CQ {parsed.sender}: {session.remote_cq_count}/"
                    f"{self.config.max_remote_cq_during_attempt}"
                )
                return None
            if remote_cq == "ignored":
                return None
            self.last_qso_notice = f"QSO abandoned - {parsed.sender} returned to CQ; searching candidates"
            halt_failed = False
            if self.status.tx_enabled and self.status.dx_call.upper() == parsed.sender.upper():
                halted = self._halt_tx(event.original.instance_id, "remote returned to CQ")
                if not halted and not isinstance(self.control, DryRunControl):
                    self._disarm_control("halt_tx_failed_remote_cq", "AutopilotRuntime._handle_decode")
                    self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({parsed.sender})"
                    halt_failed = True
            if not halt_failed:
                self.engine.abort_qso(
                    "remote returned to CQ",
                    now,
                    self.config.remote_returned_to_cq_cooldown_seconds,
                    CooldownKind.REMOTE_RETURNED_TO_CQ,
                    preserve_candidates=True,
                )
                self.opportunities.mark_unavailable(parsed.sender, now, self.config.remote_returned_to_cq_cooldown_seconds)
            return None
        abandoned = self.engine.remote_engaged_other(event, now)
        if abandoned is not None:
            remote, other = abandoned
            self.last_qso_notice = f"QSO abandoned - {remote} is working {other}; searching candidates"
            halt_failed = False
            if self.status.tx_enabled and self.status.dx_call.upper() == remote.upper():
                halted = self._halt_tx(event.original.instance_id, "remote engaged another station")
                if not halted and not isinstance(self.control, DryRunControl):
                    self._disarm_control("halt_tx_failed_remote_busy", "AutopilotRuntime._handle_decode")
                    self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({remote})"
                    halt_failed = True
            if halt_failed:
                return None
            self.engine.abort_qso(
                f"working {other}",
                now,
                self.config.remote_busy_cooldown_seconds,
                CooldownKind.REMOTE_BUSY_OTHER_QSO,
                preserve_candidates=True,
            )
            self.opportunities.mark_unavailable(remote, now, self.config.remote_busy_cooldown_seconds)
        direct_to_local = parsed.is_addressed_to(self.config.local_callsign) and parsed.kind in {
            MessageKind.DIRECTED,
            MessageKind.REPORT,
            MessageKind.R_REPORT,
        }
        if self.finalization.active and direct_to_local:
            self.engine.queue_pending_direct(event, now, "finalization hold")
            return None
        if direct_to_local and self._can_preempt_unanswered_cq_attempt(event):
            candidate = self.engine.observe(event, now, allow_direct_during_attempt=True)
            if candidate is None:
                return None
            previous_remote = self.engine.state.session.remote_callsign or "unknown"
            if not self._halt_tx(self.status.instance_id, "direct caller preempts unanswered CQ attempt"):
                self._disarm_control("halt_tx_failed_direct_preemption", "AutopilotRuntime._handle_decode")
                self.engine.clear_candidates()
                self.engine.queue_pending_direct(event, now, "HaltTx failed during direct preemption")
                self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({previous_remote})"
                return None
            self.engine.abort_qso(
                f"unanswered CQ attempt preempted by direct caller {parsed.sender}",
                now,
                self.config.stalled_qso_cooldown_seconds,
                preserve_candidates=True,
            )
            self.engine.queue_pending_direct(event, now, "preempted unanswered CQ attempt")
            LOGGER.info("[DIRECT] preempted unanswered CQ attempt remote=%s caller=%s", previous_remote, parsed.sender)
            return candidate
        self.station_locator_cache.note(parsed.sender, parsed.grid)
        candidate = self.engine.observe(event, now)
        if parsed.kind in {MessageKind.CQ, MessageKind.QRZ} and packet.header.instance_id == self.status.instance_id:
            worked = bool(
                self.worked_service is not None
                and self.worked_service.check(parsed.sender, self.status.dial_frequency, now).duplicate
            )
            if worked or candidate is not None or self.opportunities.is_unavailable(parsed.sender, now):
                self.opportunities.observe(parsed.sender, now, worked=worked)
                if candidate is not None and self.adaptive_state in {
                    AdaptiveState.PARITY_CHANGE_PENDING, AdaptiveState.BAND_HOP_PENDING,
                }:
                    self.adaptive_state = self._adaptive_pending_origin or AdaptiveState.NORMAL
                    self._adaptive_pending_origin = None
                    LOGGER.info("[ADAPT] New viable CQ %s; pending switch cancelled", parsed.sender)
        return candidate

    def _can_preempt_unanswered_cq_attempt(self, event: DecodeEvent) -> bool:
        blocker = self._direct_preemption_blocker(event)
        if blocker is not None:
            LOGGER.info(
                "[DIRECT] preemption unavailable station=%s blocker=%s",
                event.parsed.sender,
                blocker,
            )
            return False
        return True

    def _direct_preemption_blocker(self, event: DecodeEvent) -> str | None:
        session = self.engine.state.session
        checks = (
            (
                not (self.ap1_controls_available or self.config.wsjtx_direct_reply_patched),
                "AP1_DIRECT_REPLY_UNAVAILABLE",
            ),
            (not isinstance(self.control, WsjtxUdpControl), "CONTROL_NOT_UDP"),
            (not getattr(self.control, "armed", False), "CONTROL_DISARMED"),
            (not self.status.tx_enabled, "TX_DISABLED"),
            (self.status.transmitting, "CURRENTLY_TRANSMITTING"),
            (self.finalization.active, "FINALIZATION_ACTIVE"),
            (self._pending_dial_frequency is not None, "DIAL_CHANGE_PENDING"),
            (self._pending_ftx1_band_change is not None, "FTX1_BAND_CHANGE_PENDING"),
            (self._pending_ftx1_profile_apply is not None, "FTX1_PROFILE_PENDING"),
            (session.state is not QsoState.CALLING_STATION, f"QSO_STATE_{session.state.name}"),
            (session.progress_stage is not QsoProgressStage.CALL_OR_GRID, "QSO_ALREADY_PROGRESSED"),
            (event.original is None, "MISSING_ORIGINAL_DECODE"),
            (event.parsed.sender == session.remote_callsign, "CURRENT_REMOTE"),
        )
        for blocked, reason in checks:
            if blocked:
                return reason
        assert event.original is not None
        if session.instance_id != event.original.instance_id:
            return "DECODE_INSTANCE_MISMATCH"
        if self.status.instance_id != session.instance_id:
            return "STATUS_INSTANCE_MISMATCH"
        if session.frequency != self.status.dial_frequency:
            return "FREQUENCY_MISMATCH"
        if session.mode != event.mode:
            return "MODE_MISMATCH"
        if self.status.dx_call.upper() != session.remote_callsign:
            return "WSJTX_TARGET_MISMATCH"
        previous = parse_ft8_message(session.last_message or "")
        if previous is None or previous.kind not in {MessageKind.CQ, MessageKind.QRZ}:
            return "CURRENT_QSO_HAS_REMOTE_EXCHANGE"
        maximum = self.control.max_actions
        if maximum is not None and self.control.actions_used >= maximum:
            return "ACTION_LIMIT_REACHED"
        return None

    def _log_rejected_direct_packet(self, packet: DecodePacket, blocker: str) -> None:
        parsed = parse_ft8_message(packet.message)
        if parsed is None or not parsed.is_addressed_to(self.config.local_callsign):
            return
        LOGGER.info("[DIRECT] received station=%s df=%d", parsed.sender, packet.delta_frequency)
        LOGGER.info("[DIRECT] hard_blockers=[%s]", blocker)
        LOGGER.info("[DIRECT] soft_blockers=[]")
        LOGGER.info("[DIRECT] rejected station=%s blocker=%s", parsed.sender, blocker)

    def _execute_action(self, action: IntendedAction, now: datetime) -> None:
        outcome = self.control.execute(action, now)
        LOGGER.info("[RUNTIME] control outcome=%s station=%s", outcome.name, action.station)
        self.engine.record_action_outcome(action, outcome, now)
        if outcome is ActionOutcome.SENT:
            self.last_qso_notice = ""
            self._watchdog_halt_failed = False

    def _observe_finalization_status(
        self,
        packet: StatusPacket,
        now: datetime,
        was_transmitting: bool,
    ) -> None:
        state = self.finalization.state
        if state is None:
            return
        if packet.mode != state.mode or (
            state.frequency is not None and packet.dial_frequency != state.frequency
        ):
            self.finalization.close("mode or frequency changed")
            self.last_qso_notice = ""
            return
        tokens = packet.tx_message.upper().split()
        terminal_tx = (
            bool(tokens)
            and tokens[-1] == "73"
            and state.remote_callsign.upper() in tokens
            and self.config.local_callsign.upper() in tokens
        )
        if terminal_tx and packet.transmitting:
            self.finalization.begin_final_tx(now)
            return
        if state.tx_confirmed and was_transmitting and not packet.transmitting:
            self.finalization.confirm_final_tx()
            if state.qso_logged:
                if self.engine.complete_qso(
                    state.remote_callsign,
                    state.instance_id,
                    "QSOLogged received after final 73 completed",
                ):
                    self._log_ready_for_next_qso()
            elif self.engine.state.matches_remote(state.remote_callsign):
                self.engine.state.mark_complete("final 73 transmitted")

    def _observe_adaptive_status(self, packet: StatusPacket, now: datetime) -> None:
        if not packet.transmitting:
            return
        phase = now.second % 30 + now.microsecond / 1_000_000
        observed = True if phase <= 3 else False if 12 <= phase <= 18 else None
        if observed is None:
            return
        self.current_tx_first = observed
        if self.requested_tx_period is not None and observed != self.requested_tx_period:
            LOGGER.error("[CONTROL] SetTxPeriod validation failed requested=%s observed=%s", self.requested_tx_period, observed)
            self.requested_tx_period = None

    def _confirm_dial_frequency(self, packet: StatusPacket, now: datetime) -> None:
        pending = self._pending_dial_frequency
        if pending is None or packet.header.instance_id != pending.instance_id:
            return
        if packet.dial_frequency != pending.frequency_hz:
            return
        self._pending_dial_frequency = None
        if pending.source == "forced":
            LOGGER.info("[BAND-HOP] Frequency confirmed: %s / %.3f MHz", pending.band, pending.frequency_hz / 1_000_000)
            if self.ftx1_band_drive is None or not self.config.ftx1_auto_apply_band_profiles:
                self._block_for_unready_band_drive(pending, "automatic per-band drive profiles are unavailable")
                return
        if self._requires_band_drive_restore(pending):
            self._pending_ftx1_band_change = pending
            self._complete_pending_ftx1_band_change(now)
            return
        self._finish_band_change(pending, _tr_period_seconds(packet.tr_period), now)

    def _observe_manual_band_change(self, packet: StatusPacket, now: datetime) -> None:
        band = BandResolver().resolve(packet.dial_frequency)
        previous_band = self._last_reported_band
        self._last_reported_band = band
        if (
            band is None
            or previous_band is None
            or band == previous_band
            or self._pending_dial_frequency is not None
            or self._pending_ftx1_band_change is not None
            or self._pending_ftx1_profile_apply is not None
            or self.status.transmitting
            or self.ftx1_band_drive is None
            or not self.config.ftx1_auto_apply_band_profiles
        ):
            return
        if band.lower() not in self.ftx1_band_drive.profiles:
            LOGGER.info("[DRIVE] No saved drive profile for manually selected %s", band)
            return
        LOGGER.info("[DRIVE] Manual band change detected: restoring saved drive profile for %s", band)
        self._pending_ftx1_band_change = PendingDialFrequency(
            packet.header.instance_id,
            packet.dial_frequency,
            band,
            now + timedelta(seconds=self.config.dial_change_confirmation_timeout_seconds),
            previous_band,
            "manual",
        )
        self._complete_pending_ftx1_band_change(now)

    def _complete_pending_ftx1_band_change(self, now: datetime) -> None:
        pending = self._pending_ftx1_band_change
        if pending is None or self.status.transmitting:
            return
        assert self.ftx1_band_drive is not None
        if pending.band.lower() not in self.ftx1_band_drive.profiles:
            self._pending_ftx1_band_change = None
            if pending.source == "forced":
                self._block_for_unready_band_drive(pending, "no saved destination drive profile")
                return
            self.ftx1_band_drive.apply_profile(pending.band, self.current_tx_audio_attenuation)
            self._finish_band_change(pending, self.status.period, now)
            return
        if self.current_tx_audio_attenuation is None:
            self._query_tx_audio_attenuation_if_needed(pending.instance_id, now)
            return
        self._pending_ftx1_band_change = None
        if pending.source == "forced":
            LOGGER.info("[DRIVE] Restoring saved drive profile for %s", pending.band)
            profile = self.ftx1_band_drive.profiles[pending.band.lower()]
            LOGGER.info("[DRIVE] Requesting TX attenuation: %d", profile.tx_audio_attenuation)
        applied = self.ftx1_band_drive.apply_profile(
            pending.band,
            self.current_tx_audio_attenuation,
            force_attenuation=pending.source == "forced",
        )
        if not applied:
            if pending.source == "forced":
                self._block_for_unready_band_drive(pending, "destination drive profile could not be applied")
                return
            self._finish_band_change(pending, self.status.period, now)
            return
        if self.pending_tx_audio_attenuation is not None:
            self._pending_ftx1_profile_apply = pending
            if pending.source == "forced":
                LOGGER.info("[DRIVE] Waiting for attenuation confirmation")
            return
        self._finish_band_change(pending, self.status.period, now)

    def _finish_pending_ftx1_profile_apply(self, now: datetime) -> None:
        pending = self._pending_ftx1_profile_apply
        if pending is None or self.pending_tx_audio_attenuation is not None:
            return
        self._pending_ftx1_profile_apply = None
        if pending.source == "forced":
            LOGGER.info("[BAND-HOP] Drive state ready")
        self._finish_band_change(pending, self.status.period, now)

    def _finish_band_change(self, pending: PendingDialFrequency, period_seconds: int | None, now: datetime) -> None:
        self.adaptive_state = AdaptiveState.BAND_TRIAL
        self.stagnation.reset()
        self.opportunities.reset(pending.band, self.status.mode or "unknown", now)
        self.last_band_change_at = now
        if pending.source == "swr_recovery":
            if self._swr_recovery is not None:
                self._swr_recovery.phase = "wait_rx"
                self._swr_recovery.current_band = pending.band
                self._antenna_event(now, f"Bande {pending.band} confirmée, nouvel accord")
            LOGGER.info("[BAND] SWR recovery confirmed=%s", pending.band)
            return
        if pending.source == "forced":
            self._start_forced_band_hop_dwell(pending.band, now)
            settle_seconds = self.config.forced_band_hop_settle_seconds
            self._band_resume_at = now + timedelta(seconds=settle_seconds)
            self._band_resume_band = pending.band
            LOGGER.info("[BAND-HOP] Confirmed on %s", pending.band)
            LOGGER.info("[BAND-HOP] Settling for %g s", settle_seconds)
        else:
            self._band_resume_at = now + timedelta(seconds=period_seconds or 15)
            self._band_resume_band = None
        LOGGER.info("[BAND] confirmed=%s", pending.band)

    def _requires_band_drive_restore(self, pending: PendingDialFrequency) -> bool:
        # A safety recovery must never wait on a WSJT-X audio profile: the
        # tuner and RF power are controlled and verified directly via Hamlib.
        if pending.source == "swr_recovery":
            return False
        return self.ftx1_band_drive is not None and self.config.ftx1_auto_apply_band_profiles

    def _block_for_unready_band_drive(self, pending: PendingDialFrequency, reason: str) -> None:
        self._blocked_band_drive_change = pending
        LOGGER.warning("[BAND-HOP] Drive state for %s is not ready: %s; automatic TX remains blocked", pending.band, reason)

    def _expire_dial_frequency_request(self, now: datetime) -> None:
        pending = self._pending_dial_frequency
        if pending is None or now <= pending.deadline:
            return
        if pending.source == "swr_recovery":
            self._pending_dial_frequency = None
            self._finish_swr_recovery_fault(now, f"confirmation de la bande {pending.band} expirée")
            return
        if pending.source == "forced":
            LOGGER.warning("[BAND-HOP] Request to %s / %.3f MHz timed out waiting for Status", pending.band, pending.frequency_hz / 1_000_000)
            self._forced_band_hop_pending_band = None
            current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            if current_band is not None:
                self._start_forced_band_hop_dwell(current_band, now)
        else:
            LOGGER.error("[BAND] requested=%d confirmation timeout", pending.frequency_hz)
        self._pending_dial_frequency = None
        self.adaptive_state = AdaptiveState.NORMAL

    def note_adaptive_failure(
        self,
        callsign: str | None,
        outcome: AttemptOutcome,
        now: datetime,
    ) -> None:
        if not self.config.adaptive_operation_enabled or not callsign:
            return
        band = BandResolver().resolve(self.status.dial_frequency) or "unknown" if self.status.dial_frequency else "unknown"
        self.stagnation.record(
            AttemptRecord(callsign.upper(), band, self.status.mode or "unknown", now, self.current_tx_first, outcome)
        )
        LOGGER.info(
            "[ADAPT] attempts=%d failed=%d unique=%d",
            len(self.stagnation.snapshot()), self.stagnation.failed_attempts, self.stagnation.unique_calls,
        )
        self.opportunities.mark_unavailable(
            callsign, now, max(self.config.stalled_qso_cooldown_seconds, self.opportunities.observation.total_seconds())
        )

    def _adaptive_success(self) -> None:
        self.stagnation.reset()
        self.adaptive_state = AdaptiveState.NORMAL
        self._adaptive_pending_origin = None
        LOGGER.info("[ADAPT] QSO success")
        LOGGER.info("[ADAPT] stagnation reset")

    def reset_adaptive_strategy(self) -> None:
        self.stagnation.reset()
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        self.opportunities.reset(band, self.status.mode, self._last_runtime_at or datetime.now(timezone.utc))
        if self._pending_dial_frequency is None:
            self.adaptive_state = AdaptiveState.NORMAL
            self._adaptive_pending_origin = None
        LOGGER.info("[ADAPT] strategy reset")

    def set_tx_audio_attenuation_confirmed(self, attenuation: int, timeout_seconds: float = 2.0) -> bool:
        if not 0 <= attenuation <= 450:
            LOGGER.info("[DRIVE] action skipped reason=OUT_OF_BOUNDS")
            return False
        if self.status.transmitting:
            LOGGER.info("[DRIVE] action skipped reason=TX_ACTIVE")
            return False
        if not self.status.instance_id:
            LOGGER.info("[DRIVE] action skipped reason=ATTENUATION_UNKNOWN")
            return False
        if self.pending_tx_audio_attenuation is not None:
            LOGGER.info("[DRIVE] action skipped reason=PENDING_CONFIRMATION")
            return False
        if not isinstance(self.control, WsjtxUdpControl) or not self.control.set_tx_audio_attenuation(self.status.instance_id, attenuation):
            LOGGER.info("[DRIVE] action skipped reason=AP1_SEND_FAILED")
            return False
        LOGGER.info("[DRIVE] applying attenuation %s -> %d", self.current_tx_audio_attenuation, attenuation)
        self.pending_tx_audio_attenuation = attenuation
        self._tx_audio_attenuation_deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        LOGGER.info("[WSJTX] Request TX attenuation=%d", attenuation)
        return True

    def _request_ftx1_profile_attenuation(self, attenuation: int) -> bool:
        """Apply a user-saved attenuation only while WSJT-X is confirmed in RX."""
        return not self.status.transmitting and self.set_tx_audio_attenuation_confirmed(attenuation)

    def _observe_tx_audio_attenuation(self, attenuation: int, now: datetime) -> None:
        previous = self.current_tx_audio_attenuation
        requested = self.pending_tx_audio_attenuation
        LOGGER.info("[WSJTX] AP1 attenuation state packet received value=%d", attenuation)
        self.current_tx_audio_attenuation = attenuation
        self.tx_audio_attenuation_last_update = now
        LOGGER.info("[WSJTX] current_tx_audio_attenuation=%d", self.current_tx_audio_attenuation)
        if requested == attenuation:
            self.pending_tx_audio_attenuation = None
            self._tx_audio_attenuation_deadline = None
            LOGGER.info("[WSJTX] TX attenuation confirmed=%d", attenuation)
            if self._pending_alc_profile_band is not None and self.ftx1_band_drive is not None:
                self.ftx1_band_drive.update_profile_attenuation(self._pending_alc_profile_band, attenuation)
                self._pending_alc_profile_band = None
        elif previous is not None and previous != attenuation:
            LOGGER.info("[WSJTX] Manual TX attenuation change %d -> %d", previous, attenuation)
        else:
            LOGGER.info("[WSJTX] TX attenuation state=%d", attenuation)
        pending_save = self._pending_ftx1_profile_save_band
        if pending_save is not None and self.ftx1_band_drive is not None:
            self._pending_ftx1_profile_save_band = None
            if self._pending_dial_frequency is not None or self._pending_ftx1_band_change is not None or self._pending_ftx1_profile_apply is not None:
                LOGGER.error("[BAND-DRIVE] Save current band profile refused: band changed while attenuation was queried")
                return
            self.ftx1_band_drive.save_current_profile(pending_save, attenuation)

    def _query_tx_audio_attenuation_if_needed(self, instance_id: str, now: datetime) -> None:
        if (
            not instance_id
            or not self.ap1_controls_available
            or self.current_tx_audio_attenuation is not None
            or not isinstance(self.control, WsjtxUdpControl)
        ):
            return
        if (
            self._attenuation_query_instance_id == instance_id
            and self._last_attenuation_query_at is not None
            and now - self._last_attenuation_query_at < _ATTENUATION_QUERY_INTERVAL
        ):
            return
        self._attenuation_query_instance_id = instance_id
        self._last_attenuation_query_at = now
        if self.control.query_tx_audio_attenuation(instance_id):
            LOGGER.info("[WSJTX] AP1 attenuation query sent")

    def invalidate_tx_audio_attenuation_state(self, reason: str) -> None:
        if self.current_tx_audio_attenuation is not None:
            LOGGER.info("[WSJTX] current_tx_audio_attenuation invalidated: %s", reason)
        self.current_tx_audio_attenuation = None
        self.tx_audio_attenuation_last_update = None
        self.pending_tx_audio_attenuation = None
        self._tx_audio_attenuation_deadline = None
        self._attenuation_query_instance_id = ""
        self._last_attenuation_query_at = None
        self._pending_alc_profile_band = None

    def _poll_hamlib_meters(self, now: datetime) -> None:
        controller = self.ftx1_band_drive
        if controller is None or not self.config.ftx1_hamlib_enabled:
            return
        if not self.status.transmitting:
            if self._hamlib_tx_active:
                self._finish_hamlib_alc_period()
            self._hamlib_tx_active = False
            self._hamlib_tx_started_at = None
            self._hamlib_next_meter_at = None
            self._hamlib_high_swr_samples = 0
            return
        if not self._hamlib_tx_active:
            self._hamlib_tx_active = True
            self._hamlib_tx_started_at = now
            self._hamlib_next_meter_at = now + timedelta(seconds=1.5)
            self._hamlib_alc_samples.clear()
            self._hamlib_power_samples.clear()
            self._hamlib_zero_power_samples = 0
            self._hamlib_high_swr_samples = 0
            read_requested_power = getattr(controller.cat, "read_requested_power_watts", None)
            self.last_hamlib_requested_power_watts = (
                read_requested_power(self.status.dial_frequency, self.status.mode or "USB")
                if callable(read_requested_power) and self.status.dial_frequency
                else None
            )
            LOGGER.info("[HAMLIB] TX meter sampling armed")
            return
        if self._hamlib_next_meter_at is not None and now < self._hamlib_next_meter_at:
            return
        self._hamlib_next_meter_at = now + timedelta(seconds=self.config.ftx1_meter_poll_seconds)
        cat = controller.cat
        read_power = getattr(cat, "read_power", None)
        read_alc = getattr(cat, "read_alc", None)
        read_swr = getattr(cat, "read_swr", None)
        power = read_power() if callable(read_power) else None
        alc = read_alc() if callable(read_alc) else None
        swr = read_swr() if callable(read_swr) else None
        self.last_hamlib_power_watts = float(power) if power is not None else None
        self.last_hamlib_alc = float(alc) if alc is not None else None
        self.last_hamlib_swr = float(swr) if swr is not None else None
        LOGGER.info("[HAMLIB] meters ALC=%s SWR=%s PWR_W=%s", alc, swr, power)
        if power is not None and float(power) < 1.0:
            self._hamlib_zero_power_samples += 1
        elif alc is not None:
            self._hamlib_alc_samples.append(float(alc))
            if power is not None:
                self._hamlib_power_samples.append(float(power))
        if self.config.ftx1_swr_protection_enabled and swr is not None and float(swr) >= self.config.ftx1_swr_limit:
            self._hamlib_high_swr_samples += 1
        else:
            self._hamlib_high_swr_samples = 0
        if self._hamlib_high_swr_samples >= self.config.ftx1_swr_trip_samples:
            self._trip_hamlib_swr(float(swr), now)

    def _finish_hamlib_alc_period(self) -> None:
        samples = tuple(self._hamlib_alc_samples)
        power_samples = tuple(self._hamlib_power_samples)
        zero_power_samples = self._hamlib_zero_power_samples
        self._hamlib_alc_samples.clear()
        self._hamlib_power_samples.clear()
        self._hamlib_zero_power_samples = 0
        if self._swr_recovery is not None:
            LOGGER.info("[HAMLIB] drive correction skipped during SWR recovery")
            return
        if not self.config.ftx1_alc_control_enabled:
            return
        current = self.current_tx_audio_attenuation
        if current is None:
            LOGGER.warning("[HAMLIB] ALC correction skipped: WSJT-X attenuation is unknown")
            return
        step = self.config.ftx1_alc_attenuation_step
        measured_power = float(statistics.median(power_samples)) if power_samples else None
        requested_power = self.last_hamlib_requested_power_watts
        power_ratio = (
            measured_power / requested_power
            if measured_power is not None and requested_power is not None and requested_power > 0
            else None
        )
        if not samples and zero_power_samples:
            recovery_step = max(step, 12)
            requested = max(0, current - recovery_step)
            direction = "increase drive (zero RF output)"
            measured = self.last_hamlib_alc
            LOGGER.warning(
                "[HAMLIB] No RF output during TX samples=%d ALC=%s attenuation=%d->%d action=%s",
                zero_power_samples,
                measured,
                current,
                requested,
                direction,
            )
        elif not samples:
            return
        else:
            measured = float(statistics.median(samples))
        if samples and measured > max(0.32, self.config.ftx1_alc_maximum):
            requested = min(450, current + step)
            direction = "reduce drive (ALC ceiling)"
        elif power_ratio is not None and power_ratio > 1.03:
            requested = min(450, current + step)
            direction = "reduce drive (PO above requested power)"
        elif power_ratio is not None and power_ratio < 0.88 and measured <= self.config.ftx1_alc_maximum:
            requested = max(0, current - step)
            direction = "increase drive (PO below requested power)"
        elif power_ratio is not None:
            LOGGER.info(
                "[HAMLIB] drive in target requested=%.1fW PO=%.1fW ratio=%.1f%% ALC=%.3f attenuation=%d",
                requested_power,
                measured_power,
                power_ratio * 100.0,
                measured,
                current,
            )
            band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            if band is not None and self.ftx1_band_drive is not None:
                self.ftx1_band_drive.update_profile_attenuation(band, current)
            return
        elif samples and measured < self.config.ftx1_alc_minimum:
            requested = max(0, current - step)
            direction = "increase drive (ALC fallback)"
        elif samples:
            LOGGER.info("[HAMLIB] ALC in target window median=%.3f attenuation=%d", measured, current)
            band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            if band is not None and self.ftx1_band_drive is not None:
                self.ftx1_band_drive.update_profile_attenuation(band, current)
            return
        if requested == current:
            LOGGER.warning("[HAMLIB] ALC correction reached attenuation limit value=%d", current)
            return
        LOGGER.info(
            "[HAMLIB] drive correction ALC=%.3f target=%.3f..%.3f requested_W=%s PO_W=%s ratio=%s attenuation=%d->%d action=%s",
            measured if measured is not None else float("nan"),
            self.config.ftx1_alc_minimum,
            self.config.ftx1_alc_maximum,
            f"{requested_power:.1f}" if requested_power is not None else "unknown",
            f"{measured_power:.1f}" if measured_power is not None else "unknown",
            f"{power_ratio * 100.0:.1f}%" if power_ratio is not None else "unknown",
            current,
            requested,
            direction,
        )
        if self.set_tx_audio_attenuation_confirmed(requested):
            band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            self._pending_alc_profile_band = band

    def _trip_hamlib_swr(self, measured_swr: float, now: datetime) -> None:
        if self._hamlib_swr_fault_latched:
            return
        self._hamlib_swr_fault_latched = True
        reason = f"Hamlib SWR {measured_swr:.2f} >= {self.config.ftx1_swr_limit:.2f}"
        halted = self._halt_tx(self.status.instance_id or None, reason)
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if not _SWR_AUTOMATIC_RECOVERY_ENABLED:
            self._swr_recovery = None
            self.antenna_safety_level = "fault"
            self._antenna_event(
                now,
                f"SWR {measured_swr:.2f} sur {band or 'bande inconnue'} : arrêt TX et désarmement "
                "(accord automatique désactivé)",
            )
            self.last_qso_notice = (
                f"SWR SAFETY {measured_swr:.2f} - AutoPilot disarmed - automatic tune disabled"
            )
            if self.engine.state.session.state is not QsoState.IDLE:
                self.engine.abort_qso("SWR safety trip", now, self.config.stalled_qso_cooldown_seconds)
            self.finalization.close("SWR safety trip")
            self._disarm_control("swr_safety_trip", "AutopilotRuntime._trip_hamlib_swr")
            LOGGER.critical(
                "[HAMLIB] SWR SAFETY TRIP swr=%.2f halted=%s auto_recovery=disabled",
                measured_swr,
                halted,
            )
            return
        self._swr_recovery = SwrRecovery(
            "wait_rx",
            band,
            band,
            rx_deadline=now + _SWR_RX_CONFIRM_TIMEOUT,
        )
        self.antenna_safety_level = "warning"
        self._antenna_event(now, f"SWR {measured_swr:.2f} sur {band or 'bande inconnue'} : arrêt TX demandé")
        self.last_qso_notice = f"SWR RECOVERY {measured_swr:.2f} - automatic tune pending"
        if self.engine.state.session.state is not QsoState.IDLE:
            self.engine.abort_qso("SWR safety recovery", now, self.config.stalled_qso_cooldown_seconds)
        self.finalization.close("SWR safety recovery")
        LOGGER.critical("[HAMLIB] SWR SAFETY TRIP swr=%.2f halted=%s recovery=started", measured_swr, halted)
        if not halted and not isinstance(self.control, DryRunControl):
            LOGGER.warning("[HAMLIB] hardware PTT stop unconfirmed; waiting for WSJT-X RX status")

    def _run_swr_recovery(self, now: datetime) -> None:
        recovery = self._swr_recovery
        controller = self.ftx1_band_drive
        if recovery is None or controller is None:
            return
        cat = controller.cat
        if recovery.phase == "changing_band":
            return
        if recovery.phase == "wait_rx":
            if self.status.transmitting:
                if recovery.rx_deadline is not None and now >= recovery.rx_deadline:
                    self._finish_swr_recovery_fault(now, "émission toujours active après 5 secondes")
                return
            recovery.rx_deadline = None
            self._antenna_event(now, "Réception confirmée, lancement de l'accord automatique")
            read_power = getattr(cat, "read_rf_power_fraction", None)
            set_tuner = getattr(cat, "set_tuner_enabled", None)
            start_tune = getattr(cat, "start_tune", None)
            if not callable(read_power) or not callable(set_tuner) or not callable(start_tune):
                self._finish_swr_recovery_fault(now, "commandes Hamlib d'accord indisponibles")
                return
            recovery.original_power_fraction = read_power()
            if recovery.original_power_fraction is None:
                self._finish_swr_recovery_fault(now, "puissance RF initiale illisible")
                return
            recovery.swr_samples = []
            recovery.seen_rf = False
            self._antenna_event(
                now,
                f"Accord automatique {recovery.current_band or 'bande inconnue'} "
                f"(puissance mémorisée {recovery.original_power_fraction * 100:.0f} W)",
            )
            if not set_tuner(True) or not start_tune():
                self._complete_swr_tune(now, success=False, detail="commande d'accord refusée")
                return
            recovery.tune_attempts += 1
            recovery.phase = "tuning"
            recovery.tune_started_at = now
            recovery.next_poll_at = now + timedelta(seconds=0.2)
            LOGGER.warning("[HAMLIB] SWR recovery tune started band=%s", recovery.current_band)
            return
        if recovery.phase != "tuning" or recovery.tune_started_at is None:
            return
        if recovery.next_poll_at is not None and now < recovery.next_poll_at:
            return
        recovery.next_poll_at = now + _SWR_TUNE_POLL_INTERVAL
        read_power_meter = getattr(cat, "read_power", None)
        read_swr = getattr(cat, "read_swr", None)
        power = read_power_meter() if callable(read_power_meter) else None
        swr = read_swr() if callable(read_swr) else None
        self.last_hamlib_power_watts = float(power) if power is not None else None
        self.last_hamlib_swr = float(swr) if swr is not None else None
        if power is not None and float(power) >= 1.0:
            recovery.seen_rf = True
            if swr is not None:
                recovery.swr_samples.append(float(swr))
            LOGGER.info("[HAMLIB] tune meters band=%s SWR=%s PWR_W=%s", recovery.current_band, swr, power)
        elif recovery.seen_rf:
            self._evaluate_swr_tune(now)
            return
        if not recovery.seen_rf and now - recovery.tune_started_at >= _SWR_TUNE_RF_START_TIMEOUT:
            self._finish_swr_recovery_fault(
                now,
                "accord automatique non démarré (aucune puissance RF)",
            )
            return
        if now - recovery.tune_started_at >= _SWR_TUNE_TIMEOUT:
            self._complete_swr_tune(now, success=False, detail="délai d'accord dépassé")

    def _evaluate_swr_tune(self, now: datetime) -> None:
        recovery = self._swr_recovery
        if recovery is None:
            return
        samples = recovery.swr_samples or []
        # An automatic tuner normally sweeps through a high SWR before it
        # reaches its match.  Judging the median of the whole tail therefore
        # rejects a good match when the meter only catches one final low
        # reading.  Only RF-valid samples are stored, so the best reading in
        # the final window is the useful end-of-tune result.
        terminal_samples = samples[-3:]
        measured = min(terminal_samples) if terminal_samples else None
        if measured is not None and measured < self.config.ftx1_swr_limit:
            self._complete_swr_tune(now, success=True, detail=f"SWR final {measured:.2f}")
            return

        # Some external tuners drop their carrier before Hamlib refreshes the
        # final meter value.  A second, short tune on the now matched network
        # provides a real RF verification instead of excluding the band from
        # measurements taken during the first sweep.
        if recovery.tune_attempts < 2:
            controller = self.ftx1_band_drive
            start_tune = getattr(controller.cat, "start_tune", None) if controller is not None else None
            if callable(start_tune) and start_tune():
                recovery.tune_attempts += 1
                recovery.swr_samples = []
                recovery.seen_rf = False
                recovery.tune_started_at = now
                recovery.next_poll_at = now + timedelta(seconds=0.2)
                self._antenna_event(
                    now,
                    f"Contrôle final de l'accord sur {recovery.current_band or 'bande inconnue'}",
                )
                LOGGER.warning(
                    "[HAMLIB] SWR recovery verification tune started band=%s attempt=%d",
                    recovery.current_band,
                    recovery.tune_attempts,
                )
                return

        detail = "mesures SWR insuffisantes" if len(samples) < _SWR_TUNE_MIN_SAMPLES else f"SWR final {measured:.2f}"
        self._complete_swr_tune(
            now,
            success=False,
            detail=detail,
        )

    def _complete_swr_tune(self, now: datetime, *, success: bool, detail: str) -> None:
        recovery = self._swr_recovery
        controller = self.ftx1_band_drive
        if recovery is None or controller is None:
            return
        cat = controller.cat
        restored = True
        if recovery.original_power_fraction is not None:
            set_power = getattr(cat, "set_rf_power_fraction", None)
            restored = bool(callable(set_power) and set_power(recovery.original_power_fraction))
            self._antenna_event(
                now,
                f"Puissance restaurée à {recovery.original_power_fraction * 100:.0f} W"
                if restored else "Échec de restauration de la puissance RF",
            )
        if not restored:
            self._finish_swr_recovery_fault(now, "puissance RF non restaurée")
            return
        if success:
            band = recovery.current_band
            self._hamlib_swr_fault_latched = False
            self._hamlib_high_swr_samples = 0
            self.antenna_safety_level = "warning"
            self._antenna_event(now, f"Accord réussi sur {band or 'bande inconnue'} : {detail}")
            self.last_qso_notice = f"SWR RECOVERED {band or ''} - {detail}"
            self._swr_recovery = None
            self._band_resume_at = now + timedelta(seconds=self.status.period or 15)
            self._band_resume_band = band
            return
        self._fail_current_swr_band(now, detail)

    def _fail_current_swr_band(self, now: datetime, detail: str) -> None:
        recovery = self._swr_recovery
        if recovery is None:
            return
        controller = self.ftx1_band_drive
        set_tuner = getattr(controller.cat, "set_tuner_enabled", None) if controller is not None else None
        if not callable(set_tuner) or not set_tuner(False):
            self._finish_swr_recovery_fault(now, "accord automatique impossible à arrêter")
            return
        band = (recovery.current_band or "").lower()
        if band:
            self._swr_excluded_bands.add(band)
            self._antenna_event(now, f"Bande {band} exclue pour la session : {detail}")
        target = self._next_swr_recovery_band()
        if target is None:
            self._finish_swr_recovery_fault(now, "aucune bande autorisée utilisable")
            return
        frequency = self._band_frequency(target, self.status.mode or "FT8")
        if frequency is None or not self._request_band_change(target, frequency, now, "swr_recovery"):
            self._finish_swr_recovery_fault(now, f"changement vers {target} impossible")
            return
        recovery.phase = "changing_band"
        recovery.current_band = target
        recovery.original_power_fraction = None
        recovery.tune_started_at = None
        recovery.next_poll_at = None
        recovery.seen_rf = False
        recovery.swr_samples = []
        recovery.tune_attempts = 0
        self._antenna_event(now, f"Essai de la bande {target}")

    def _next_swr_recovery_band(self) -> str | None:
        allowed = tuple(dict.fromkeys(band.lower() for band in self.config.allowed_auto_hop_bands))
        for band in allowed:
            if band not in self._swr_excluded_bands and self._band_frequency(band, self.status.mode or "FT8") is not None:
                return band
        return None

    def _finish_swr_recovery_fault(self, now: datetime, detail: str) -> None:
        recovery = self._swr_recovery
        if recovery is not None and self.ftx1_band_drive is not None:
            set_tuner = getattr(self.ftx1_band_drive.cat, "set_tuner_enabled", None)
            if callable(set_tuner):
                set_tuner(False)
        self.antenna_safety_level = "fault"
        self._antenna_event(now, f"ARRÊT DÉFINITIF : {detail}")
        self._swr_recovery = None
        self._disarm_control("swr_safety_unrecoverable", "AutopilotRuntime._finish_swr_recovery_fault")
        self.last_qso_notice = f"SWR SAFETY FINAL - {detail} - AutoPilot disarmed"
        LOGGER.critical("[HAMLIB] SWR recovery failed: %s", detail)

    def _antenna_event(self, now: datetime, message: str) -> None:
        entry = f"{now.astimezone().strftime('%H:%M:%S')}  {message}"
        self.antenna_safety_events.append(entry)
        del self.antenna_safety_events[:-12]
        LOGGER.warning("[ANTENNA-SAFETY] %s", message)

    def _expire_tx_audio_attenuation_request(self, now: datetime) -> None:
        if self.pending_tx_audio_attenuation is not None and self._tx_audio_attenuation_deadline is not None and now > self._tx_audio_attenuation_deadline:
            requested = self.pending_tx_audio_attenuation
            pending_band_change = self._pending_ftx1_profile_apply
            LOGGER.error("[WSJTX] TX attenuation confirmation timeout requested=%d current=%s", requested, self.current_tx_audio_attenuation)
            self.pending_tx_audio_attenuation = None
            self._tx_audio_attenuation_deadline = None
            self._pending_alc_profile_band = None
            if pending_band_change is not None and pending_band_change.source == "forced":
                self._pending_ftx1_profile_apply = None
                self._block_for_unready_band_drive(pending_band_change, f"TX attenuation {requested} was not confirmed")
                return
            self._finish_pending_ftx1_profile_apply(now)

    def save_ftx1_current_band_profile(self) -> bool:
        controller = self.ftx1_band_drive
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if controller is None or band is None:
            LOGGER.error("[FTX1] Save manual band profile failed: CAT-2 or current band unavailable")
            return False
        if self._pending_dial_frequency is not None or self._pending_ftx1_band_change is not None or self._pending_ftx1_profile_apply is not None:
            LOGGER.error("[BAND-DRIVE] Save current band profile refused: band change is pending")
            return False
        if self.status.transmitting:
            LOGGER.error("[FTX1] Save manual band profile failed: WSJT-X is transmitting")
            return False
        if self.pending_tx_audio_attenuation is not None:
            LOGGER.error("[BAND-DRIVE] Save current band profile refused: TX attenuation is awaiting confirmation")
            return False
        if self.current_tx_audio_attenuation is None:
            if not self.ap1_controls_available or not isinstance(self.control, WsjtxUdpControl):
                LOGGER.error("[FTX1] Save manual band profile failed: WSJT-X attenuation state unavailable")
                return False
            self._pending_ftx1_profile_save_band = band
            if self.control.query_tx_audio_attenuation(self.status.instance_id):
                LOGGER.info("[FTX1] Save manual band profile awaiting AP1 attenuation confirmation")
                return True
            self._pending_ftx1_profile_save_band = None
            LOGGER.error("[FTX1] Save manual band profile failed: attenuation query could not be sent")
            return False
        return controller.save_current_profile(band, self.current_tx_audio_attenuation)

    def delete_ftx1_current_band_profile(self) -> bool:
        band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        return bool(self.ftx1_band_drive is not None and band is not None and self.ftx1_band_drive.delete_profile(band))

    def reset_all_ftx1_band_profiles(self) -> None:
        if self.ftx1_band_drive is not None:
            self.ftx1_band_drive.reset_profiles()

    def _disarm_control(self, reason: str, source: str) -> None:
        if isinstance(self.control, WsjtxUdpControl):
            self.control.disarm(reason, source=source)
        else:
            self.control.disarm(reason)

    def adaptive_snapshot(self) -> dict[str, object]:
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        observed_at = self._last_runtime_at or datetime.now(timezone.utc)
        opportunities = self.opportunities.snapshot(observed_at)
        quiet_threshold = max(1.0, float(self.config.adaptive_opportunity_quiet_seconds))
        quiet_anchor = self.opportunities.last_viable_at or self.opportunities.started_at
        quiet_elapsed = max(0.0, (observed_at - quiet_anchor).total_seconds()) if quiet_anchor is not None else 0.0
        opportunities.update(
            {
                "viable_count": len(opportunities["available"]),
                "quiet_elapsed_seconds": min(quiet_threshold, quiet_elapsed),
                "quiet_threshold_seconds": quiet_threshold,
                "quiet_remaining_seconds": max(0.0, quiet_threshold - quiet_elapsed),
                "exhausted": self.opportunities.exhausted(observed_at),
            }
        )
        snapshot = {
            "ap1_available": self.ap1_controls_available,
            "state": self.adaptive_state.name,
            "current_tx_first": self.current_tx_first,
            "requested_tx_period": self.requested_tx_period,
            "current_band": current_band,
            "next_band": self._next_auto_hop_band(),
            "band_changing_to": self._pending_dial_frequency.band if self._pending_dial_frequency else None,
            "opportunities": opportunities,
            "antenna_safety": {
                "level": self.antenna_safety_level,
                "events": tuple(self.antenna_safety_events),
                "excluded_bands": tuple(sorted(self._swr_excluded_bands)),
                "recovering": self._swr_recovery is not None,
                "phase": self._swr_recovery.phase if self._swr_recovery is not None else None,
            },
        }
        if self.ftx1_band_drive is not None:
            ftx1 = self.ftx1_band_drive.snapshot(current_band, self.current_tx_audio_attenuation)
            ftx1.update(
                {
                    "hamlib_enabled": self.config.ftx1_hamlib_enabled,
                    "radio_model": getattr(
                        self.ftx1_band_drive.cat,
                        "detected_radio_model",
                        "Yaesu FTX-1" if not self.config.ftx1_hamlib_enabled else "Hamlib generic",
                    ),
                    "alc": self.last_hamlib_alc,
                    "swr": self.last_hamlib_swr,
                    "power_watts": self.last_hamlib_power_watts,
                    "requested_power_watts": self.last_hamlib_requested_power_watts,
                    "swr_fault": self._hamlib_swr_fault_latched,
                }
            )
            snapshot["ftx1"] = ftx1
        return snapshot

    def _forced_band_hop_bands(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                band.strip().lower()
                for band in self.config.forced_band_hop_bands
                if isinstance(band, str)
                and band.strip()
                and band.strip().lower() not in self._swr_excluded_bands
                and self._band_frequency(band.strip(), "FT8") is not None
            )
        )

    def _observe_forced_band_hop_status(self, now: datetime) -> None:
        if not self.config.forced_band_hopping_enabled or self._pending_dial_frequency is not None:
            return
        bands = self._forced_band_hop_bands()
        if not bands:
            self._warn_forced_band_hop_configuration()
            return
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if current_band in bands and self._forced_band_hop_dwell_band != current_band:
            self._forced_band_hop_pending_band = None
            self._start_forced_band_hop_dwell(current_band, now)
        elif current_band not in bands and self._forced_band_hop_pending_band is None:
            self._forced_band_hop_pending_band = bands[0]
            LOGGER.info("[BAND-HOP] Current band is outside the configured sequence; pending %s", bands[0])

    def _run_forced_band_hop(self, now: datetime) -> None:
        if self._swr_recovery is not None or not self.config.forced_band_hopping_enabled:
            return
        bands = self._forced_band_hop_bands()
        if not bands:
            self._warn_forced_band_hop_configuration()
            return
        if self._forced_band_hop_pending_band is None:
            started_at = self._forced_band_hop_dwell_started_at
            current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            if started_at is None or current_band not in bands:
                return
            if now - started_at < timedelta(minutes=self.config.forced_band_hop_interval_minutes):
                return
            target = self._next_forced_band_hop_band()
            if target is None:
                LOGGER.warning("[BAND-HOP] No alternate configured band is available; restarting dwell")
                self._start_forced_band_hop_dwell(current_band, now)
                return
            self._forced_band_hop_pending_band = target
            LOGGER.info("[BAND-HOP] Dwell expired on %s", current_band)
            if self.engine.state.session.state is not QsoState.IDLE or self.finalization.active:
                LOGGER.info("[BAND-HOP] QSO active, deferring hop to %s", target)
        target = self._forced_band_hop_pending_band
        if target is None or not self._forced_band_hop_safe():
            return
        frequency = self._band_frequency(target, self.status.mode or "FT8")
        if frequency is None:
            LOGGER.warning("[BAND-HOP] No frequency is configured for %s in %s; remaining on the current band", target, self.status.mode or "unknown")
            self._forced_band_hop_pending_band = None
            return
        if self._request_band_change(target, frequency, now, "forced"):
            self._forced_band_hop_pending_band = None

    def _start_forced_band_hop_dwell(self, band: str, now: datetime) -> None:
        self._forced_band_hop_dwell_band = band
        self._forced_band_hop_dwell_started_at = now
        LOGGER.info(
            "[BAND-HOP] Started dwell on %s for %g min",
            band,
            self.config.forced_band_hop_interval_minutes,
        )

    def _next_forced_band_hop_band(self) -> str | None:
        bands = self._forced_band_hop_bands()
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if current_band not in bands or len(bands) < 2:
            return None
        start = bands.index(current_band)
        for offset in range(1, len(bands)):
            candidate = bands[(start + offset) % len(bands)]
            if self._band_frequency(candidate, self.status.mode or "FT8") is not None:
                return candidate
        return None

    def _forced_band_hop_safe(self) -> bool:
        return bool(
            getattr(self.control, "armed", False)
            and self.status.instance_id
            and not self.status.transmitting
            and self.engine.state.session.state is QsoState.IDLE
            and not self.finalization.active
            and self._pending_watchdog_halt is None
            and self._pending_dial_frequency is None
            and self._pending_ftx1_band_change is None
            and self._pending_ftx1_profile_apply is None
            and not self.engine.pending_direct_snapshot(self._last_runtime_at or datetime.now(timezone.utc))
        )

    def _warn_forced_band_hop_configuration(self) -> None:
        if not self._forced_band_hop_config_warning_logged:
            LOGGER.warning("[BAND-HOP] Forced hopping is enabled but no configured bands are available")
            self._forced_band_hop_config_warning_logged = True

    def _request_band_change(self, target: str, frequency: int, now: datetime, source: str) -> bool:
        self.engine.clear_candidates()
        self.engine.invalidate_pending_context(self.status.instance_id, target, self.status.mode or "unknown")
        cat = self.ftx1_band_drive.cat if self.ftx1_band_drive is not None else None
        set_frequency = getattr(cat, "set_frequency", None)
        read_frequency = getattr(cat, "read_frequency", None)
        if callable(set_frequency) and callable(read_frequency):
            if not set_frequency(frequency):
                LOGGER.error("[HAMLIB] frequency change rejected target=%d", frequency)
                return False
            observed = read_frequency()
            if observed is None or abs(observed - frequency) > 5:
                LOGGER.error("[HAMLIB] frequency confirmation failed requested=%d observed=%s", frequency, observed)
                return False
            LOGGER.info("[HAMLIB] frequency confirmed=%d", observed)
        elif not self.control.set_dial_frequency(self.status.instance_id, frequency):
            # Compatibility path for dry-run/tests and installations where
            # Hamlib radio control is explicitly unavailable.
            return False
        self._pending_dial_frequency = PendingDialFrequency(
            self.status.instance_id,
            frequency,
            target,
            now + timedelta(seconds=self.config.dial_change_confirmation_timeout_seconds),
            BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None,
            source,
        )
        self.adaptive_state = AdaptiveState.BAND_CHANGING
        if source == "forced":
            LOGGER.info("[BAND-HOP] Requesting %s / %.3f MHz", target, frequency / 1_000_000)
        else:
            LOGGER.info("[BAND] current=%s", BandResolver().resolve(self.status.dial_frequency) or "unknown")
            LOGGER.info("[BAND] next=%s", target)
            LOGGER.info("[BAND] waiting confirmation")
        return True

    def _run_adaptive(self, now: datetime) -> None:
        if self._swr_recovery is not None:
            return
        if self.adaptive_state in {AdaptiveState.NORMAL, AdaptiveState.PARITY_TRIAL, AdaptiveState.BAND_TRIAL} and self._adaptive_safe(now):
            exhausted = self.opportunities.exhausted(now)
            quiet = self.opportunities.quiet(now, self.config.adaptive_opportunity_quiet_seconds)
            if exhausted or quiet:
                previous = self.adaptive_state
                self._adaptive_pending_origin = previous
                self.adaptive_state = (
                    AdaptiveState.PARITY_CHANGE_PENDING
                    if previous is AdaptiveState.NORMAL and self.config.adaptive_parity_enabled and self.ap1_controls_available
                    else AdaptiveState.BAND_HOP_PENDING
                )
                LOGGER.info(
                    "[ADAPT] opportunities=%s reason=%s state=%s -> %s",
                    self.opportunities.snapshot(now),
                    "exhausted" if exhausted else "no new viable CQ",
                    previous.name,
                    self.adaptive_state.name,
                )
        if self.adaptive_state is AdaptiveState.PARITY_CHANGE_PENDING:
            if not self.config.adaptive_parity_enabled or not self._adaptive_safe(now):
                return
            if self.current_tx_first is None:
                return
            requested = not self.current_tx_first
            if self.control.set_tx_period(self.status.instance_id, requested):
                self.requested_tx_period = requested
                self.adaptive_state = AdaptiveState.PARITY_TRIAL
                self._adaptive_pending_origin = None
                self.stagnation.reset()
                band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
                self.opportunities.reset(band, self.status.mode or "unknown", now)
                LOGGER.info("[ADAPT] action=TX_PERIOD_FLIP")
                LOGGER.info("[ADAPT] %s -> %s", "FIRST" if self.current_tx_first else "SECOND", "FIRST" if requested else "SECOND")
            return
        if self.adaptive_state is AdaptiveState.BAND_HOP_PENDING:
            if not self.config.automatic_band_hopping_enabled or not self._adaptive_safe(now):
                return
            if self.last_band_change_at is not None:
                dwell = now - self.last_band_change_at
                if dwell < timedelta(minutes=self.config.minimum_band_dwell_minutes):
                    return
            target = self._next_auto_hop_band()
            if target is None:
                return
            frequency = self._band_frequency(target, self.status.mode or "")
            if frequency is None:
                return
            self._request_band_change(target, frequency, now, "adaptive")

    def _adaptive_safe(self, now: datetime) -> bool:
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        has_pending_direct = any(
            entry["band"] == current_band for entry in self.engine.pending_direct_snapshot(now)
        )
        return bool(
            self.config.adaptive_operation_enabled
            and getattr(self.control, "armed", False)
            and self.status.instance_id
            and not self.status.transmitting
            and self.engine.state.session.state is QsoState.IDLE
            and not self.finalization.active
            and self._pending_watchdog_halt is None
            and self._pending_dial_frequency is None
            and self.pending_tx_audio_attenuation is None
            and not has_pending_direct
        )

    def _next_auto_hop_band(self) -> str | None:
        allowed = tuple(
            band for band in dict.fromkeys(band.lower() for band in self.config.allowed_auto_hop_bands)
            if band not in self._swr_excluded_bands
        )
        if not allowed or self.status.dial_frequency is None:
            return None
        current = (BandResolver().resolve(self.status.dial_frequency) or "").lower()
        if current not in allowed:
            return next((band for band in allowed if self._band_frequency(band, self.status.mode or "") is not None), None)
        start = allowed.index(current)
        for offset in range(1, len(allowed)):
            candidate = allowed[(start + offset) % len(allowed)]
            if self._band_frequency(candidate, self.status.mode or "") is not None:
                return candidate
        return None

    def _band_frequency(self, band: str, mode: str) -> int | None:
        override = self.config.auto_hop_band_frequencies.get(band.lower(), {}).get(mode.upper())
        if override is not None:
            return override
        profile = next((item for item in DEFAULT_BAND_PROFILES if item.band.lower() == band.lower()), None)
        return profile.frequency_for(mode) if profile is not None else None

    def clear_finalization(self, reason: str) -> None:
        self.finalization.close(reason)
        self.last_qso_notice = ""

    def _automation_mode(self, decode_mode: str) -> str | None:
        if decode_mode in _AUTOMATION_MODES:
            return decode_mode
        marker_mode = _DECODE_MODE_MARKERS.get(decode_mode)
        if marker_mode is not None:
            return marker_mode
        if self.status.mode in _AUTOMATION_MODES:
            return self.status.mode
        return None

    def _status_confirms_reply(self, packet: StatusPacket) -> bool:
        session = self.engine.state.session
        if session.state not in {QsoState.DIRECT_REPLY_SENT, QsoState.CALLING_STATION}:
            return False
        if session.remote_callsign is None or session.reply_confirmed:
            return False
        remote = session.remote_callsign.upper()
        dx_matches = packet.dx_call.upper() == remote
        tx_message_matches = remote in packet.tx_message.upper().split()
        return dx_matches and (packet.transmitting or (packet.tx_enabled and tx_message_matches))

    def _maintain_qso_lifecycle(self, now: datetime) -> None:
        session = self.engine.state.session
        if session.state is QsoState.IDLE or session.last_activity is None:
            return

        remote = (session.remote_callsign or "").upper()
        tx_message_matches = remote and remote in self.status.tx_message.upper().split()
        status_engaged = self.status.dx_call.upper() == remote and tx_message_matches
        inactive = not self.status.transmitting and (not self.status.tx_enabled or not status_engaged)

        if session.state is QsoState.COMPLETE:
            completion_age = (now - session.last_activity).total_seconds()
            if inactive and completion_age >= self.config.qso_completion_grace_seconds:
                if self.engine.finalize_observed_completion("terminal Decode and inactive WSJT-X Status"):
                    self._log_ready_for_next_qso()
            return

        if session.state is QsoState.WAITING_FINAL_TX:
            return

        if session.state is QsoState.DIRECT_REPLY_SENT:
            return
        if session.no_progress_periods >= self.config.max_no_progress_periods:
            if self._watchdog_halt_failed:
                return
            stage = session.progress_stage.name if session.progress_stage is not None else "UNKNOWN"
            LOGGER.error(
                "[WATCHDOG] QSO stalled remote=%s stage=%s no_progress=%d/%d",
                remote,
                stage,
                session.no_progress_periods,
                self.config.max_no_progress_periods,
            )
            halted = self._halt_tx(session.instance_id, "QSO stalled")
            if not halted and not isinstance(self.control, DryRunControl):
                self._disarm_control("halt_tx_failed_stalled_qso", "AutopilotRuntime._maintain_qso_lifecycle")
                self._watchdog_halt_failed = True
                self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({remote})"
                LOGGER.critical(
                    "[WATCHDOG] HaltTx failed; retaining active QSO and blocking new initiations remote=%s",
                    remote,
                )
                return
            self.last_qso_notice = f"QSO abandoned - no progress ({remote}, {stage})"
            if self.engine.abort_qso(
                "QSO stalled",
                now,
                self.config.stalled_qso_cooldown_seconds,
            ):
                self.note_adaptive_failure(remote, AttemptOutcome.STALLED, now)
                self._log_ready_for_next_qso()
            return
        inactivity = (now - session.last_activity).total_seconds()
        if inactive and inactivity > self.config.qso_timeout_seconds:
            outcome = (
                AttemptOutcome.NO_RESPONSE
                if session.state is QsoState.CALLING_STATION
                else AttemptOutcome.STALLED
            )
            if self.engine.abort_qso(
                "QSO inactivity timeout while WSJT-X is not engaged",
                now,
                self.config.stalled_qso_cooldown_seconds,
            ):
                self.note_adaptive_failure(remote, outcome, now)
                self._log_ready_for_next_qso()

    def _handle_wsjtx_tx_watchdog(self, packet: StatusPacket, now: datetime) -> None:
        """Release an active QSO as soon as WSJT-X reports its TX watchdog."""
        if not packet.tx_watchdog:
            return
        if self._pending_watchdog_halt is not None:
            return
        session = self.engine.state.session
        if session.state is QsoState.IDLE:
            return
        remote = (session.remote_callsign or "").upper()
        stage = session.progress_stage.name if session.progress_stage is not None else "UNKNOWN"
        LOGGER.warning(
            "[WATCHDOG] WSJT-X Runaway TX watchdog remote=%s stage=%s; abandoning immediately",
            remote or "unknown",
            stage,
        )
        if packet.transmitting or packet.tx_enabled:
            halt_result = self._request_halt_tx(packet.header.instance_id, "WSJT-X Runaway TX watchdog")
            halted = halt_result.radio_ok and halt_result.wsjtx_ok
            if not halted and halt_result.wsjtx_ok and not packet.transmitting:
                halted = True
                LOGGER.warning(
                    "[WATCHDOG] Hamlib PTT OFF confirmation unavailable, but WSJT-X is already in RX"
                )
            elif not halted and halt_result.wsjtx_ok and not isinstance(self.control, DryRunControl):
                self._pending_watchdog_halt = PendingWatchdogHalt(
                    packet.header.instance_id,
                    remote,
                    now + _WATCHDOG_HALT_CONFIRM_TIMEOUT,
                )
                LOGGER.warning(
                    "[WATCHDOG] HaltTx sent; waiting %.1f s for WSJT-X RX confirmation remote=%s",
                    _WATCHDOG_HALT_CONFIRM_TIMEOUT.total_seconds(),
                    remote or "unknown",
                )
            elif not halted and not isinstance(self.control, DryRunControl):
                self._disarm_control("halt_tx_failed_wsjtx_watchdog", "AutopilotRuntime._handle_wsjtx_tx_watchdog")
                self._watchdog_halt_failed = True
                self.last_qso_notice = f"SAFETY FAULT - HaltTx failed ({remote})"
                LOGGER.critical(
                    "[WATCHDOG] HaltTx failed after WSJT-X watchdog; retaining active QSO remote=%s",
                    remote or "unknown",
                )
                return
        state = session.state
        if self.engine.abort_qso(
            "WSJT-X Runaway TX watchdog",
            now,
            self.config.stalled_qso_cooldown_seconds,
            preserve_candidates=True,
        ):
            outcome = AttemptOutcome.NO_RESPONSE if state is QsoState.CALLING_STATION else AttemptOutcome.STALLED
            self.note_adaptive_failure(remote, outcome, now)
            self.last_qso_notice = f"QSO abandoned - WSJT-X TX watchdog ({remote})"
            self._log_ready_for_next_qso()

    def _poll_watchdog_halt_confirmation(self, now: datetime) -> None:
        pending = self._pending_watchdog_halt
        if pending is None:
            return
        if not self.status.transmitting:
            self._pending_watchdog_halt = None
            self._watchdog_halt_failed = False
            LOGGER.warning(
                "[WATCHDOG] WSJT-X RX confirmed after HaltTx; AutoPilot remains armed remote=%s",
                pending.remote or "unknown",
            )
            return
        if now < pending.deadline:
            return
        self._pending_watchdog_halt = None
        self._disarm_control("halt_tx_unconfirmed_wsjtx_watchdog", "AutopilotRuntime._poll_watchdog_halt_confirmation")
        self._watchdog_halt_failed = True
        self.last_qso_notice = f"SAFETY FAULT - TX still active after HaltTx ({pending.remote})"
        LOGGER.critical(
            "[WATCHDOG] WSJT-X still transmitting after HaltTx confirmation timeout; AutoPilot disarmed remote=%s",
            pending.remote or "unknown",
        )

    def _log_ready_for_next_qso(self) -> None:
        maximum = self.control.max_actions
        if maximum is not None and self.control.actions_used < maximum:
            LOGGER.info(
                "[ENGINE] ready for next QSO actions=%d/%d",
                self.control.actions_used,
                maximum,
            )

    def _update_diagnostic_context(self, now: datetime) -> None:
        session = current_logging_session()
        if session is not None:
            session.context.pending_direct_calls = self.engine.pending_direct_snapshot(now)
            session.context.adaptive = {
                "enabled": self.config.adaptive_operation_enabled,
                "state": self.adaptive_state.name,
                "recent_attempts": self.stagnation.snapshot(),
                "failed_attempts": self.stagnation.failed_attempts,
                "unique_calls": self.stagnation.unique_calls,
                "current_tx_period": self.current_tx_first,
                "requested_tx_period": self.requested_tx_period,
                "current_band": BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None,
                "allowed_bands": list(self.config.allowed_auto_hop_bands),
                "last_band_change": self.last_band_change_at.isoformat() if self.last_band_change_at else None,
                "minimum_band_dwell_minutes": self.config.minimum_band_dwell_minutes,
                "next_band": self._next_auto_hop_band(),
                "opportunities": self.opportunities.snapshot(now),
            }
