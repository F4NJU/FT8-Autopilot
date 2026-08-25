import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from wsjtx_autopilot.config import AppConfig, AppPaths, UserSettings
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, UnknownDxccResolver
from wsjtx_autopilot.engine.models import EngineEvent, StationProfile
from wsjtx_autopilot.engine.scoring import CandidateScorer, ScoringPreferences
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.listener import UdpListener
from wsjtx_autopilot.wsjtx.models import DecodePacket, HeartbeatPacket, StatusPacket
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.sync import synchronize_adif
from wsjtx_autopilot.worked.store import WorkedQsoStore

from .viewmodels import ActivityRow, StatusView

LOGGER = logging.getLogger(__name__)
CONNECTION_TIMEOUT_SECONDS = 45


class BackendWorker(QObject):
    started = Signal()
    stopped = Signal()
    error = Signal(str)
    connection_changed = Signal(bool)
    armed_changed = Signal(bool)
    activity_received = Signal(object)
    engine_event = Signal(object)
    status_changed = Signal(object)
    qso_changed = Signal(str, str, str, str, int, int, int, int, str, str, str, str)

    def __init__(self, settings: UserSettings) -> None:
        super().__init__()
        self._settings = deepcopy(settings)
        self._paths = AppPaths.from_environment().ensure_directories()
        self._timer: QTimer | None = None
        self._listener: UdpListener | None = None
        self._store: WorkedQsoStore | None = None
        self._runtime: AutopilotRuntime | None = None
        self._scorer: CandidateScorer | None = None
        self._last_packet_at: datetime | None = None
        self._connected = False

    @Slot()
    def start(self) -> None:
        try:
            self._open_backend()
        except Exception as exc:
            LOGGER.exception("Unable to start GUI backend")
            self._close_backend()
            self.error.emit(str(exc))
            return
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self.started.emit()

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.disarm("application shutdown")
        if self._runtime is not None:
            self._runtime.clear_finalization("application shutdown")
        self._close_backend()
        self.stopped.emit()

    @Slot(object)
    def apply_settings(self, settings: UserSettings) -> None:
        self.disarm("settings changed")
        self._close_backend()
        self._settings = deepcopy(settings)
        try:
            self._open_backend()
        except Exception as exc:
            LOGGER.exception("Unable to apply GUI settings")
            self._close_backend()
            self.error.emit(str(exc))

    @Slot()
    def arm(self) -> None:
        if self._runtime is None or self._listener is None:
            self.error.emit("Backend is not running")
            return
        control = self._runtime.control
        if not isinstance(control, WsjtxUdpControl):
            self.error.emit("GUI runtime is not using WsjtxUdpControl")
            return
        LOGGER.info(
            "[GUI] ARM runtime=%d control=%d implementation=%s transport=%d",
            id(self._runtime),
            id(control),
            type(control).__name__,
            id(self._listener),
        )
        control.arm()
        self.armed_changed.emit(control.armed)

    @Slot(str)
    def disarm(self, reason: str = "operator request") -> None:
        if self._runtime is None:
            return
        self._runtime.control.disarm(reason)
        self.armed_changed.emit(False)

    @Slot(str)
    def ignore_station(self, station: str) -> None:
        if self._scorer is not None:
            until = datetime.now(timezone.utc) + timedelta(minutes=self._settings.ignore_minutes)
            self._scorer.ignore_station(station, until)

    @Slot()
    def _poll(self) -> None:
        if self._listener is None or self._runtime is None:
            return
        now = datetime.now(timezone.utc)
        try:
            received = self._listener.receive()
            if received is None:
                self._runtime.handle(None, now)
            else:
                self._last_packet_at = now
                self._set_connected(True)
                packet = received.packet
                if isinstance(packet, DecodePacket):
                    self.activity_received.emit(ActivityRow.from_decode(packet, now))
                self._runtime.handle(packet, now, received.endpoint)
                if isinstance(packet, (StatusPacket, HeartbeatPacket)):
                    self.status_changed.emit(StatusView.from_status(self._runtime.status))
            session = self._runtime.engine.state.session
            remote_metadata = (
                self._scorer.dxcc_resolver.resolve(session.remote_callsign)
                if self._scorer is not None and session.remote_callsign
                else None
            )
            remote_detail = (
                f"{remote_metadata.country_name} / {remote_metadata.primary_prefix} / {remote_metadata.continent}"
                if remote_metadata is not None
                else "-"
            )
            self.qso_changed.emit(
                session.state.name,
                session.remote_callsign or "-",
                remote_detail,
                session.progress_stage.name if session.progress_stage is not None else "-",
                session.no_progress_periods,
                self._runtime.config.max_no_progress_periods,
                session.remote_cq_count,
                self._runtime.config.max_remote_cq_during_attempt,
                self._runtime.last_qso_notice,
                f"RX  {session.remote_df} Hz" if session.remote_df is not None else "RX  -",
                f"TX  {session.chosen_tx_df} Hz" if session.chosen_tx_df is not None else "TX  -",
                (
                    f"Smart TX  {session.tx_df_reason}"
                    + (f" / gap {session.tx_df_gap_width} Hz" if session.tx_df_gap_width else "")
                    if session.tx_df_reason
                    else "Smart TX  -"
                ),
            )
            control = self._runtime.control
            self.armed_changed.emit(
                isinstance(control, WsjtxUdpControl) and control.armed,
            )
            if (
                self._last_packet_at is not None
                and (now - self._last_packet_at).total_seconds() > CONNECTION_TIMEOUT_SECONDS
            ):
                self._set_connected(False)
        except Exception as exc:
            LOGGER.exception("GUI backend poll failed")
            self.error.emit(str(exc))

    def _open_backend(self) -> None:
        self._settings.normalize()
        store_path = self._paths.database_path
        LOGGER.info("[PATHS] database=%s", store_path)
        self._store = WorkedQsoStore(store_path)
        worked = WorkedTodayService(self._store)
        adif_path = self._paths.resolve_adif_path(self._settings.wsjtx_log_path)
        synchronize_adif(adif_path, worked, self._settings.sync_adif_on_startup)
        today = datetime.now(timezone.utc).date()
        LOGGER.info("[WORKED] loaded %d QSOs for %s UTC", worked.count(today), today.isoformat())
        resolver = (
            CtyDatResolver(Path(self._settings.cty_dat_path))
            if self._settings.cty_dat_path
            else UnknownDxccResolver()
        )
        preferences = ScoringPreferences(
            preferred_continents=set(self._settings.preferred_continents),
            preferred_dxcc=set(self._settings.preferred_dxcc),
            direct_call_policy=self._settings.direct_call_policy,
            allow_dupes=self._settings.allow_dupes,
            allow_direct_call_dupes=self._settings.allow_direct_call_dupes,
            minimum_snr=self._settings.minimum_snr,
            favor_strong_signals=self._settings.favor_strong_signals,
            direct_call_bonus=self._settings.direct_call_bonus,
            preferred_dxcc_bonus=self._settings.preferred_dxcc_bonus,
            preferred_continent_bonus=self._settings.preferred_continent_bonus,
            signal_bonus_max=self._settings.signal_bonus_max,
            blacklist=set(self._settings.blacklist),
            pota_policy=self._settings.pota_policy,
            sota_policy=self._settings.sota_policy,
            qrp_policy=self._settings.qrp_policy,
            activity_priority_bonus=self._settings.activity_priority_bonus,
        )
        self._scorer = CandidateScorer(preferences, resolver)
        config = AppConfig(
            local_callsign=self._settings.local_callsign,
            bind_address=self._settings.bind_address,
            udp_port=self._settings.udp_port,
            allow_dupes=self._settings.allow_dupes,
            max_initiation_attempts=1,
            wsjtx_direct_reply_patched=self._settings.direct_reply_patched,
            wsjtx_set_tx_df_patched=self._settings.direct_reply_patched,
            worked_store_path=store_path,
            wsjtx_log_path=Path(self._settings.wsjtx_log_path) if self._settings.wsjtx_log_path else None,
            respond_to_cq_dx=self._settings.respond_to_cq_dx,
            max_no_progress_periods=self._settings.max_no_progress_periods,
            stalled_qso_cooldown_seconds=self._settings.stalled_qso_cooldown_seconds,
            remote_busy_cooldown_seconds=self._settings.remote_busy_cooldown_seconds,
            max_remote_cq_during_attempt=self._settings.max_remote_cq_during_attempt,
            remote_returned_to_cq_cooldown_seconds=self._settings.remote_returned_to_cq_cooldown_seconds,
            finalization_hold_periods=self._settings.finalization_hold_periods,
            max_final_retries=self._settings.max_final_retries,
            smart_tx_frequency=self._settings.smart_tx_frequency,
            smart_tx_find_free=self._settings.smart_tx_find_free,
            smart_tx_fallback_remote=self._settings.smart_tx_fallback_remote,
            occupied_guard_hz=self._settings.occupied_guard_hz,
            occupancy_history_seconds=self._settings.occupancy_history_seconds,
            tx_df_min=self._settings.tx_df_min,
            tx_df_max=self._settings.tx_df_max,
            minimum_free_gap_hz=self._settings.minimum_free_gap_hz,
            cty_dat_path=Path(self._settings.cty_dat_path) if self._settings.cty_dat_path else None,
        )
        local_profile = StationProfile(config.local_callsign, resolver.resolve(config.local_callsign))
        cq_eligibility = CqEligibility(local_profile, resolver, config.respond_to_cq_dx)
        engine = DecisionEngine(config, worked.check, self._scorer, self._on_engine_event, cq_eligibility)
        self._listener = UdpListener(config.bind_address, config.udp_port, timeout=0)
        control = WsjtxUdpControl(
            self._listener,
            config.stale_decode_seconds,
            None,
            self._paths.data_dir / "DISARM_AUTOPILOT",
            config.local_callsign,
            self._settings.direct_reply_patched,
            armed=False,
        )
        self._runtime = AutopilotRuntime(config, engine, control, worked)
        LOGGER.info(
            "[GUI] backend runtime=%d control=%d runtime_control=%d transport=%d implementation=%s armed=%s",
            id(self._runtime),
            id(control),
            id(self._runtime.control),
            id(self._listener),
            type(control).__name__,
            control.armed,
        )
        self._last_packet_at = None
        self._set_connected(False)
        self.armed_changed.emit(False)

    def _close_backend(self) -> None:
        if self._listener is not None:
            self._listener.close()
        if self._store is not None:
            self._store.close()
        self._listener = None
        self._store = None
        self._runtime = None
        self._scorer = None
        self._set_connected(False)

    def _on_engine_event(self, event: EngineEvent) -> None:
        if event.kind.name == "CANDIDATE_SELECTED":
            LOGGER.info("[GUI] candidate selected station=%s", event.station)
        self.engine_event.emit(event)

    def _set_connected(self, connected: bool) -> None:
        if not connected and self._connected and self._runtime is not None:
            self._runtime.control.disarm("WSJT-X connection lost")
            self._runtime.clear_finalization("WSJT-X connection lost")
            self.armed_changed.emit(False)
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)
