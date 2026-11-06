import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from wsjtx_autopilot.config import AppPaths, SettingsStore, UserSettings
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.discord_presence import DiscordPresencePublisher
from wsjtx_autopilot.engine.decision import DecisionEngine
from wsjtx_autopilot.engine.cq import CqEligibility
from wsjtx_autopilot.engine.dxcc import CtyDatResolver, FallbackDxccResolver, PrefixDxccResolver
from wsjtx_autopilot.engine.models import EngineEvent, StationProfile
from wsjtx_autopilot.engine.scoring import CandidateScorer, ScoringPreferences
from wsjtx_autopilot.runtime import AutopilotRuntime, HuntRemoteSpot
from wsjtx_autopilot.wsjtx.listener import UdpListener
from wsjtx_autopilot.wsjtx.models import DecodePacket, HeartbeatPacket, QsoLoggedPacket, StatusPacket
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.bands import BandResolver
from wsjtx_autopilot.worked.sync import synchronize_adif
from wsjtx_autopilot.worked.store import WorkedQsoStore
from wsjtx_autopilot.wsjtx_process import WsjtxProcessManager

from .viewmodels import ActivityRow, SessionQsoMapPoint, StatusView

LOGGER = logging.getLogger(__name__)
CONNECTION_TIMEOUT_SECONDS = 45


