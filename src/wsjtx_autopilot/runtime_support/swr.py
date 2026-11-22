"""Antenna SWR recovery: RX wait, tune carrier, verification and fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .hamlib_meters import _SWR_TUNE_MIN_RF_WATTS

LOGGER = logging.getLogger(__name__)
_SWR_RX_CONFIRM_TIMEOUT = timedelta(seconds=5)
_SWR_TUNE_TIMEOUT = timedelta(seconds=15)
_SWR_TUNE_RF_START_TIMEOUT = timedelta(seconds=3)
_SWR_TUNE_POLL_INTERVAL = timedelta(seconds=0.5)
_SWR_TUNE_MIN_ACTIVE_TIME = timedelta(seconds=2)
_SWR_TUNE_MIN_SAMPLES = 2


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
    tuner_state_supported: bool = False
    seen_tuning_state: bool = False
    swr_samples: list[float] | None = None
    tune_attempts: int = 0

    def __post_init__(self) -> None:
        if self.swr_samples is None:
            self.swr_samples = []



class SwrRecoveryMixin:
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
            recovery.tuner_state_supported = bool(getattr(cat, "tuner_reports_active_state", False))
            recovery.seen_tuning_state = False
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
        read_tuner_state = getattr(cat, "read_tuner_state", None)
        tuner_state = read_tuner_state() if recovery.tuner_state_supported and callable(read_tuner_state) else None
        power = read_power_meter() if callable(read_power_meter) else None
        swr = read_swr() if callable(read_swr) else None
        self.last_hamlib_power_watts = float(power) if power is not None else None
        self.last_hamlib_swr = float(swr) if swr is not None else None
        elapsed = now - recovery.tune_started_at
        if power is not None and float(power) >= _SWR_TUNE_MIN_RF_WATTS:
            recovery.seen_rf = True
            if swr is not None:
                recovery.swr_samples.append(float(swr))
            LOGGER.info("[HAMLIB] tune meters band=%s SWR=%s PWR_W=%s", recovery.current_band, swr, power)
        if tuner_state == 3:
            recovery.seen_tuning_state = True
        LOGGER.info(
            "[HAMLIB] tune poll band=%s elapsed=%.2fs tuner_state=%s power_w=%s swr=%s "
            "seen_active=%s seen_rf=%s samples=%d",
            recovery.current_band,
            elapsed.total_seconds(),
            tuner_state,
            power,
            swr,
            recovery.seen_tuning_state,
            recovery.seen_rf,
            len(recovery.swr_samples or []),
        )
        tuner_finished = (
            recovery.tuner_state_supported
            and recovery.seen_tuning_state
            and recovery.seen_rf
            and elapsed >= _SWR_TUNE_MIN_ACTIVE_TIME
            and tuner_state in {0, 1}
        )
        generic_tune_finished = (
            not recovery.tuner_state_supported
            and recovery.seen_rf
            and (power is None or float(power) < _SWR_TUNE_MIN_RF_WATTS)
        )
        if tuner_finished or generic_tune_finished:
            self._evaluate_swr_tune(now)
            return
        # AC..3 alone is not sufficient proof that the FTX-1 really emitted a
        # tuning carrier: rigctld can briefly return a stale state immediately
        # after the start command. Require RF too before accepting AC..1 as the
        # end of the sweep.
        tune_confirmed = (
            recovery.seen_tuning_state and recovery.seen_rf
            if recovery.tuner_state_supported
            else recovery.seen_rf
        )
        if not tune_confirmed and now - recovery.tune_started_at >= _SWR_TUNE_RF_START_TIMEOUT:
            self._finish_swr_recovery_fault(
                now,
                "accord automatique non démarré (état tuner/RF non confirmé, bande conservée)",
            )
            return
        if now - recovery.tune_started_at >= _SWR_TUNE_TIMEOUT:
            self._complete_swr_tune(now, success=False, detail="délai d'accord dépassé")

    def _evaluate_swr_tune(self, now: datetime) -> None:
        recovery = self._swr_recovery
        if recovery is None:
            return
        samples = recovery.swr_samples or []
        # The FTX-1 reports AC..3 for the whole tuner sweep and returns to
        # AC..1 when it is finished.  Ignore the expected high SWR at the
        # beginning and require the last two RF-valid samples to be good.
        terminal_samples = samples[-_SWR_TUNE_MIN_SAMPLES:]
        measured = max(terminal_samples) if terminal_samples else None
        if (
            len(terminal_samples) >= _SWR_TUNE_MIN_SAMPLES
            and measured is not None
            and measured < self.config.ftx1_swr_limit
        ):
            self._complete_swr_tune(now, success=True, detail=f"SWR final {measured:.2f}")
            return
        # A completed tuner cycle with even one RF-valid sample above the
        # safety limit is enough to reject this band.  Requiring two samples
        # here used to turn a clear SWR 5.0 failure into a global disarm before
        # the recovery loop could exclude the band and try the next one.
        if measured is not None and measured >= self.config.ftx1_swr_limit:
            count = len(terminal_samples)
            self._complete_swr_tune(
                now,
                success=False,
                detail=f"SWR final {measured:.2f} ({count} mesure{'s' if count > 1 else ''} RF)",
            )
            return
        if len(samples) < _SWR_TUNE_MIN_SAMPLES:
            self._finish_swr_recovery_fault(now, "mesures SWR insuffisantes après accord, bande conservée")
            return
        detail = f"SWR final {measured:.2f}"
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
            read_power = getattr(cat, "read_rf_power_fraction", None)
            current_power = read_power() if callable(read_power) else None
            radio_restores_power = bool(getattr(cat, "tuner_reports_active_state", False))
            if radio_restores_power:
                restored = current_power is not None and abs(float(current_power) - recovery.original_power_fraction) <= 0.011
            else:
                set_power = getattr(cat, "set_rf_power_fraction", None)
                restored = bool(callable(set_power) and set_power(recovery.original_power_fraction))
            self._antenna_event(
                now,
                f"Puissance radio restaurée automatiquement à {recovery.original_power_fraction * 100:.0f} W"
                if restored and radio_restores_power
                else f"Puissance restaurée à {recovery.original_power_fraction * 100:.0f} W"
                if restored
                else "Échec de restauration de la puissance RF",
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
        recovery.tuner_state_supported = False
        recovery.seen_tuning_state = False
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
