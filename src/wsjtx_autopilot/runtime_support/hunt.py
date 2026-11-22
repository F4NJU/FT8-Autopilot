"""Hunt target tracking and remote-spot band guidance."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from wsjtx_autopilot.engine.propagation import mqtt_path_probability
from wsjtx_autopilot.worked.bands import BandResolver

from .models import HuntRemoteSpot, HuntTarget

LOGGER = logging.getLogger(__name__)
_MINIMUM_DXCC_MQTT_PATH_PROBABILITY = 55


class HuntMixin:
    def configure_hunt(self, activities: object, manual_target: str = "") -> bool:
        allowed = set(self.config.allowed_auto_hop_bands)
        targets = self._rare_targets_from_activities(activities)
        selected = manual_target.strip().upper()
        if selected:
            targets = [target for target in targets if target.callsign == selected]
        targets.sort(key=lambda target: (-target.path_score, target.callsign))
        self.engine.clear_candidates()
        self._hunt_targets = tuple(targets)
        self._hunt_manual_target = selected
        self._hunt_active = bool(targets)
        self._hunt_band_started_at = None
        self._hunt_observed_band = None
        self._hunt_rotation_index = 0
        LOGGER.info(
            "[HUNT] %s targets=%s manual=%s allowed_bands=%s",
            "started" if self._hunt_active else "refused",
            ",".join(target.callsign for target in targets) or "-",
            selected or "auto",
            ",".join(sorted(allowed)) or "-",
        )
        return self._hunt_active

    def set_reply_rare_targets(self, activities: object) -> None:
        """Update rare calls eligible for Reply-mode remote band guidance."""
        self._reply_rare_targets = tuple(self._rare_targets_from_activities(activities))
        LOGGER.info("[REPLY] rare remote tracking targets=%s", ",".join(item.callsign for item in self._reply_rare_targets) or "-")

    def _rare_targets_from_activities(self, activities: object) -> list[HuntTarget]:
        allowed = set(self.config.allowed_auto_hop_bands)
        targets: list[HuntTarget] = []
        for activity in activities if isinstance(activities, (tuple, list)) else ():
            callsign = str(getattr(activity, "callsign", "")).strip().upper()
            bands = tuple(
                band for band in dict.fromkeys(str(value).lower() for value in getattr(activity, "bands", ()))
                if band in allowed and band not in self._swr_excluded_bands and self._band_frequency(band, "FT8") is not None
            )
            best_band = str(getattr(activity, "best_band", "") or "").lower() or None
            if best_band not in bands:
                best_band = bands[0] if bands else None
            if callsign and bands:
                targets.append(HuntTarget(callsign, bands, best_band, int(getattr(activity, "path_score", 0))))
        return targets

    def set_remote_hunt_enabled(self, enabled: bool) -> None:
        self._remote_hunt_enabled = bool(enabled)
        LOGGER.info("[HUNT] PSK Reporter remote spot guidance=%s", self._remote_hunt_enabled)

    def set_unconfirmed_dxcc_follow_enabled(self, enabled: bool) -> None:
        self._unconfirmed_dxcc_follow_enabled = bool(enabled)
        if not enabled:
            self._unconfirmed_dxcc_remote_spots.clear()
            self._unconfirmed_dxcc_target = ""
            self._unconfirmed_dxcc_target_started_at = None
        LOGGER.info("[REPLY] PSK Reporter LoTW-missing DXCC guidance=%s", self._unconfirmed_dxcc_follow_enabled)

    def note_hunt_remote_spot(self, spot: HuntRemoteSpot) -> bool:
        """Record an advisory spot for a selected Hunt target.

        It is deliberately not inserted into the candidate engine: only a
        local WSJT-X decode can produce a reply or any RF transmission.
        """
        callsign = spot.callsign.strip().upper()
        band = spot.band.strip().lower()
        if not self._remote_hunt_enabled or not callsign or not band:
            return False
        if not self._rare_target_accepts(callsign):
            return False
        if band in self._swr_excluded_bands or self._band_frequency(band, "FT8") is None:
            return False
        if spot.frequency_hz is not None and BandResolver().resolve(spot.frequency_hz) != band:
            LOGGER.warning("[HUNT] remote spot ignored: frequency=%s does not match band=%s", spot.frequency_hz, band)
            return False
        current = self._hunt_remote_spots.get(callsign)
        if current is not None and current.observed_at >= spot.observed_at:
            return False
        self._hunt_remote_spots[callsign] = HuntRemoteSpot(
            callsign, band, spot.observed_at, spot.frequency_hz, spot.snr, spot.receiver
        )
        LOGGER.info("[HUNT] remote FT8 spot target=%s band=%s receiver=%s", callsign, band, spot.receiver or "-")
        return True

    def note_unconfirmed_dxcc_remote_spot(self, spot: HuntRemoteSpot) -> bool:
        """Keep an advisory spot for a Wavelog-unconfirmed DXCC only."""
        callsign = spot.callsign.strip().upper()
        band = spot.band.strip().lower()
        if not self._unconfirmed_dxcc_follow_enabled or not callsign or not band:
            return False
        if band in self._swr_excluded_bands or band not in self.config.allowed_auto_hop_bands:
            return False
        if self._band_frequency(band, "FT8") is None:
            return False
        if spot.frequency_hz is not None and BandResolver().resolve(spot.frequency_hz) != band:
            return False
        path_probability = mqtt_path_probability(
            self.status.local_grid,
            spot.transmitter_grid,
            spot.receiver_grid,
            band,
            spot.observed_at,
            spot.snr,
        )
        if path_probability < _MINIMUM_DXCC_MQTT_PATH_PROBABILITY:
            LOGGER.info(
                "[REPLY] remote FT8 spot skipped unconfirmed-dxcc=%s band=%s path_probability=%d",
                callsign,
                band,
                path_probability,
            )
            return False
        current = self._unconfirmed_dxcc_remote_spots.get(callsign)
        if current is not None and current.observed_at >= spot.observed_at:
            return False
        self._unconfirmed_dxcc_remote_spots[callsign] = HuntRemoteSpot(
            callsign,
            band,
            spot.observed_at,
            spot.frequency_hz,
            spot.snr,
            spot.receiver,
            spot.transmitter_grid,
            spot.receiver_grid,
            path_probability,
        )
        LOGGER.info(
            "[REPLY] remote FT8 spot unconfirmed-dxcc=%s band=%s receiver=%s path_probability=%d",
            callsign,
            band,
            spot.receiver or "-",
            path_probability,
        )
        return True

    def stop_hunt(self, reason: str = "user request") -> None:
        if self._hunt_active:
            LOGGER.info("[HUNT] stopped reason=%s", reason)
        self._hunt_active = False
        self._hunt_manual_target = ""
        self._hunt_targets = ()
        self._hunt_band_started_at = None
        self._hunt_observed_band = None
        self._hunt_rotation_index = 0
        self.engine.clear_candidates()

    def hunt_snapshot(self) -> dict[str, object]:
        return {
            "active": self._hunt_active,
            "manual_target": self._hunt_manual_target,
            "targets": [target.callsign for target in self._hunt_targets],
            "current_target": self._hunt_current_target().callsign if self._hunt_current_target() is not None else None,
            "remote_guidance": self._remote_hunt_enabled,
        }

    def _hunt_accepts(self, station: str) -> bool:
        normalized = station.strip().upper()
        return any(target.callsign == normalized for target in self._hunt_targets)

    def _rare_target_accepts(self, station: str) -> bool:
        normalized = station.strip().upper()
        return any(target.callsign == normalized for target in (*self._hunt_targets, *self._reply_rare_targets))

    def _hunt_current_target(self) -> HuntTarget | None:
        if not self._hunt_targets:
            return None
        if self._hunt_manual_target:
            return self._hunt_targets[0]
        return self._hunt_targets[self._hunt_rotation_index % len(self._hunt_targets)]

    def _run_hunt(self, now: datetime) -> None:
        target = self._hunt_current_target()
        if target is None:
            self.stop_hunt("no usable target")
            return
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if current_band != self._hunt_observed_band:
            self._hunt_observed_band = current_band
            self._hunt_band_started_at = now
        spot = self._hunt_remote_spots.get(target.callsign)
        spot_is_recent = bool(
            spot is not None
            and now - spot.observed_at <= timedelta(seconds=self.config.pskreporter_mqtt_spot_max_age_seconds)
        )
        allowed = set(self.config.allowed_auto_hop_bands)
        available_bands = tuple(
            dict.fromkeys(
                band
                for band in (*target.bands, spot.band if spot_is_recent and spot is not None else "")
                if band in allowed and band not in self._swr_excluded_bands and self._band_frequency(band, "FT8") is not None
            )
        )
        remote_band = spot.band if spot_is_recent and spot is not None and spot.band in available_bands else None
        desired_band = remote_band or (target.best_band if target.best_band in available_bands else (available_bands[0] if available_bands else None))
        if desired_band is None:
            self._advance_hunt_target(now, "target band unavailable")
            return
        frequency = self._remote_spot_frequency(spot, desired_band) if remote_band else self._band_frequency(desired_band, "FT8")
        if frequency is None:
            self._advance_hunt_target(now, "no FT8 frequency")
            return
        if current_band != desired_band or self.status.dial_frequency != frequency:
            if not self._forced_band_hop_safe():
                return
            if self._request_band_change(desired_band, frequency, now, "hunt"):
                source = "remote PSK Reporter spot" if remote_band else "activity schedule"
                LOGGER.info("[HUNT] target=%s changing band %s -> %s source=%s", target.callsign, current_band or "unknown", desired_band, source)
                self._hunt_band_started_at = now
            return
        if self._hunt_manual_target or len(self._hunt_targets) < 2:
            return
        dwell_started = self._hunt_band_started_at or now
        if now - dwell_started >= timedelta(seconds=self.config.adaptive_opportunity_quiet_seconds):
            self._advance_hunt_target(now, "listen window expired")

    def _run_reply_rare_watch(self, now: datetime) -> None:
        """Follow the freshest rare FT8 spot while ordinary Reply remains active.

        A remote spot can only select an RX band; candidate creation and every
        transmission still require a local WSJT-X Decode.
        """
        if not self._remote_hunt_enabled or not self._reply_rare_targets:
            return
        targets = {target.callsign: target for target in self._reply_rare_targets}
        candidates = [
            (spot, targets.get(callsign))
            for callsign, spot in self._hunt_remote_spots.items()
            if callsign in targets
            and now - spot.observed_at <= timedelta(seconds=self.config.pskreporter_mqtt_spot_max_age_seconds)
            and spot.band not in self._swr_excluded_bands
            and spot.band in self.config.allowed_auto_hop_bands
            and self._band_frequency(spot.band, "FT8") is not None
        ]
        if not candidates:
            return
        spot, target = max(candidates, key=lambda item: (item[0].observed_at, item[1].path_score if item[1] else 0))
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        frequency = self._remote_spot_frequency(spot, spot.band)
        if frequency is None or (current_band == spot.band and self.status.dial_frequency == frequency) or not self._forced_band_hop_safe():
            return
        if self._request_band_change(spot.band, frequency, now, "reply rare MQTT"):
            LOGGER.info(
                "[REPLY] rare=%s changing band %s -> %s from remote PSK Reporter spot",
                target.callsign,
                current_band or "unknown",
                spot.band,
            )

    def _run_unconfirmed_dxcc_watch(self, now: datetime) -> None:
        """Follow one fresh MQTT lead for an entity missing from LoTW.

        This is receiver guidance only.  The scorer still requires a local
        WSJT-X Decode before it can select a station or request any TX.
        """
        max_age = timedelta(seconds=self.config.pskreporter_mqtt_spot_max_age_seconds)
        eligible = {
            callsign: spot
            for callsign, spot in self._unconfirmed_dxcc_remote_spots.items()
            if now - spot.observed_at <= max_age
            and spot.band not in self._swr_excluded_bands
            and spot.band in self.config.allowed_auto_hop_bands
            and self._band_frequency(spot.band, "FT8") is not None
        }
        self._unconfirmed_dxcc_remote_spots = eligible
        if not eligible or not self._forced_band_hop_safe():
            return
        dwell = timedelta(seconds=self.config.adaptive_opportunity_quiet_seconds)
        current_spot = eligible.get(self._unconfirmed_dxcc_target)
        if current_spot is not None and self._unconfirmed_dxcc_target_started_at is not None:
            if now - self._unconfirmed_dxcc_target_started_at < dwell:
                return
        callsign, spot = max(
            eligible.items(),
            key=lambda item: (item[1].path_probability, item[1].observed_at, item[1].snr if item[1].snr is not None else -99, item[0]),
        )
        frequency = self._remote_spot_frequency(spot, spot.band)
        if frequency is None:
            return
        current_band = BandResolver().resolve(self.status.dial_frequency) if self.status.dial_frequency else None
        if current_band != spot.band or self.status.dial_frequency != frequency:
            if not self._request_band_change(spot.band, frequency, now, "reply unconfirmed DXCC MQTT"):
                return
            LOGGER.info(
                "[REPLY] unconfirmed-dxcc=%s changing band %s -> %s from remote PSK Reporter spot",
                callsign,
                current_band or "unknown",
                spot.band,
            )
        self._unconfirmed_dxcc_target = callsign
        self._unconfirmed_dxcc_target_started_at = now

    def _advance_hunt_target(self, now: datetime, reason: str) -> None:
        if not self._hunt_targets:
            return
        previous = self._hunt_current_target()
        self._hunt_rotation_index = (self._hunt_rotation_index + 1) % len(self._hunt_targets)
        self._hunt_band_started_at = now
        target = self._hunt_current_target()
        LOGGER.info(
            "[HUNT] next target=%s previous=%s reason=%s",
            target.callsign if target else "-",
            previous.callsign if previous else "-",
            reason,
        )
