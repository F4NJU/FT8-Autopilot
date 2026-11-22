"""Hamlib TX meter sampling and ALC drive regulation."""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta

from wsjtx_autopilot.worked.bands import BandResolver

LOGGER = logging.getLogger(__name__)
_SWR_TUNE_MIN_RF_WATTS = 0.25


class HamlibMetersMixin:
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
        swr_capability = getattr(cat, "swr_protection_capability", None)
        power = read_power() if callable(read_power) else None
        alc = read_alc() if callable(read_alc) else None
        self.last_hamlib_swr_protection = (
            str(swr_capability()) if callable(swr_capability) else "numeric" if callable(read_swr) else "uncertain"
        )
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
        rf_valid_for_swr = power is not None and float(power) >= _SWR_TUNE_MIN_RF_WATTS
        if (
            self.config.ftx1_swr_protection_enabled
            and rf_valid_for_swr
            and swr is not None
            and float(swr) >= self.config.ftx1_swr_limit
        ):
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
