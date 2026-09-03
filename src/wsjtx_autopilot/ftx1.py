"""FTX-1 CAT-2 manual band-drive profiles."""

import logging
import re
from dataclasses import dataclass
from typing import Callable, Protocol

try:
    import serial
except ImportError:  # pragma: no cover - exercised by packaged dependency checks
    serial = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
FTX1_BANDS = ("160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m")


@dataclass(frozen=True, slots=True)
class FTX1BandDriveProfile:
    band: str
    usb_mod_gain: int
    tx_audio_attenuation: int

    def to_settings(self) -> dict[str, int]:
        return {"usb_mod_gain": self.usb_mod_gain, "tx_audio_attenuation": self.tx_audio_attenuation}


class FTX1Serial(Protocol):
    def close(self) -> None: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def read_until(self, expected: bytes = b";") -> bytes: ...


class FTX1CatController:
    """Owns only the FTX-1 Standard COM/CAT-2 serial connection."""

    def __init__(self, port: str, baudrate: int, timeout_seconds: float, serial_factory: Callable[[], FTX1Serial] | None = None) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_seconds = timeout_seconds
        self._serial_factory = serial_factory
        self._connection: FTX1Serial | None = None
        self._identified = False
        self.last_identification_response: str | None = None
        self.last_configured_power_response: str | None = None

    @staticmethod
    def usb_mod_gain_command(gain: int) -> bytes:
        if not 0 <= gain <= 100:
            raise ValueError("FTX-1 USB MOD GAIN must be between 0 and 100")
        return f"EX010414{gain:03d};".encode("ascii")

    @property
    def is_ready(self) -> bool:
        return self._connection is not None and self._identified

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
        self._connection = None
        self._identified = False

    def identify(self) -> bool:
        if self._identified:
            return True
        response = self._query("ID;")
        self.last_identification_response = response
        if response is not None and "ID0840;" in response:
            self._identified = True
            LOGGER.info("[FTX1] FTX-1 identified")
            return True
        LOGGER.error("[FTX1] CAT-2 identification failed: %r", response)
        self.close()
        return False

    def set_usb_mod_gain(self, gain: int) -> bool:
        if not self.identify() or not self._write(self.usb_mod_gain_command(gain)):
            return False
        response = self._query("EX010414;")
        observed = self._parse_meter_value(response, "EX010414") if response is not None else None
        if observed is None:
            LOGGER.warning("[FTX1] USB MOD GAIN sent but CAT-2 confirmation unavailable")
            return True
        if observed != gain:
            LOGGER.error("[FTX1] USB MOD GAIN update failed: expected=%d observed=%d", gain, observed)
            return False
        LOGGER.info("[FTX1] USB MOD GAIN confirmed: %d", gain)
        return True

    def read_usb_mod_gain(self) -> int | None:
        if not self.identify():
            return None
        value = self._parse_meter_value(self._query("EX010414;"), "EX010414")
        if value is None:
            LOGGER.warning("[FTX1] Current USB MOD GAIN unavailable")
        else:
            LOGGER.info("[FTX1] Current USB MOD GAIN=%d", value)
        return value

    def read_alc(self) -> int | None:
        # Diagnostic only; never used for automatic drive control.
        return self._parse_meter_value(self._query("RM4;") if self.identify() else None, "RM4")

    def read_power(self) -> int | None:
        # Diagnostic only; never used for automatic drive control.
        return self._parse_meter_value(self._query("RM5;") if self.identify() else None, "RM5")

    def read_configured_power(self) -> int | None:
        if not self.identify():
            return None
        response = self._query("PC;")
        self.last_configured_power_response = response
        return self._parse_configured_power(response)

    def _query(self, command: str) -> str | None:
        if not self._write(command.encode("ascii")) or self._connection is None:
            return None
        try:
            response = self._connection.read_until(b";").decode("ascii", errors="replace")
        except OSError as exc:
            LOGGER.error("[FTX1] CAT-2 read failed: %s", exc)
            self.close()
            return None
        if not response:
            LOGGER.warning("[FTX1] CAT-2 timeout")
            self.close()
            return None
        return response

    def _write(self, command: bytes) -> bool:
        if not self._connect() or self._connection is None:
            return False
        try:
            self._connection.write(command)
            self._connection.flush()
            return True
        except OSError as exc:
            LOGGER.error("[FTX1] CAT-2 write failed: %s", exc)
            self.close()
            return False

    def _connect(self) -> bool:
        if self._connection is not None:
            return True
        if not self.port:
            LOGGER.error("[FTX1] CAT-2 open failed: no port configured")
            return False
        try:
            if self._serial_factory is not None:
                self._connection = self._serial_factory()
            elif serial is not None:
                connection = serial.Serial()
                connection.port = self.port
                connection.baudrate = self.baudrate
                connection.timeout = self.timeout_seconds
                connection.write_timeout = self.timeout_seconds
                connection.rtscts = connection.dsrdtr = connection.rts = connection.dtr = False
                connection.open()
                self._connection = connection
            else:
                raise OSError("pyserial is unavailable")
        except (OSError, ValueError) as exc:
            LOGGER.error("[FTX1] CAT-2 open failed: %s", exc)
            self.close()
            return False
        return True

    @staticmethod
    def _parse_meter_value(response: str | None, prefix: str) -> int | None:
        if response is None:
            return None
        match = re.search(re.escape(prefix) + r"[^0-9]*(\d{1,3})\d*;", response)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _parse_configured_power(response: str | None) -> int | None:
        match = re.search(r"PC([12])(\d{3});", response or "")
        if match is None:
            return None
        head, value = match.groups()
        power = int(value)
        return power if (head == "1" and 5 <= power <= 10) or (head == "2" and 5 <= power <= 100) else None


