import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wsjtx_autopilot.config import AppConfig, AppPaths
from wsjtx_autopilot.control.base import ControlAdapter, DatagramTransport
from wsjtx_autopilot.control.dry_run import DryRunControl
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.runtime import AutopilotRuntime
from wsjtx_autopilot.wsjtx.capture import DatagramRecorder, replay_datagrams
from wsjtx_autopilot.wsjtx.listener import UdpListener
from wsjtx_autopilot.wsjtx.protocol import ProtocolError, parse_datagram
from wsjtx_autopilot.logging_setup import configure_logging
from wsjtx_autopilot.worked.sync import synchronize_adif
from wsjtx_autopilot.worked.service import WorkedTodayService
from wsjtx_autopilot.worked.store import WorkedQsoStore

LOGGER = logging.getLogger(__name__)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WSJT-X FT8 AutoPilot")
    parser.add_argument("--gui", action="store_true", help="launch the Windows desktop interface")
    parser.add_argument("--callsign", default="F4NJU", help="local amateur callsign")
    parser.add_argument("--bind", default="127.0.0.1", help="UDP bind address")
    parser.add_argument("--port", default=2237, type=int, help="WSJT-X UDP server port")
    parser.add_argument("--stale-seconds", default=15.0, type=float, help="maximum decode age")
    parser.add_argument("--allow-dupes", action="store_true", help="allow same-day/same-band duplicate QSOs")
    parser.add_argument(
        "--respond-to-cq-dx",
        action="store_true",
        help="respond to CQ DX (disabled by default because DX is relative)",
    )
    parser.add_argument(
        "--max-no-progress-periods",
        default=10,
        type=_positive_int,
        help="abort after this many remote responses without protocol progress",
    )
    parser.add_argument(
        "--stalled-qso-cooldown-seconds",
        default=300.0,
        type=float,
        help="temporary ignore duration after a stalled QSO",
    )
    parser.add_argument(
        "--remote-busy-cooldown-seconds",
        default=180.0,
        type=float,
        help="station-only cooldown when the remote starts another QSO",
    )
    parser.add_argument("--max-remote-cq-during-attempt", default=2, type=_positive_int)
    parser.add_argument("--remote-returned-to-cq-cooldown-seconds", default=90.0, type=float)
    parser.add_argument("--finalization-hold-periods", default=1, type=_positive_int)
    parser.add_argument("--max-final-retries", default=3, type=int)
    parser.add_argument(
        "--worked-store",
        default=AppPaths.from_environment().database_path,
        type=Path,
        help="persistent Worked Today SQLite database",
    )
    parser.add_argument("--wsjtx-log", type=Path, help="path to wsjtx_log.adi for startup synchronization")
    parser.add_argument("--cty-dat", type=Path, help="offline cty.dat used for directed CQ matching")
    parser.add_argument(
        "--dry-run-cooldown-seconds",
        default=90.0,
        type=float,
        help="delay before proposing the same station again",
    )
    parser.add_argument("--control", action="store_true", help="enable the WSJT-X control adapter")
    parser.add_argument(
        "--arm-auto-reply",
        action="store_true",
        help="explicitly arm Reply packets (requires --control)",
    )
    parser.add_argument(
        "--max-actions",
        default=1,
        type=_positive_int,
        help="maximum successfully sent Reply initiations",
    )
    parser.add_argument(
        "--wsjtx-direct-reply-patched",
        action="store_true",
        help="allow direct Reply only with the documented patched WSJT-X build",
    )
    parser.add_argument(
        "--direct-reply-confirmation-timeout",
        default=20.0,
        type=float,
        help="seconds to wait for coherent WSJT-X Status after Direct Reply",
    )
    parser.add_argument(
        "--kill-switch-file",
        default=AppPaths.from_environment().data_dir / "DISARM_AUTOPILOT",
        type=Path,
        help="creating this file immediately disarms control",
    )
    capture = parser.add_mutually_exclusive_group()
    capture.add_argument("--record", type=Path, help="record raw UDP datagrams to a capture file")
    capture.add_argument("--replay", type=Path, help="replay a capture file without opening a UDP socket")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_control(
    transport: DatagramTransport,
    config: AppConfig,
    kill_switch_file: Path | None,
) -> ControlAdapter:
    if not (config.control_enabled and config.auto_reply_armed):
        if config.control_enabled or config.auto_reply_armed:
            LOGGER.warning("[CONTROL] incomplete arming flags; remaining in dry-run")
        return DryRunControl()
    return WsjtxUdpControl(
        transport,
        config.stale_decode_seconds,
        config.max_initiation_attempts,
        kill_switch_file,
        config.local_callsign,
        config.wsjtx_direct_reply_patched,
    )


