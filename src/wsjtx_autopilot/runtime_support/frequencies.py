"""FT8 channel planning shared by Hunt, Reply and adaptive scanning."""

from __future__ import annotations

from wsjtx_autopilot.engine.adaptive import DEFAULT_BAND_PROFILES
from wsjtx_autopilot.worked.bands import BandResolver


# The normal FT8 watering holes are defined in DEFAULT_BAND_PROFILES.  These
# additional dial frequencies are the conventional DXpedition / secondary FT8
# channels shipped by WSJT-X.  They are deliberately a receive scan target;
# a normal QSO still has to originate from a local WSJT-X Decode.
_SECONDARY_FT8_DIAL_FREQUENCIES = {
    "80m": 3_567_000,
    "40m": 7_056_000,
    "30m": 10_131_000,
    "20m": 14_090_000,
    "17m": 18_095_000,
    "15m": 21_091_000,
    "12m": 24_911_000,
    "10m": 28_091_000,
    "6m": 50_323_000,
}


class FrequencyPlanMixin:
    def _remote_spot_frequency(self, spot: HuntRemoteSpot | None, band: str) -> int | None:
        """Pick the nearest configured FT8 scan channel for a remote spot.

        PSK Reporter may report any dial offset used by the transmitting
        station.  Remote information is guidance, not authority to retune the
        radio to an arbitrary frequency: Reply and Hunt use only the primary
        or secondary FT8 channels configured for that band.
        """
        channels = self._scan_frequencies(band, "FT8")
        if not channels:
            return None
        if spot is None or spot.frequency_hz is None:
            return channels[0]
        return min(channels, key=lambda frequency: abs(frequency - int(spot.frequency_hz)))

    def _next_auto_hop_band(self) -> str | None:
        destination = self._next_auto_hop_destination()
        return destination[0] if destination is not None else None

    def _next_auto_hop_destination(self) -> tuple[str, int] | None:
        """Return the next primary/secondary FT8 scan position.

        Each allowed band is visited first on its normal FT8 frequency, then
        on its secondary DX channel.  SWR-excluded bands are intentionally
        absent, so the scanner can never bring them back into service.
        """
        allowed = tuple(
            band for band in dict.fromkeys(band.lower() for band in self.config.allowed_auto_hop_bands)
            if band not in self._swr_excluded_bands
        )
        if not allowed or self.status.dial_frequency is None:
            return None
        current = (BandResolver().resolve(self.status.dial_frequency) or "").lower()
        positions = tuple(
            (band, frequency)
            for band in allowed
            for frequency in self._scan_frequencies(band, self.status.mode or "")
        )
        if not positions:
            return None
        for index, position in enumerate(positions):
            if position == (current, self.status.dial_frequency):
                return positions[(index + 1) % len(positions)]
        if current in allowed:
            return next((position for position in positions if position[0] == current), positions[0])
        return positions[0]

    def _scan_frequencies(self, band: str, mode: str) -> tuple[int, ...]:
        primary = self._band_frequency(band, mode)
        if primary is None:
            return ()
        frequencies = [primary]
        secondary = _SECONDARY_FT8_DIAL_FREQUENCIES.get(band.lower()) if mode.upper() == "FT8" else None
        if secondary is not None and secondary != primary and BandResolver().resolve(secondary) == band.lower():
            frequencies.append(secondary)
        return tuple(frequencies)

    def _band_frequency(self, band: str, mode: str) -> int | None:
        override = self.config.auto_hop_band_frequencies.get(band.lower(), {}).get(mode.upper())
        if override is not None:
            return override
        profile = next((item for item in DEFAULT_BAND_PROFILES if item.band.lower() == band.lower()), None)
        return profile.frequency_for(mode) if profile is not None else None