class FTX1BandDriveController:
    """Applies explicit user-saved USB gain and WSJT-X attenuation pairs by band."""

    def __init__(
        self,
        cat: FTX1CatController,
        profiles: dict[str, dict[str, int]] | None = None,
        changed: Callable[[dict[str, dict[str, int]]], None] | None = None,
        set_tx_audio_attenuation: Callable[[int], bool] | None = None,
        is_transmitting: Callable[[], bool] | None = None,
    ) -> None:
        self.cat = cat
        self.profiles = self._load_profiles(profiles or {})
        self._changed = changed
        self._set_tx_audio_attenuation = set_tx_audio_attenuation
        self._is_transmitting = is_transmitting or (lambda: False)
        self.current_usb_mod_gain: int | None = None

    def close(self) -> None:
        self.cat.close()

    def save_current_profile(self, band: str, attenuation: int) -> bool:
        if self._is_transmitting():
            LOGGER.error("[FTX1] Save manual band profile failed: radio is transmitting")
            return False
        gain = self.cat.read_usb_mod_gain()
        if gain is None:
            LOGGER.error("[FTX1] Save manual band profile failed: current USB MOD GAIN unavailable")
            return False
        self.current_usb_mod_gain = gain
        self.save_profile(band, gain, attenuation)
        return True

    def save_profile(self, band: str, gain: int, attenuation: int) -> None:
        profile = self._validate_profile(band, gain, attenuation)
        self.profiles[profile.band] = profile
        self._notify()
        LOGGER.info("[FTX1] Saved manual band profile %s USB_GAIN=%d ATT=%d", profile.band, gain, attenuation)

    def apply_profile(self, band: str, current_attenuation: int | None) -> bool:
        profile = self.profiles.get(band.lower())
        if profile is None:
            LOGGER.info("[FTX1] No saved drive profile for %s; keeping current settings", band)
            return False
        if self._is_transmitting():
            LOGGER.warning("[FTX1] Manual band profile deferred: radio is transmitting")
            return False
        if not self.cat.set_usb_mod_gain(profile.usb_mod_gain):
            LOGGER.error("[FTX1] Manual band profile apply failed: USB MOD GAIN")
            return False
        self.current_usb_mod_gain = profile.usb_mod_gain
        if current_attenuation != profile.tx_audio_attenuation:
            if self._set_tx_audio_attenuation is None or not self._set_tx_audio_attenuation(profile.tx_audio_attenuation):
                LOGGER.error("[FTX1] Manual band profile apply failed: WSJT-X attenuation")
                return False
        LOGGER.info("[FTX1] Loaded manual band profile %s USB_GAIN=%d ATT=%d", profile.band, profile.usb_mod_gain, profile.tx_audio_attenuation)
        return True

    def delete_profile(self, band: str) -> bool:
        if self.profiles.pop(band.lower(), None) is None:
            return False
        self._notify()
        LOGGER.info("[FTX1] Deleted manual band profile %s", band.lower())
        return True

    def reset_profiles(self) -> None:
        if self.profiles:
            self.profiles.clear()
            self._notify()
        LOGGER.info("[FTX1] Reset all manual band profiles")

    def snapshot(self, current_band: str | None, current_attenuation: int | None) -> dict[str, object]:
        profile = self.profiles.get((current_band or "").lower())
        return {
            "connected": self.cat.is_ready,
            "band": current_band,
            "current_usb_mod_gain": self.current_usb_mod_gain,
            "current_tx_audio_attenuation": current_attenuation,
            "saved_usb_mod_gain": profile.usb_mod_gain if profile else None,
            "saved_tx_audio_attenuation": profile.tx_audio_attenuation if profile else None,
            "profile_count": len(self.profiles),
        }

    @staticmethod
    def _validate_profile(band: str, gain: int, attenuation: int) -> FTX1BandDriveProfile:
        normalized_band = band.lower()
        if normalized_band not in FTX1_BANDS:
            raise ValueError(f"unsupported FTX-1 band: {band}")
        if not 0 <= gain <= 100 or not 0 <= attenuation <= 450:
            raise ValueError("FTX-1 profile values are out of range")
        return FTX1BandDriveProfile(normalized_band, gain, attenuation)

    @classmethod
    def _load_profiles(cls, values: dict[str, dict[str, int]]) -> dict[str, FTX1BandDriveProfile]:
        profiles: dict[str, FTX1BandDriveProfile] = {}
        for band, value in values.items():
            if not isinstance(value, dict):
                continue
            try:
                profiles[band.lower()] = cls._validate_profile(band, value["usb_mod_gain"], value["tx_audio_attenuation"])
            except (KeyError, TypeError, ValueError):
                continue
        return profiles

    def _notify(self) -> None:
        if self._changed is not None:
            self._changed({band: profile.to_settings() for band, profile in self.profiles.items()})