def run(
    config: AppConfig,
    worked_service: WorkedTodayService,
    record_path: Path | None = None,
    kill_switch_file: Path | None = None,
) -> None:
    LOGGER.info("Listening on udp://%s:%d", config.bind_address, config.udp_port)

    recorder = DatagramRecorder(record_path) if record_path is not None else None
    try:
        with UdpListener(config.bind_address, config.udp_port, record=recorder) as listener:
            control = build_control(listener, config, kill_switch_file)
            runtime = AutopilotRuntime(config, control=control, worked_service=worked_service)
            if isinstance(control, WsjtxUdpControl) and control.armed:
                LOGGER.critical("[AUTO CONTROL ARMED]")
                LOGGER.critical("WSJT-X Reply packets may initiate transmissions")
            for received in listener.packets():
                now = datetime.now(timezone.utc)
                if received is None:
                    runtime.handle(None, now)
                else:
                    runtime.handle(received.packet, now, received.endpoint)
    finally:
        if recorder is not None:
            recorder.close()


def replay(config: AppConfig, path: Path, worked_service: WorkedTodayService) -> None:
    runtime = AutopilotRuntime(config, worked_service=worked_service)
    first_timestamp: float | None = None
    replay_start = datetime.now(timezone.utc)
    last_now = replay_start
    LOGGER.info("Replaying WSJT-X capture %s (dry-run only)", path)
    for captured in replay_datagrams(path):
        if first_timestamp is None:
            first_timestamp = captured.timestamp
        last_now = replay_start + timedelta(seconds=captured.timestamp - first_timestamp)
        try:
            packet = parse_datagram(captured.data)
        except ProtocolError:
            LOGGER.exception("Rejected malformed captured WSJT-X datagram")
            continue
        runtime.handle(packet, last_now)
    runtime.handle(None, last_now + timedelta(seconds=config.candidate_collection_seconds))


def main() -> None:
    args = _arguments()
    if args.gui:
        from wsjtx_autopilot.gui.app import run_gui

        raise SystemExit(run_gui())
    paths = AppPaths.from_environment().ensure_directories()
    configure_logging(paths, logging.DEBUG if args.verbose else logging.INFO, console=True)
    config = AppConfig(
        local_callsign=args.callsign.upper(),
        bind_address=args.bind,
        udp_port=args.port,
        stale_decode_seconds=args.stale_seconds,
        dry_run_cooldown_seconds=args.dry_run_cooldown_seconds,
        max_initiation_attempts=args.max_actions,
        control_enabled=args.control,
        auto_reply_armed=args.arm_auto_reply,
        wsjtx_direct_reply_patched=args.wsjtx_direct_reply_patched,
        direct_reply_confirmation_timeout_seconds=args.direct_reply_confirmation_timeout,
        allow_dupes=args.allow_dupes,
        respond_to_cq_dx=args.respond_to_cq_dx,
        max_no_progress_periods=args.max_no_progress_periods,
        stalled_qso_cooldown_seconds=max(0.0, args.stalled_qso_cooldown_seconds),
        remote_busy_cooldown_seconds=max(0.0, args.remote_busy_cooldown_seconds),
        max_remote_cq_during_attempt=args.max_remote_cq_during_attempt,
        remote_returned_to_cq_cooldown_seconds=max(0.0, args.remote_returned_to_cq_cooldown_seconds),
        finalization_hold_periods=args.finalization_hold_periods,
        max_final_retries=max(0, args.max_final_retries),
        cty_dat_path=args.cty_dat,
        worked_store_path=args.worked_store,
        wsjtx_log_path=args.wsjtx_log,
    )
    store = WorkedQsoStore(config.worked_store_path)
    worked_service = WorkedTodayService(store)
    LOGGER.info("[PATHS] database=%s", config.worked_store_path)
    _synchronize_wsjtx_log(config, worked_service, paths)
    today = datetime.now(timezone.utc).date()
    LOGGER.info("[WORKED] loaded %d QSOs for UTC date %s", worked_service.count(today), today.isoformat())
    LOGGER.info("[WORKED] duplicate policy: same callsign + same band + same UTC day%s", " (override enabled)" if config.allow_dupes else "")
    try:
        if args.replay is not None:
            replay(config, args.replay, worked_service)
        else:
            run(
                config,
                worked_service,
                args.record,
                args.kill_switch_file,
            )
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
    finally:
        store.close()


def _synchronize_wsjtx_log(config: AppConfig, service: WorkedTodayService, paths: AppPaths) -> None:
    synchronize_adif(paths.resolve_adif_path(config.wsjtx_log_path), service)


if __name__ == "__main__":
    main()
