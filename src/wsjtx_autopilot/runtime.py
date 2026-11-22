import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.base import ControlAdapter, Endpoint
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.adaptive import AdaptiveState, AttemptOutcome, BandOpportunityTracker, StagnationTracker
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, FallbackDxccResolver, PrefixDxccResolver
from wsjtx_autopilot.engine.finalization import FinalizationTracker
from wsjtx_autopilot.engine.location import StationLocatorCache, distance_and_bearing, grid_to_location
from wsjtx_autopilot.engine.models import (
    Candidate,
    CooldownKind,
    DecodeEvent,
    IntendedAction,
    MessageKind,
    OriginalDecode,
    StationMetadata,
    StationProfile,
)
from wsjtx_autopilot.engine.parser import parse_ft8_message
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.ftx1 import FTX1BandDriveController, FTX1CatController
from wsjtx_autopilot.hamlib import HamlibFtx1Controller
from wsjtx_autopilot.logging_setup import current_logging_session
from wsjtx_autopilot.runtime_support.adaptive import AdaptiveMixin
from wsjtx_autopilot.runtime_support.attenuation import AttenuationMixin
from wsjtx_autopilot.runtime_support.atu100 import Atu100Mixin
from wsjtx_autopilot.runtime_support.band_hop import BandHopMixin
from wsjtx_autopilot.runtime_support.band_transitions import BandTransitionMixin
from wsjtx_autopilot.runtime_support.constants import _tr_period_seconds
from wsjtx_autopilot.runtime_support.decision import DecisionMixin
from wsjtx_autopilot.runtime_support.frequencies import FrequencyPlanMixin
from wsjtx_autopilot.runtime_support.hamlib_meters import HamlibMetersMixin
from wsjtx_autopilot.runtime_support.hunt import HuntMixin
from wsjtx_autopilot.runtime_support.models import (
    HuntRemoteSpot,
    HuntTarget,
    PendingAtu100Tune,
    PendingDialFrequency,
    PendingWatchdogHalt,
)
from wsjtx_autopilot.runtime_support.swr import SwrRecovery, SwrRecoveryMixin
from wsjtx_autopilot.runtime_support.watchdogs import _WATCHDOG_HALT_CONFIRM_TIMEOUT, WatchdogMixin
from wsjtx_autopilot.worked.bands import BandResolver
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    QsoLoggedPacket,
    StatusPacket,
    TuneStatePacket,
    TxAudioAttenuationStatePacket,
    WsjtxPacket,
)

LOGGER = logging.getLogger(__name__)
_SWR_TUNE_TIMEOUT = timedelta(seconds=15)
_SWR_TUNE_RF_START_TIMEOUT = timedelta(seconds=3)
_SWR_TUNE_POLL_INTERVAL = timedelta(seconds=0.5)
_SWR_TUNE_MIN_ACTIVE_TIME = timedelta(seconds=2)
_SWR_TUNE_MIN_SAMPLES = 2
_SWR_RX_CONFIRM_TIMEOUT = timedelta(seconds=5)
_SWR_AUTOMATIC_RECOVERY_ENABLED = True
_MINIMUM_DXCC_MQTT_PATH_PROBABILITY = 55




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
class SessionDiagnostics:
    """Small, session-only operational ledger for the Reply screen."""

    candidates_selected: int = 0
    replies_sent: int = 0
    qso_completed: int = 0
    candidates_refused: int = 0
    stations_busy: int = 0
    local_rejections: int = 0
    control_failures: int = 0
    autopilot_watchdogs: int = 0
    wsjtx_watchdogs: int = 0
    recent: list[str] | None = None

    def __post_init__(self) -> None:
        if self.recent is None:
            self.recent = []

    def note(self, message: str) -> None:
        assert self.recent is not None
        self.recent.append(message)
        del self.recent[:-5]

    def snapshot(self) -> dict[str, object]:
        reply_success_percent = round(100 * self.qso_completed / self.replies_sent) if self.replies_sent else None
        return {
            "candidates_selected": self.candidates_selected,
            "replies_sent": self.replies_sent,
            "qso_completed": self.qso_completed,
            "candidates_refused": self.candidates_refused,
            "stations_busy": self.stations_busy,
            "local_rejections": self.local_rejections,
            "control_failures": self.control_failures,
            "autopilot_watchdogs": self.autopilot_watchdogs,
            "wsjtx_watchdogs": self.wsjtx_watchdogs,
            "reply_success_percent": reply_success_percent,
            "recent": tuple(reversed(self.recent or [])),
        }


