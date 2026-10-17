"""Hamlib rigctld integration for FTX-1 meters and vendor CAT commands."""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable, Protocol


LOGGER = logging.getLogger(__name__)
DEFAULT_HAMLIB_MODEL = 1051
FTX1_CAT_ID = "ID0840"
GENERIC_RADIO_NAME = "Hamlib generic"
FTX1_RADIO_NAME = "Yaesu FTX-1"


class VendorCatController(Protocol):
    def close(self) -> None: ...
    def identify(self) -> bool: ...
    def set_usb_mod_gain(self, gain: int) -> bool: ...
    def read_usb_mod_gain(self) -> int | None: ...
    def read_configured_power(self) -> int | None: ...


def discover_rigctld(configured_path: str = "") -> Path | None:
    """Return an explicit, PATH, or standard Windows Hamlib rigctld executable."""
    if configured_path:
        candidate = Path(configured_path).expanduser()
        return candidate if candidate.is_file() else None
    found = shutil.which("rigctld")
    if found:
        return Path(found)
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    candidates: list[Path] = []
    for root in roots:
        if root:
            candidates.extend(Path(root).glob("hamlib-w64-*/bin/rigctld.exe"))
    return sorted(candidates, reverse=True)[0] if candidates else None


def list_supported_radios(configured_path: str = "") -> list[tuple[int, str, str]]:
    """Read the radio catalogue from the installed Hamlib version."""
    executable = discover_rigctld(configured_path)
    if executable is None:
        return []
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [str(executable), "-l"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("[HAMLIB] radio catalogue unavailable: %s", exc)
        return []

    radios: list[tuple[int, str, str]] = []
    for line in result.stdout.splitlines():
        columns = re.split(r"\s{2,}", line.strip())
        if len(columns) < 3 or not columns[0].isdigit():
            continue
        model_number, manufacturer, model_name = int(columns[0]), columns[1], columns[2]
        if manufacturer.casefold() == "hamlib":
            continue
        radios.append((model_number, manufacturer, model_name))
    return sorted(radios, key=lambda radio: (radio[1].casefold(), radio[2].casefold(), radio[0]))


class HamlibFtx1Controller:
    """Own one rigctld process so Hamlib is the sole owner of the CAT-2 port."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        timeout_seconds: float,
        rigctld_path: str = "",
        tcp_port: int = 4533,
        vendor_cat: VendorCatController | None = None,
        process_factory: Callable[..., subprocess.Popen[bytes]] | None = None,
        rig_model: int = DEFAULT_HAMLIB_MODEL,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_seconds = timeout_seconds
        self.rigctld_path = rigctld_path
        self.tcp_port = tcp_port
        self.vendor_cat = vendor_cat
        self.rig_model = int(rig_model)
        self._process_factory = process_factory or subprocess.Popen
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._identified = False
        self.detected_radio_model = GENERIC_RADIO_NAME
        self.detected_cat_id: str | None = None
        self._ftx1_tuner_prefix: str | None = None
        self.last_identification_response: str | None = None
        self.last_frequency_response: str | None = None
        self.last_configured_power_response: str | None = None

    @property
    def is_ready(self) -> bool:
        return self._identified and self._process is not None and self._process.poll() is None and self._socket is not None

    def start(self) -> bool:
        if self._process is not None and self._process.poll() is None and self._socket is not None:
            return True
        self.close()
        executable = discover_rigctld(self.rigctld_path)
        if executable is None:
            LOGGER.error("[HAMLIB] rigctld not found configured=%s", self.rigctld_path or "auto")
            return False
        if not self.port:
            LOGGER.error("[HAMLIB] CAT-2 port is not configured")
            return False
        command = [
            str(executable),
            "-m", str(self.rig_model),
            "-r", self.port,
            "-s", str(self.baudrate),
            "-T", "127.0.0.1",
            "-t", str(self.tcp_port),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = self._process_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            LOGGER.error("[HAMLIB] rigctld start failed: %s", exc)
            self.close()
            return False
        deadline = time.monotonic() + max(1.0, self.timeout_seconds * 8)
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                LOGGER.error("[HAMLIB] rigctld exited during startup code=%s", self._process.returncode)
                self.close()
                return False
            try:
                connection = socket.create_connection(("127.0.0.1", self.tcp_port), timeout=self.timeout_seconds)
                connection.settimeout(self.timeout_seconds)
                self._socket = connection
                LOGGER.info(
                    "[HAMLIB] rigctld ready model=%d port=%s baud=%d tcp=127.0.0.1:%d",
                    self.rig_model,
                    self.port,
                    self.baudrate,
                    self.tcp_port,
                )
                return True
            except OSError:
                time.sleep(0.05)
        LOGGER.error("[HAMLIB] rigctld startup timeout port=%s", self.port)
        self.close()
        return False

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self._identified = False
        self.detected_radio_model = GENERIC_RADIO_NAME
        self.detected_cat_id = None
        self._ftx1_tuner_prefix = None

    def identify(self) -> bool:
        if self._identified:
            return True
        # A valid frequency read verifies both rigctld and the selected radio
        # backend without assuming a Yaesu-specific identification string.
        response = self._command("f")
        self.last_frequency_response = response
        try:
            identified = response is not None and float(response) > 0
        except ValueError:
            identified = False
        if identified:
            self._identified = True
            self._detect_radio_model()
            LOGGER.info(
                "[HAMLIB] radio identified backend_model=%d detected=%s cat_id=%r frequency=%s",
                self.rig_model,
                self.detected_radio_model,
                self.detected_cat_id,
                response,
            )
            return True
        LOGGER.error("[HAMLIB] radio identification failed model=%d response=%r", self.rig_model, response)
        self.close()
        return False

    @property
    def is_ftx1(self) -> bool:
        """True only after the connected radio answered with the FTX-1 CAT ID."""
        return self.detected_radio_model == FTX1_RADIO_NAME

    def _detect_radio_model(self) -> None:
        """Select vendor commands from a real CAT identity, never from config alone."""
        self.detected_radio_model = GENERIC_RADIO_NAME
        self.detected_cat_id = None
        self.last_identification_response = None

        # Hamlib obtains the rig information through its selected backend.  No
        # manufacturer command is sent here, so this probe is safe for every
        # radio family supported by Hamlib.
        response = self._command("\\get_info")
        self.last_identification_response = response
        normalized = "".join((response or "").upper().split()).rstrip(";")
        if normalized == FTX1_CAT_ID:
            self.detected_cat_id = FTX1_CAT_ID
            self.detected_radio_model = FTX1_RADIO_NAME
            self._read_ftx1_tuner_prefix()
            return

        LOGGER.info(
            "[HAMLIB] no specialized command driver for radio_info=%r; generic commands selected",
            response,
        )

    def _read_ftx1_tuner_prefix(self) -> str | None:
        """Read and preserve the installed FTX-1 tuner path (internal/external/ATAS)."""
        response = self._command("\\send_raw ; AC;")
        match = re.fullmatch(r"AC([01])([02])[0-3];", response or "")
        if match is None:
            self._ftx1_tuner_prefix = None
            LOGGER.error("[HAMLIB] FTX-1 tuner path detection failed response=%r", response)
            return None
        self._ftx1_tuner_prefix = "".join(match.groups())
        LOGGER.info(
            "[HAMLIB] FTX-1 tuner path detected prefix=%s response=%s",
            self._ftx1_tuner_prefix,
            response,
        )
        return self._ftx1_tuner_prefix

    def _send_ftx1_tuner_state(self, state: int) -> bool:
        if state not in {0, 1, 3}:
            raise ValueError("invalid FTX-1 tuner state")
        prefix = self._ftx1_tuner_prefix or self._read_ftx1_tuner_prefix()
        if prefix is None:
            return False
        # Set commands do not return CAT data. Hamlib's zero-byte send_raw
        # transport sends immediately instead of waiting for a serial reply.
        response = self._command(f"\\send_raw 0 AC{prefix}{state};")
        accepted = response in {"No answer", "RPRT 0"}
        if not accepted:
            LOGGER.error(
                "[HAMLIB] FTX-1 tuner command rejected prefix=%s state=%d response=%r",
                prefix,
                state,
                response,
            )
        return accepted

    def set_usb_mod_gain(self, gain: int) -> bool:
        if not 0 <= gain <= 100:
            raise ValueError("FTX-1 USB MOD GAIN must be between 0 and 100")
        return bool(self._with_vendor(lambda vendor: vendor.set_usb_mod_gain(gain)))

    def read_usb_mod_gain(self) -> int | None:
        result = self._with_vendor(lambda vendor: vendor.read_usb_mod_gain())
        return result if isinstance(result, int) and not isinstance(result, bool) else None

    def read_alc(self) -> float | None:
        return self._read_level("ALC")

    def read_swr(self) -> float | None:
        return self._read_level("SWR")

    def read_power(self) -> float | None:
        return self._read_level("RFPOWER_METER_WATTS")

    def read_configured_power(self) -> int | None:
        result = self._with_vendor(lambda vendor: vendor.read_configured_power())
        return result if isinstance(result, int) and not isinstance(result, bool) else None

    def read_rf_power_fraction(self) -> float | None:
        """Read Hamlib's configured RFPOWER value (0.05..1.00 on the FTX-1)."""
        return self._read_level("RFPOWER")

    def read_requested_power_watts(self, frequency_hz: int, mode: str = "USB") -> float | None:
        """Convert Hamlib's relative RFPOWER setting to watts for this band/mode."""
        fraction = self.read_rf_power_fraction()
        if fraction is None or frequency_hz <= 0:
            return None
        hamlib_mode = "USB" if mode.upper() in {"FT8", "FT4", "JT65", "JT9", "Q65"} else mode.upper()
        response = self._command(f"2 {fraction:.6f} {frequency_hz} {hamlib_mode}")
        if response is None:
            return None
        try:
            milliwatts = float(response)
        except ValueError:
            LOGGER.warning("[HAMLIB] invalid power2mW response: %r", response)
            return None
        return milliwatts / 1000.0 if milliwatts >= 0 else None

    def read_frequency(self) -> int | None:
        """Read the radio dial frequency in hertz through Hamlib."""
        if not self.identify():
            return None
        response = self._command("f")
        if response is None:
            return None
        try:
            return int(round(float(response)))
        except ValueError:
            LOGGER.warning("[HAMLIB] invalid frequency response: %r", response)
            return None

    def set_frequency(self, frequency_hz: int) -> bool:
        if frequency_hz <= 0:
            raise ValueError("radio frequency must be positive")
        return self._command_ok(f"F {frequency_hz}")

    def stop_transmit(self) -> bool:
        """Force the hardware PTT to RX; WSJT-X must also cancel its TX state."""
        return self._command_ok("T 0")

    def read_ptt(self) -> bool | None:
        if not self.identify():
            return None
        response = self._command("t")
        if response not in {"0", "1"}:
            return None
        return response == "1"

    def set_rf_power_fraction(self, value: float) -> bool:
        if not 0.05 <= value <= 1.0:
            raise ValueError("FTX-1 RF power must be between 0.05 and 1.0")
        return self._command_ok(f"L RFPOWER {value:.3f}")

    def set_tuner_enabled(self, enabled: bool) -> bool:
        if not self.identify():
            return False
        if self.is_ftx1:
            return self._send_ftx1_tuner_state(1 if enabled else 0)
        return self._command_ok(f"U TUNER {1 if enabled else 0}")

    def read_tuner_enabled(self) -> bool | None:
        if not self.identify():
            return None
        response = self._command("u TUNER")
        if response not in {"0", "1"}:
            return None
        return response == "1"

    def start_tune(self) -> bool:
        """Start a real tuner cycle through Hamlib."""
        if not self.identify():
            return False
        if self.is_ftx1:
            return self._send_ftx1_tuner_state(3)
        return self._command_ok("G TUNE")

    def _with_vendor(self, action: Callable[[VendorCatController], object]) -> object | None:
        if self.vendor_cat is None:
            LOGGER.error("[HAMLIB] vendor CAT command unavailable")
            return None
        self.close()
        try:
            result = action(self.vendor_cat)
        finally:
            self.vendor_cat.close()
        if not self.identify():
            LOGGER.error("[HAMLIB] meter service did not recover after vendor CAT command")
        return result

    def _read_level(self, name: str) -> float | None:
        if not self.identify():
            return None
        response = self._command(f"l {name}")
        if response is None:
            return None
        try:
            return float(response)
        except ValueError:
            LOGGER.warning("[HAMLIB] invalid %s meter response: %r", name, response)
            return None

    def _command_ok(self, command: str) -> bool:
        if not self.identify():
            return False
        response = self._command(command)
        return response == "RPRT 0"

    def _command(self, command: str) -> str | None:
        if not self.start() or self._socket is None:
            return None
        try:
            self._socket.sendall(command.encode("ascii") + b"\n")
            payload = bytearray()
            while b"\n" not in payload:
                chunk = self._socket.recv(512)
                if not chunk:
                    raise OSError("rigctld closed the connection")
                payload.extend(chunk)
            response = bytes(payload).split(b"\n", 1)[0].decode("ascii", errors="replace").strip()
        except OSError as exc:
            LOGGER.error("[HAMLIB] command failed command=%s error=%s", command.split(" ", 1)[0], exc)
            self.close()
            return None
        if response.startswith("RPRT -"):
            LOGGER.error("[HAMLIB] command rejected command=%s response=%s", command.split(" ", 1)[0], response)
            return None
        return response