class BackendWorker(QObject):
    started = Signal()
    stopped = Signal()
    error = Signal(str)
    connection_changed = Signal(bool)
    armed_changed = Signal(bool)
    swr_safety_triggered = Signal(str)
    activity_received = Signal(object)
    engine_event = Signal(object)
    status_changed = Signal(object)
    adaptive_changed = Signal(object)
    qso_changed = Signal(str, str, str, str, int, int, int, int, str)
    ftx1_band_profiles_changed = Signal(object)
    hunt_changed = Signal(bool, str)
    rare_station_worked = Signal(str, str)
    session_qso_logged = Signal(object)
    unconfirmed_dxcc_spotted = Signal(object)
    wavelog_lotw_changed = Signal(int, int, bool, str)
    diagnostics_changed = Signal(object)

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
        self._last_reported_armed = False
        self._last_swr_safety_notice = ""
        self._last_reported_hunt_active = False
        self._last_diagnostics: dict[str, object] | None = None
        self._discord_presence: DiscordPresencePublisher | None = None
        self._special_calls: set[str] = set()
        self._dxcc_resolver = None
        self._wsjtx_process = WsjtxProcessManager()

    @Slot()
    def start(self) -> None:
        try:
            self._open_backend()
        except Exception as exc:
            LOGGER.exception("Unable to start GUI backend")
            self._close_backend()
            self._wsjtx_process.stop()
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
        self._wsjtx_process.stop()
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
            self._wsjtx_process.stop()
            self.error.emit(str(exc))

    @Slot()
    def arm(self) -> None:
        LOGGER.info("[WORKER] ARM request received")
        if self._runtime is None or self._listener is None:
            self.error.emit("Backend is not running")
            return
        if self._settings.launch_wsjtx_with_autopilot and not self._connected:
            self.error.emit("WSJT-X est en cours de démarrage : attends sa connexion avant d’armer AutoPilot")
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
        armed = self._runtime.arm_control()
        if armed:
            self._last_swr_safety_notice = ""
        self._last_reported_armed = armed
        self.armed_changed.emit(armed)

    @Slot(str)
    def disarm(self, reason: str = "user_request") -> None:
        if self._runtime is None:
            return
        self._disarm_control(reason, "BackendWorker.disarm")
        self._last_reported_armed = False
        self.armed_changed.emit(False)

    @Slot()
    def reset_swr_safety(self) -> None:
        if self._runtime is None:
            self.error.emit("Backend is not running")
            return
        armed = self._runtime.reset_swr_safety_and_rearm()
        if armed:
            self._last_swr_safety_notice = ""
        else:
            self.error.emit(self._runtime.last_qso_notice or "Impossible de réinitialiser la protection SWR")
        self._last_reported_armed = armed
        self.armed_changed.emit(armed)

    @Slot(str)
    def ignore_station(self, station: str) -> None:
        if self._scorer is not None:
            until = datetime.now(timezone.utc) + timedelta(minutes=self._settings.ignore_minutes)
            self._scorer.ignore_station(station, until)

    @Slot(object)
    def update_special_calls(self, callsigns: object) -> None:
        activities = tuple(callsigns) if isinstance(callsigns, (list, tuple, set, frozenset)) else ()
        self._special_calls = {
            str(getattr(value, "callsign", value)).strip().upper()
            for value in activities
            if str(getattr(value, "callsign", value)).strip()
        }
        if self._scorer is not None:
            self._scorer.update_special_calls(self._special_calls)
        if self._runtime is not None:
            self._runtime.set_reply_rare_targets(activities)

    @Slot(bool)
    def set_reply_rare_tracking(self, enabled: bool) -> None:
        self._settings.pskreporter_mqtt_enabled = bool(enabled)
        self._settings.prioritize_special_calls_in_reply = bool(enabled)
        if self._scorer is not None:
            self._scorer.preferences.prioritize_special_calls = bool(enabled)
        if self._runtime is not None:
            self._runtime.set_remote_hunt_enabled(bool(enabled))
        LOGGER.info("[REPLY] rare priority + MQTT tracking=%s", bool(enabled))

    @Slot(bool)
    def set_favorable_path_priority(self, enabled: bool) -> None:
        self._settings.prioritize_favorable_path_in_reply = bool(enabled)
        if self._scorer is not None:
            self._scorer.preferences.prioritize_favorable_path = bool(enabled)
        LOGGER.info("[REPLY] favourable-path priority=%s", bool(enabled))

    @Slot(object)
    def update_space_weather(self, snapshot: object) -> None:
        if self._scorer is not None:
            self._scorer.update_space_weather(
                getattr(snapshot, "solar_flux", None),
                getattr(snapshot, "kp", None),
            )

    @Slot(bool)
    def set_rare_stations_only(self, enabled: bool) -> None:
        self._settings.rare_stations_only = bool(enabled)
        if self._scorer is not None:
            self._scorer.preferences.rare_stations_only = bool(enabled)
        LOGGER.info("[REPLY] rare stations only=%s", bool(enabled))

    @Slot(bool)
    def set_unconfirmed_dxcc_only(self, enabled: bool) -> None:
        self._settings.wavelog_unconfirmed_dxcc_only = bool(enabled)
        if self._scorer is not None:
            self._scorer.preferences.unconfirmed_dxcc_only = bool(enabled)
        if self._runtime is not None:
            self._runtime.set_unconfirmed_dxcc_follow_enabled(bool(enabled))
        LOGGER.info("[REPLY] unconfirmed LoTW DXCC priority=%s", bool(enabled))

    @Slot(bool)
    def set_exclude_worked_history(self, enabled: bool) -> None:
        self._settings.exclude_previously_worked_calls = bool(enabled)
        LOGGER.info("[REPLY] exclude previously worked calls=%s", bool(enabled))

    @Slot(object)
    def update_wavelog_lotw_calls(self, snapshot: object) -> None:
        available = bool(getattr(snapshot, "available", False))
        warning = str(getattr(snapshot, "warning", ""))
        callsigns = getattr(snapshot, "callsigns", ())
        if self._scorer is None or self._dxcc_resolver is None:
            return
        if not available:
            self._scorer.update_lotw_confirmed_dxcc(None)
            self.wavelog_lotw_changed.emit(0, 0, False, warning)
            return
        entities: set[str] = set()
        unresolved = 0
        for callsign in callsigns:
            metadata = self._dxcc_resolver.resolve(str(callsign))
            entity = metadata.primary_prefix.strip().upper()
            if entity:
                entities.add(entity)
            else:
                unresolved += 1
        self._scorer.update_lotw_confirmed_dxcc(entities)
        self.wavelog_lotw_changed.emit(len(entities), unresolved, True, warning)

    @Slot(object)
    def update_hunt_remote_spot(self, spot: object) -> None:
        received = self._remote_spot_from_object(spot)
        if received is None or self._runtime is None:
            return
        self._runtime.note_hunt_remote_spot(received)

    @Slot(object)
    def update_unconfirmed_dxcc_remote_spot(self, spot: object) -> None:
        received = self._remote_spot_from_object(spot)
        if received is None or self._runtime is None or self._scorer is None:
            return
        if self._scorer.is_unconfirmed_lotw_dxcc(received.callsign):
            if self._runtime.note_unconfirmed_dxcc_remote_spot(received):
                # The UI receives only LoTW-eligible, allowed-band spots;
                # never the unbounded raw MQTT discovery feed.
                self.unconfirmed_dxcc_spotted.emit(spot)

    @staticmethod
    def _remote_spot_from_object(spot: object) -> HuntRemoteSpot | None:
        try:
            return HuntRemoteSpot(
                str(getattr(spot, "callsign", "")),
                str(getattr(spot, "band", "")),
                getattr(spot, "observed_at"),
                getattr(spot, "frequency_hz", None),
                getattr(spot, "snr", None),
                str(getattr(spot, "receiver", "")),
                str(getattr(spot, "transmitter_grid", "")),
                str(getattr(spot, "receiver_grid", "")),
            )
        except (TypeError, ValueError):
            return None

    @Slot()
    def reset_adaptive_strategy(self) -> None:
        if self._runtime is not None:
            self._runtime.reset_adaptive_strategy()

    @Slot()
    def save_ftx1_current_band_profile(self) -> None:
        if self._runtime is not None:
            self._runtime.save_ftx1_current_band_profile()

    @Slot()
    def delete_ftx1_current_band_profile(self) -> None:
        if self._runtime is not None:
            self._runtime.delete_ftx1_current_band_profile()

    @Slot()
    def reset_all_ftx1_band_profiles(self) -> None:
        if self._runtime is not None:
            self._runtime.reset_all_ftx1_band_profiles()

    @Slot(object, str)
    def start_hunt(self, activities: object, manual_target: str = "") -> None:
        if self._runtime is None:
            self.error.emit("Backend is not running")
            return
        active = self._runtime.configure_hunt(activities, manual_target)
        self._last_reported_hunt_active = active
        self.hunt_changed.emit(active, manual_target if active else "")
        if not active:
            self.error.emit("Aucune cible Hunt n'utilise une bande FT8 autorisée et disponible")

    @Slot()
    def stop_hunt(self) -> None:
        if self._runtime is not None:
            self._runtime.stop_hunt()
        self._last_reported_hunt_active = False
        self.hunt_changed.emit(False, "")

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
                self._runtime.handle(received.packet, now, received.endpoint)
            if (
                self._last_packet_at is not None
                and (now - self._last_packet_at).total_seconds() > CONNECTION_TIMEOUT_SECONDS
            ):
                self._set_connected(False)
        except Exception as exc:
            self._handle_runtime_exception(exc)
            return
        try:
            self._publish_runtime_state(received, now)
        except Exception as exc:
            LOGGER.exception("[GUI] presentation_exception; control state unchanged")
            self.error.emit(str(exc))

    def _handle_runtime_exception(self, exc: Exception) -> None:
        if self._runtime is None:
            return
        LOGGER.exception("[GUI] backend_exception; requesting fail-safe TX halt and disarm")
        try:
            self._runtime.fail_safe_disarm("backend_exception", "BackendWorker._poll")
        except Exception:
            LOGGER.exception("[GUI] backend_exception fail-safe TX halt also failed")
        finally:
            self._last_reported_armed = False
            self.armed_changed.emit(False)
        self.error.emit(str(exc))

    def _publish_runtime_state(self, received, now: datetime) -> None:
        if self._runtime is None:
            return
        if received is not None:
            packet = received.packet
            if isinstance(packet, DecodePacket):
                self.activity_received.emit(ActivityRow.from_decode(packet, now))
            if isinstance(packet, QsoLoggedPacket):
                band = BandResolver().resolve(packet.tx_frequency) or "—"
                self.session_qso_logged.emit(SessionQsoMapPoint.from_qso_logged(packet, band))
            if isinstance(packet, QsoLoggedPacket) and packet.dx_call.strip().upper() in self._special_calls:
                band = BandResolver().resolve(packet.tx_frequency) or "—"
                self.rare_station_worked.emit(packet.dx_call.strip().upper(), band)
            if isinstance(packet, (StatusPacket, HeartbeatPacket)):
                if self._scorer is not None:
                    self._scorer.update_local_grid(self._runtime.status.local_grid)
                self.status_changed.emit(StatusView.from_status(self._runtime.status))
        safety_notice = self._runtime.last_qso_notice
        if safety_notice.startswith("SWR SAFETY") and safety_notice != self._last_swr_safety_notice:
            self._last_swr_safety_notice = safety_notice
            self.swr_safety_triggered.emit(safety_notice)
        session = self._runtime.engine.state.session
        remote_metadata = (
            self._scorer.dxcc_resolver.resolve(session.remote_callsign)
            if self._scorer is not None and session.remote_callsign
            else None
        )
        remote_detail = (
            f"{remote_metadata.country_name} / {remote_metadata.primary_prefix} / {remote_metadata.continent}"
            if remote_metadata is not None and remote_metadata.country_name != "unknown"
            else "-"
        )
        if remote_metadata is not None and remote_detail != "-":
            location_detail = self._runtime.working_location_detail(session.remote_callsign, remote_metadata)
            if location_detail:
                remote_detail = f"{remote_detail} / {location_detail}"
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
        )
        self._publish_discord_presence(session.remote_callsign)
        self.adaptive_changed.emit(self._runtime.adaptive_snapshot())
        diagnostics = self._runtime.session_diagnostic_snapshot()
        if diagnostics != self._last_diagnostics:
            self._last_diagnostics = diagnostics
            self.diagnostics_changed.emit(diagnostics)
        control = self._runtime.control
        backend_armed = isinstance(control, WsjtxUdpControl) and control.armed
        periodic_state = received is not None and isinstance(received.packet, (StatusPacket, HeartbeatPacket))
        if periodic_state or backend_armed != self._last_reported_armed:
            self._last_reported_armed = backend_armed
            if periodic_state:
                LOGGER.info("[WORKER] control state backend_armed=%s", backend_armed)
            self.armed_changed.emit(backend_armed)

    def _publish_discord_presence(self, remote_callsign: str) -> None:
        if self._discord_presence is None or self._runtime is None:
            return
        try:
            band = (
                BandResolver().resolve(self._runtime.status.dial_frequency)
                if self._runtime.status.dial_frequency
                else None
            )
            self._discord_presence.publish(
                self._runtime.status.mode,
                band,
                remote_callsign,
                self._connected,
            )
        except Exception as exc:
            LOGGER.exception("[DISCORD] presence disabled after publication failure")
            failed_publisher = self._discord_presence
            self._discord_presence = None
            close = getattr(failed_publisher, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    LOGGER.exception("[DISCORD] presence close failed after publication failure")
            self.error.emit(str(exc))

    def _open_backend(self) -> None:
        self._settings.normalize()
        self._discord_presence = DiscordPresencePublisher(
            self._settings.discord_presence_enabled,
            self._settings.discord_application_id,
        )
        store_path = self._paths.database_path
        LOGGER.info("[PATHS] database=%s", store_path)
        self._store = WorkedQsoStore(store_path)
        worked = WorkedTodayService(self._store)
        adif_path = self._paths.resolve_adif_path(self._settings.wsjtx_log_path)
        synchronize_adif(adif_path, worked, self._settings.sync_adif_on_startup)
        today = datetime.now(timezone.utc).date()
        worked.bootstrap(today)
        resolver = (
            CtyDatResolver(Path(self._settings.cty_dat_path))
            if self._settings.cty_dat_path
            else PrefixDxccResolver()
        )
        if self._settings.cty_dat_path:
            resolver = FallbackDxccResolver(resolver)
        self._dxcc_resolver = resolver
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
            prioritize_special_calls=self._settings.prioritize_special_calls_in_reply,
            prioritize_favorable_path=self._settings.prioritize_favorable_path_in_reply,
            rare_stations_only=self._settings.rare_stations_only,
            unconfirmed_dxcc_only=self._settings.wavelog_unconfirmed_dxcc_only,
            special_calls=set(self._special_calls),
        )
        self._scorer = CandidateScorer(preferences, resolver)
        config = self._settings.to_app_config(store_path)
        local_profile = StationProfile(config.local_callsign, resolver.resolve(config.local_callsign))
        cq_eligibility = CqEligibility(local_profile, resolver, config.respond_to_cq_dx)
        def worked_lookup(callsign: str, frequency_hz: int | None, observed_at: datetime):
            return worked.check(
                callsign,
                frequency_hz,
                observed_at,
                exclude_previously_worked=self._settings.exclude_previously_worked_calls,
            )

        engine = DecisionEngine(config, worked_lookup, self._scorer, self._on_engine_event, cq_eligibility)
        self._listener = UdpListener(config.bind_address, config.udp_port, timeout=0)
        wsjtx_launch = self._wsjtx_process.reconcile(
            self._settings.launch_wsjtx_with_autopilot,
            self._settings.wsjtx_executable_path,
        )
        LOGGER.info("[WSJTX] integrated lifecycle state=%s detail=%s", wsjtx_launch.state, wsjtx_launch.message)
        if wsjtx_launch.state in {"unavailable", "failed"}:
            self.error.emit(wsjtx_launch.message)
        control = WsjtxUdpControl(
            self._listener,
            config.stale_decode_seconds,
            None,
            self._paths.data_dir / "DISARM_AUTOPILOT",
            config.local_callsign,
            config.wsjtx_direct_reply_patched,
            armed=False,
        )
        self._runtime = AutopilotRuntime(
            config,
            engine,
            control,
            worked,
            ftx1_band_profiles_changed=self._persist_ftx1_band_profiles,
        )
        self._runtime.initialize_ftx1_cat2()
        self._runtime.set_unconfirmed_dxcc_follow_enabled(self._settings.wavelog_unconfirmed_dxcc_only)
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
        self._last_reported_armed = False
        self._last_diagnostics = None
        self._set_connected(False)
        self.armed_changed.emit(False)
        self.adaptive_changed.emit(self._runtime.adaptive_snapshot())

    def _close_backend(self) -> None:
        if self._discord_presence is not None:
            self._discord_presence.close()
        self._discord_presence = None
        if self._runtime is not None and self._runtime.ftx1_band_drive is not None:
            self._runtime.ftx1_band_drive.close()
        if self._listener is not None:
            self._listener.close()
        if self._store is not None:
            self._store.close()
        self._listener = None
        self._store = None
        self._runtime = None
        self._scorer = None
        self._set_connected(False)

    def _persist_ftx1_band_profiles(self, profiles: dict[str, dict[str, int]]) -> None:
        self._settings.ftx1_band_profiles = deepcopy(profiles)
        SettingsStore().save(self._settings)
        self.ftx1_band_profiles_changed.emit(deepcopy(profiles))

    def _on_engine_event(self, event: EngineEvent) -> None:
        if event.kind.name == "CANDIDATE_SELECTED":
            LOGGER.info("[GUI] candidate selected station=%s", event.station)
        elif event.kind.name == "PENDING_DIRECT_ADDED":
            LOGGER.info("[GUI] next pending Direct Call: %s", event.station)
        self.engine_event.emit(event)

    def _set_connected(self, connected: bool) -> None:
        if not connected and self._connected and self._runtime is not None:
            self._disarm_control("wsjtx_offline", "BackendWorker._set_connected")
            self._runtime.clear_finalization("WSJT-X connection lost")
            self.armed_changed.emit(False)
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)

    def _disarm_control(self, reason: str, source: str) -> None:
        if self._runtime is None:
            return
        control = self._runtime.control
        if isinstance(control, WsjtxUdpControl):
            control.disarm(reason, source=source)
        else:
            control.disarm(reason)