@dataclass(frozen=True, slots=True)
class HaltTxResult:
    radio_ok: bool
    wsjtx_ok: bool
    observed_ptt: bool | None = None






class AutopilotRuntime(
    FrequencyPlanMixin,
    HuntMixin,
    Atu100Mixin,
    HamlibMetersMixin,
    BandHopMixin,
    WatchdogMixin,
    AttenuationMixin,
    SwrRecoveryMixin,
    DecisionMixin,
    BandTransitionMixin,
    AdaptiveMixin,
):
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
        self.last_hamlib_swr_protection = "uncertain"
        self.last_hamlib_power_watts: float | None = None
        self.last_hamlib_requested_power_watts: float | None = None
        self.last_qso_notice = ""
        self.session_diagnostics = SessionDiagnostics()
        self.engine.add_event_listener(self.note_engine_event)
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
        self._pending_atu100_tune: PendingAtu100Tune | None = None
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
        self._hunt_active = False
        self._hunt_manual_target = ""
        self._hunt_targets: tuple[HuntTarget, ...] = ()
        self._reply_rare_targets: tuple[HuntTarget, ...] = ()
        self._hunt_band_started_at: datetime | None = None
        self._hunt_observed_band: str | None = None
        self._hunt_rotation_index = 0
        self._remote_hunt_enabled = config.pskreporter_mqtt_enabled
        self._hunt_remote_spots: dict[str, HuntRemoteSpot] = {}
        self._unconfirmed_dxcc_follow_enabled = False
        self._unconfirmed_dxcc_remote_spots: dict[str, HuntRemoteSpot] = {}
        self._unconfirmed_dxcc_target = ""
        self._unconfirmed_dxcc_target_started_at: datetime | None = None
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
            cat = self.ftx1_band_drive.cat if self.ftx1_band_drive is not None else None
            radio_ready = bool(cat is not None and cat.identify())
            swr_unavailable_reason: str | None = None
            swr_unavailable_event: str | None = None
            if not radio_ready:
                if self.config.ftx1_swr_protection_enabled:
                    swr_unavailable_reason = "Hamlib radio not ready"
                    swr_unavailable_event = "Hamlib ne répond pas"
            elif self.config.ftx1_swr_protection_enabled:
                capability = getattr(cat, "swr_protection_capability", None)
                if not (callable(capability) and capability() == "numeric"):
                    swr_unavailable_reason = "numeric SWR measurement unavailable"
                    swr_unavailable_event = "Hamlib ne fournit pas de mesure SWR numérique"
            if swr_unavailable_reason is not None:
                self.antenna_safety_level = "fault"
                self.last_qso_notice = (
                    f"SWR SAFETY UNAVAILABLE - {swr_unavailable_reason} - AutoPilot disarmed"
                )
                self._antenna_event(
                    self._last_runtime_at or datetime.now(timezone.utc),
                    f"Armement refusé : protection SWR indisponible ({swr_unavailable_event})",
                )
                self.control.disarm(
                    "swr_protection_unavailable",
                    source="AutopilotRuntime.arm_control",
                )
                return False
            if not radio_ready:
                LOGGER.warning(
                    "[HAMLIB] radio source unavailable during ARM; ALC monitoring will retry during TX"
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
        if self._blocked_band_drive_change is not None:
            blocked = self._blocked_band_drive_change
            LOGGER.warning(
                "[BAND-HOP] Drive block for %s cleared by explicit operator ARM",
                blocked.band,
            )
            self._blocked_band_drive_change = None
            self._pending_ftx1_band_change = None
            self._pending_ftx1_profile_apply = None
            self._forced_band_hop_pending_band = None
            self.adaptive_state = AdaptiveState.NORMAL
        armed = self.control.arm()
        if armed:
            self._pending_watchdog_halt = None
            self._watchdog_halt_failed = False
            if self.last_qso_notice.startswith("SWR SAFETY"):
                self.last_qso_notice = ""
            band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
            self.opportunities.reset(band, self.status.mode, self._last_runtime_at or datetime.now(timezone.utc))
        return armed

    def reset_swr_safety_and_rearm(self) -> bool:
        """Clear session SWR safety state and resume after an explicit operator reset."""
        if self.status.transmitting:
            self.last_qso_notice = "SWR RESET REFUSED - transmission active"
            LOGGER.warning("[ANTENNA-SAFETY] Réinitialisation refusée : émission encore active")
            return False
        if self._swr_recovery is not None and self.ftx1_band_drive is not None:
            set_tuner = getattr(self.ftx1_band_drive.cat, "set_tuner_enabled", None)
            if callable(set_tuner) and not set_tuner(False):
                self.last_qso_notice = "SWR RESET REFUSED - tuner stop unconfirmed"
                LOGGER.warning("[ANTENNA-SAFETY] Réinitialisation refusée : arrêt du tuner non confirmé")
                return False
        excluded = tuple(sorted(self._swr_excluded_bands))
        self._swr_recovery = None
        self._swr_excluded_bands.clear()
        self._hamlib_swr_fault_latched = False
        self._hamlib_high_swr_samples = 0
        self._band_resume_at = None
        self._band_resume_band = None
        self.antenna_safety_level = "ok"
        self.antenna_safety_events.clear()
        self.last_qso_notice = ""
        LOGGER.warning(
            "[ANTENNA-SAFETY] Protection réinitialisée par l'opérateur; exclusions effacées=%s; réarmement demandé",
            ",".join(excluded) or "none",
        )
        return self.arm_control()

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

    def fail_safe_disarm(self, reason: str, source: str) -> HaltTxResult:
        """Request an immediate TX halt and always leave control disarmed."""
        try:
            return self._request_halt_tx(self.status.instance_id or None, reason)
        finally:
            self._disarm_control(reason, source)

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
        elif isinstance(packet, TuneStatePacket):
            self._observe_atu100_tune_state(packet, now)
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
                self.note_qso_completed(packet.dx_call)
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
                self.note_qso_completed(final_state.remote_callsign)
                self._log_ready_for_next_qso()
        self._expire_dial_frequency_request(now)
        self._expire_tx_audio_attenuation_request(now)
        self._query_tx_audio_attenuation_if_needed(self._attenuation_query_instance_id, now)
        self._complete_pending_ftx1_band_change(now)
        self._run_pending_atu100_tune(now)
        if self._hunt_active:
            self._run_hunt(now)
        else:
            if self._unconfirmed_dxcc_follow_enabled:
                self._run_unconfirmed_dxcc_watch(now)
            else:
                self._run_reply_rare_watch(now)
            self._run_forced_band_hop(now)
            if self._pending_watchdog_halt is None and not self._band_transition_pending():
                self._run_adaptive(now)
        if (
            self._pending_watchdog_halt is not None
            or self._band_transition_pending()
            or self.pending_tx_audio_attenuation is not None
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
        self.engine.note_station_working_other(event)
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
        if self._try_rare_reply_from_decode(event, now):
            return None
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
                halt_result = self._request_halt_tx(event.original.instance_id, "remote engaged another station")
                halted = halt_result.radio_ok and halt_result.wsjtx_ok
                if not halted and halt_result.wsjtx_ok and not self.status.transmitting:
                    halted = True
                    LOGGER.warning(
                        "[REMOTE-BUSY] Hamlib PTT OFF confirmation unavailable, but WSJT-X is already in RX"
                    )
                elif not halted and halt_result.wsjtx_ok and not isinstance(self.control, DryRunControl):
                    self._pending_watchdog_halt = PendingWatchdogHalt(
                        event.original.instance_id,
                        remote,
                        now + _WATCHDOG_HALT_CONFIRM_TIMEOUT,
                        "halt_tx_unconfirmed_remote_busy",
                        "AutopilotRuntime._poll_watchdog_halt_confirmation",
                        "REMOTE-BUSY",
                    )
                    halted = True
                    LOGGER.warning(
                        "[REMOTE-BUSY] HaltTx sent; waiting %.1f s for WSJT-X RX confirmation remote=%s",
                        _WATCHDOG_HALT_CONFIRM_TIMEOUT.total_seconds(),
                        remote,
                    )
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
        if (
            self._hunt_active
            and parsed.kind in {MessageKind.CQ, MessageKind.QRZ}
            and not self._hunt_accepts(parsed.sender)
        ):
            LOGGER.info("[HUNT] CQ ignored station=%s reason=not selected special target", parsed.sender)
            return None
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
                    self.note_qso_completed(state.remote_callsign)
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

    def _band_transition_pending(self) -> bool:
        """Whether a requested band change still needs confirmation or operator action."""
        return any(
            pending is not None
            for pending in (
                self._pending_dial_frequency,
                self._pending_ftx1_band_change,
                self._pending_ftx1_profile_apply,
                self._pending_atu100_tune,
                self._blocked_band_drive_change,
            )
        )
