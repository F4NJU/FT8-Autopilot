from datetime import datetime, timedelta, timezone
from pathlib import Path

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.ftx1 import FTX1BandDriveController, FTX1CatController
from wsjtx_autopilot.hamlib import HamlibFtx1Controller, list_supported_radios
from wsjtx_autopilot.runtime import AutopilotRuntime, PendingDialFrequency
from wsjtx_autopilot.wsjtx.models import DecodePacket, HeartbeatPacket, PacketHeader, StatusPacket, TxAudioAttenuationStatePacket
from wsjtx_autopilot.wsjtx.protocol import parse_datagram


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
ENDPOINT = ("127.0.0.1", 2237)


class FakeSerial:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def read_until(self, expected: bytes = b";") -> bytes:
        return self.responses.pop(0) if self.responses else b""

    def close(self) -> None:
        pass


class FakeCat:
    def __init__(self, gain: int | None = 35, *, alc: float = 0.05, swr: float = 1.2, power: float = 20.0) -> None:
        self.gain = gain
        self.alc = alc
        self.swr = swr
        self.power = power
        self.gains: list[int] = []
        self.alc_reads = 0
        self.power_reads = 0
        self.is_ready = True
        self.rf_power_fraction = 0.5
        self.tuner_enabled = False
        self.tune_started = False
        self.tune_start_count = 0
        self.restored_power: list[float] = []

    def set_usb_mod_gain(self, gain: int) -> bool:
        self.gain = gain
        self.gains.append(gain)
        return True

    def read_usb_mod_gain(self) -> int | None:
        return self.gain

    def read_alc(self) -> float:
        self.alc_reads += 1
        return self.alc

    def read_swr(self) -> float:
        return self.swr

    def read_power(self) -> float:
        self.power_reads += 1
        return self.power

    def read_rf_power_fraction(self) -> float:
        return self.rf_power_fraction

    def read_requested_power_watts(self, frequency_hz: int, mode: str = "USB") -> float:
        return self.rf_power_fraction * 100.0

    def set_rf_power_fraction(self, value: float) -> bool:
        self.rf_power_fraction = value
        self.restored_power.append(value)
        return True

    def set_tuner_enabled(self, enabled: bool) -> bool:
        self.tuner_enabled = enabled
        return True

    def start_tune(self) -> bool:
        self.tune_started = True
        self.tune_start_count += 1
        return True

    def close(self) -> None:
        pass


class FakeHamlibCat(FakeCat):
    def __init__(self) -> None:
        super().__init__()
        self.frequency = 14_074_000
        self.frequency_requests: list[int] = []
        self.ptt_stops = 0

    def set_frequency(self, frequency_hz: int) -> bool:
        self.frequency = frequency_hz
        self.frequency_requests.append(frequency_hz)
        return True

    def read_frequency(self) -> int:
        return self.frequency

    def stop_transmit(self) -> bool:
        self.ptt_stops += 1
        return True

    def read_ptt(self) -> bool:
        return False


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        self.sent.append((data, endpoint))
        return len(data)


def heartbeat() -> HeartbeatPacket:
    return HeartbeatPacket(PacketHeader(3, 1, "WSJT-X"), 3, "3.2.0", "260818-AP1")


def status(frequency: int, *, transmitting: bool = False) -> StatusPacket:
    return StatusPacket(PacketHeader(3, 1, "WSJT-X"), frequency, "FT8", "", "", "FT8", False, transmitting, False, 1000, 1000, "", "", "", False, "", False, 0, 0xFFFFFFFF, 15, "Default", "")


def test_band_profiles_never_touch_usb_mod_gain() -> None:
    cat = FakeCat(35)
    controller = FTX1BandDriveController(cat, set_tx_audio_attenuation=lambda _value: True)

    assert controller.save_current_profile("20m", 118)
    assert controller.apply_profile("20m", 0)
    assert cat.gains == []


def test_band_change_uses_hamlib_frequency_instead_of_wsjtx_ap1() -> None:
    cat = FakeHamlibCat()
    transport = FakeTransport()
    runtime = AutopilotRuntime(
        AppConfig(),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    transport.sent.clear()

    assert runtime._request_band_change("40m", 7_074_000, NOW, "adaptive")
    assert cat.frequency_requests == [7_074_000]
    assert transport.sent == []


def test_halt_tx_drops_hamlib_ptt_before_cancelling_wsjtx() -> None:
    cat = FakeHamlibCat()
    transport = FakeTransport()
    runtime = AutopilotRuntime(
        AppConfig(),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    transport.sent.clear()

    assert runtime._halt_tx("WSJT-X", "test")
    assert cat.ptt_stops == 1
    assert parse_datagram(transport.sent[-1][0]).header.packet_type == 8


def test_save_current_profile_captures_actual_usb_gain_and_attenuation() -> None:
    saved: list[dict[str, dict[str, int]]] = []
    controller = FTX1BandDriveController(FakeCat(35), changed=saved.append)

    assert controller.save_current_profile("20m", 118)
    assert controller.profiles["20m"].usb_mod_gain == 0
    assert controller.profiles["20m"].tx_audio_attenuation == 118
    assert saved[-1] == {"20m": {"tx_audio_attenuation": 118}}


def test_alc_learning_creates_a_profile_without_usb_gain_in_settings() -> None:
    saved: list[dict[str, dict[str, int]]] = []
    controller = FTX1BandDriveController(FakeCat(None), changed=saved.append)

    controller.update_profile_attenuation("20m", 118)

    assert controller.profiles["20m"].tx_audio_attenuation == 118
    assert saved[-1] == {"20m": {"tx_audio_attenuation": 118}}


def test_save_without_attenuation_queries_ap1_then_saves_type_23_value() -> None:
    cat = FakeCat(35)
    controller = FTX1BandDriveController(cat)
    transport = FakeTransport()
    runtime = AutopilotRuntime(AppConfig(), control=WsjtxUdpControl(transport, 15, None, armed=True), ftx1_band_drive=controller)
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    transport.sent.clear()

    assert runtime.save_ftx1_current_band_profile()
    assert parse_datagram(transport.sent[-1][0]).header.packet_type == 22
    runtime.handle(TxAudioAttenuationStatePacket(PacketHeader(3, 1, "WSJT-X"), 118), NOW, ENDPOINT)

    assert controller.profiles["20m"].usb_mod_gain == 0
    assert controller.profiles["20m"].tx_audio_attenuation == 118


def test_profile_update_preserves_legacy_usb_value_without_reading_radio() -> None:
    controller = FTX1BandDriveController(FakeCat(None), {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}})

    assert controller.save_current_profile("20m", 120)
    assert controller.profiles["20m"].usb_mod_gain == 35
    assert controller.profiles["20m"].tx_audio_attenuation == 120


def test_profiles_are_separate_by_band_and_can_be_deleted_or_reset() -> None:
    controller = FTX1BandDriveController(FakeCat())
    controller.save_profile("20m", 35, 118)
    controller.save_profile("40m", 32, 145)

    assert controller.profiles["20m"].usb_mod_gain == 35
    assert controller.profiles["40m"].tx_audio_attenuation == 145
    assert controller.delete_profile("20m")
    assert "20m" not in controller.profiles
    controller.reset_profiles()
    assert controller.profiles == {}


def test_band_hop_applies_saved_profile_only_when_auto_apply_is_on() -> None:
    cat = FakeCat(35)
    transport = FakeTransport()
    controller = FTX1BandDriveController(cat)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_auto_apply_band_profiles=True),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=controller,
    )
    controller.save_profile("40m", 32, 145)
    runtime.current_tx_audio_attenuation = 118
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime._pending_dial_frequency = PendingDialFrequency("WSJT-X", 7_074_000, "40m", NOW + timedelta(seconds=5), "20m")

    runtime.handle(status(7_074_000), NOW, ENDPOINT)

    assert cat.gains == []
    assert parse_datagram(transport.sent[-1][0]).header.packet_type == 21


def test_band_hop_keeps_settings_without_profile_or_with_auto_apply_off() -> None:
    for auto_apply, profiles in ((True, {}), (False, {"40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145}})):
        cat = FakeCat(35)
        transport = FakeTransport()
        runtime = AutopilotRuntime(
            AppConfig(ftx1_auto_apply_band_profiles=auto_apply),
            control=WsjtxUdpControl(transport, 15, None, armed=True),
            ftx1_band_drive=FTX1BandDriveController(cat, profiles),
        )
        runtime.current_tx_audio_attenuation = 118
        runtime.handle(heartbeat(), NOW, ENDPOINT)
        runtime._pending_dial_frequency = PendingDialFrequency("WSJT-X", 7_074_000, "40m", NOW + timedelta(seconds=5), "20m")
        runtime.handle(status(7_074_000), NOW, ENDPOINT)

        assert cat.gains == []
        assert not any(parse_datagram(data).header.packet_type == 21 for data, _ in transport.sent)


def test_profile_never_writes_during_tx_and_ap1_confirmation_is_tracked() -> None:
    cat = FakeCat()
    transmitting = True
    controller = FTX1BandDriveController(cat, {"40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145}}, is_transmitting=lambda: transmitting)

    assert not controller.apply_profile("40m", 118)
    assert cat.gains == []

    transport = FakeTransport()
    runtime = AutopilotRuntime(AppConfig(), control=WsjtxUdpControl(transport, 15, None, armed=True))
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    assert runtime.set_tx_audio_attenuation_confirmed(145)
    runtime.handle(TxAudioAttenuationStatePacket(PacketHeader(3, 1, "WSJT-X"), 145), NOW, ENDPOINT)
    assert runtime.pending_tx_audio_attenuation is None


def test_manual_attenuation_and_rm_diagnostics_never_trigger_correction() -> None:
    cat = FakeCat(35)
    controller = FTX1BandDriveController(cat, {"20m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145}})
    runtime = AutopilotRuntime(AppConfig(), ftx1_band_drive=controller)
    runtime.current_tx_audio_attenuation = 118
    runtime._observe_tx_audio_attenuation(120, NOW)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=2), ENDPOINT)

    assert cat.gains == []
    assert cat.alc_reads == 0
    assert cat.power_reads == 0


def test_hamlib_alc_regulation_increases_and_decreases_drive_after_tx() -> None:
    for measured_alc, measured_power, expected in (
        (0.35, 20.0, 103),
        (0.0, 20.0, 97),
        (0.15, 0.0, 88),
        (0.0, 0.0, 88),
    ):
        cat = FakeCat(35, alc=measured_alc, swr=1.2, power=measured_power)
        transport = FakeTransport()
        controller = FTX1BandDriveController(cat, {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 100}})
        runtime = AutopilotRuntime(
            AppConfig(
                ftx1_hamlib_enabled=True,
                ftx1_meter_poll_seconds=0.2,
                ftx1_alc_minimum=0.02,
                ftx1_alc_maximum=0.08,
                ftx1_alc_attenuation_step=3,
            ),
            control=WsjtxUdpControl(transport, 15, None, armed=True),
            ftx1_band_drive=controller,
        )
        runtime.current_tx_audio_attenuation = 100
        runtime.handle(heartbeat(), NOW, ENDPOINT)
        runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
        runtime.handle(None, NOW + timedelta(seconds=2), ENDPOINT)
        runtime.handle(None, NOW + timedelta(seconds=2.3), ENDPOINT)
        transport.sent.clear()

        runtime.handle(status(14_074_000, transmitting=False), NOW + timedelta(seconds=3), ENDPOINT)

        attenuation_packets = [data for data, _ in transport.sent if parse_datagram(data).header.packet_type == 21]
        assert int.from_bytes(attenuation_packets[-1][-2:], "big") == expected
        runtime.handle(TxAudioAttenuationStatePacket(PacketHeader(3, 1, "WSJT-X"), expected), NOW + timedelta(seconds=3.1), ENDPOINT)
        assert controller.profiles["20m"].tx_audio_attenuation == expected


def test_hamlib_drive_keeps_current_setting_when_po_matches_requested_power() -> None:
    cat = FakeCat(35, alc=0.266, swr=1.0, power=46.5)
    transport = FakeTransport()
    controller = FTX1BandDriveController(cat)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_alc_minimum=0.23, ftx1_alc_maximum=0.30),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=controller,
    )
    runtime.current_tx_audio_attenuation = 32
    runtime.status.dial_frequency = 14_074_000
    runtime._hamlib_alc_samples[:] = [0.266, 0.266, 0.281]
    runtime._hamlib_power_samples[:] = [46.2, 46.5, 46.7]
    runtime.last_hamlib_requested_power_watts = 50.0

    runtime._finish_hamlib_alc_period()

    assert transport.sent == []
    assert controller.profiles["20m"].tx_audio_attenuation == 32


def test_hamlib_converts_requested_power_fraction_to_watts(monkeypatch) -> None:
    controller = HamlibFtx1Controller("COM5", 38_400, 0.25)
    commands = []
    monkeypatch.setattr(controller, "read_rf_power_fraction", lambda: 0.5)
    monkeypatch.setattr(controller, "_command", lambda command: commands.append(command) or "50000")

    assert controller.read_requested_power_watts(14_074_000, "FT8") == 50.0
    assert commands == ["2 0.500000 14074000 USB"]


def test_hamlib_radio_catalogue_is_parsed_from_installed_backend(monkeypatch) -> None:
    executable = Path("rigctld.exe")
    output = """ Rig #  Mfg                    Model                   Version         Status      Macro
  1051  Yaesu                  FTX-1                   20251224.0      Beta        RIG_MODEL_FTX1
  3073  Icom                   IC-7300                 20250517.14     Stable      RIG_MODEL_IC7300
     1  Hamlib                 Dummy                   20240709.0      Stable      RIG_MODEL_DUMMY
"""
    monkeypatch.setattr("wsjtx_autopilot.hamlib.discover_rigctld", lambda configured_path="": executable)
    monkeypatch.setattr(
        "wsjtx_autopilot.hamlib.subprocess.run",
        lambda *args, **kwargs: type("Result", (), {"stdout": output})(),
    )

    assert list_supported_radios() == [
        (3073, "Icom", "IC-7300"),
        (1051, "Yaesu", "FTX-1"),
    ]


def test_ftx1_tuner_uses_documented_raw_cat_commands_through_hamlib(monkeypatch) -> None:
    controller = HamlibFtx1Controller("COM5", 38_400, 0.25, rig_model=1051)
    controller._identified = True
    controller.detected_radio_model = "Yaesu FTX-1"
    controller._ftx1_tuner_prefix = "10"
    commands = []
    monkeypatch.setattr(controller, "_command", lambda command: commands.append(command) or "No answer")

    assert controller.set_tuner_enabled(True)
    assert controller.start_tune()
    assert controller.set_tuner_enabled(False)
    assert commands == ["\\send_raw 0 AC101;", "\\send_raw 0 AC103;", "\\send_raw 0 AC100;"]


def test_ftx1_tuner_state_reports_active_and_ready(monkeypatch) -> None:
    controller = HamlibFtx1Controller("COM5", 38_400, 0.25, rig_model=1051)
    controller._identified = True
    controller.detected_radio_model = "Yaesu FTX-1"
    responses = iter(["AC103;", "AC101;"])
    monkeypatch.setattr(controller, "_command", lambda _command: next(responses))

    assert controller.tuner_reports_active_state
    assert controller.read_tuner_state() == 3
    assert controller.read_tuner_state() == 1
    assert controller._ftx1_tuner_prefix == "10"


def test_hamlib_reuses_existing_daemon_without_starting_a_duplicate(monkeypatch) -> None:
    class ExistingSocket:
        closed = False

        def settimeout(self, _timeout: float) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    existing = ExistingSocket()
    monkeypatch.setattr("wsjtx_autopilot.hamlib.socket.create_connection", lambda *_args, **_kwargs: existing)

    def duplicate_process(*_args, **_kwargs):
        raise AssertionError("a second rigctld must not be started")

    controller = HamlibFtx1Controller("COM5", 38_400, 0.25, process_factory=duplicate_process)
    assert controller.start()
    assert controller._process is None
    controller._identified = True
    assert controller.is_ready
    controller.close()
    assert existing.closed


def test_hamlib_detects_ftx1_from_real_cat_identity_before_vendor_commands(monkeypatch) -> None:
    controller = HamlibFtx1Controller("COM5", 38_400, 0.25, rig_model=1051)
    responses = {"f": "14074000", "\\get_info": "ID0840", "\\send_raw ; AC;": "AC001;"}
    monkeypatch.setattr(controller, "_command", lambda command: responses[command])

    assert controller.identify()
    assert controller.detected_radio_model == "Yaesu FTX-1"
    assert controller.detected_cat_id == "ID0840"
    assert controller._ftx1_tuner_prefix == "00"

    commands = []
    monkeypatch.setattr(controller, "_command", lambda command: commands.append(command) or "No answer")
    assert controller.start_tune()
    assert commands == ["\\send_raw 0 AC003;"]


def test_hamlib_unknown_cat_identity_uses_only_generic_commands(monkeypatch) -> None:
    controller = HamlibFtx1Controller("COM5", 38_400, 0.25, rig_model=1051)
    responses = {"f": "14074000", "\\get_info": "ID9999"}
    monkeypatch.setattr(controller, "_command", lambda command: responses[command])

    assert controller.identify()
    assert controller.detected_radio_model == "Hamlib generic"
    assert not controller.is_ftx1

    commands = []
    monkeypatch.setattr(controller, "_command_ok", lambda command: commands.append(command) or True)
    assert controller.set_tuner_enabled(True)
    assert controller.start_tune()
    assert commands == ["U TUNER 1", "G TUNE"]


def test_hamlib_swr_trip_halts_tx_and_disarms_when_automatic_tune_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", False)
    cat = FakeCat(35, alc=0.05, swr=2.5, power=20.0)
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_hamlib_enabled=True,
            ftx1_meter_poll_seconds=0.2,
            ftx1_swr_limit=2.0,
            ftx1_swr_trip_samples=2,
        ),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.current_tx_audio_attenuation = 100
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    transport.sent.clear()

    runtime.handle(None, NOW + timedelta(seconds=2), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=2.3), ENDPOINT)

    assert not control.armed
    assert runtime._hamlib_swr_fault_latched
    assert runtime._swr_recovery is None
    assert runtime.antenna_safety_level == "fault"
    assert "automatic tune disabled" in runtime.last_qso_notice
    assert "accord automatique désactivé" in "\n".join(runtime.antenna_safety_events)
    assert any(parse_datagram(data).header.packet_type == 8 for data, _ in transport.sent)


def test_hamlib_ptt_timeout_is_not_fatal_when_wsjtx_is_already_in_rx() -> None:
    cat = FakeHamlibCat()
    cat.stop_transmit = lambda: False  # type: ignore[method-assign]
    cat.read_ptt = lambda: None  # type: ignore[method-assign]
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=False), NOW, ENDPOINT)
    transport.sent.clear()

    assert runtime._halt_tx("WSJT-X", "remote engaged another station")
    assert control.armed
    assert parse_datagram(transport.sent[-1][0]).header.packet_type == 8


def test_transient_hamlib_identification_timeout_does_not_block_arm() -> None:
    cat = FakeHamlibCat()
    cat.identify = lambda: False  # type: ignore[attr-defined]
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=False)
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_hamlib_enabled=True,
            ftx1_alc_control_enabled=True,
            ftx1_swr_protection_enabled=True,
        ),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )

    assert runtime.arm_control()
    assert control.armed


class FakeTuneCat(FakeCat):
    def __init__(self, powers: list[float], swrs: list[float]) -> None:
        super().__init__(35, alc=0.05, swr=swrs[0], power=powers[0])
        self._tune_powers = list(powers)
        self._tune_swrs = list(swrs)

    def read_power(self) -> float:
        self.power_reads += 1
        if self.tune_started and self._tune_powers:
            self.power = self._tune_powers.pop(0)
        return self.power

    def read_swr(self) -> float:
        if self.tune_started and self._tune_swrs:
            self.swr = self._tune_swrs.pop(0)
        return self.swr


class FakeFtx1TuneCat(FakeTuneCat):
    tuner_reports_active_state = True

    def __init__(self, powers: list[float], swrs: list[float], states: list[int]) -> None:
        super().__init__(powers, swrs)
        self._tuner_states = list(states)
        self._last_tuner_state = 1

    def read_tuner_state(self) -> int:
        if self._tuner_states:
            self._last_tuner_state = self._tuner_states.pop(0)
        return self._last_tuner_state

    def read_rf_power_fraction(self) -> float:
        return 0.05 if self._last_tuner_state == 3 else self.rf_power_fraction


class FailingPttOffTuneCat(FakeTuneCat):
    def stop_transmit(self) -> bool:
        return False

    def read_ptt(self) -> bool | None:
        return None


def test_swr_recovery_waits_for_wsjtx_rx_when_hamlib_ptt_off_times_out(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FailingPttOffTuneCat([5.0, 5.0, 0.0], [1.5, 1.4, 1.4])
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_swr_limit=2.0),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)

    runtime._trip_hamlib_swr(2.5, NOW)

    assert runtime._swr_recovery is not None
    assert runtime._swr_recovery.phase == "wait_rx"
    assert control.armed
    runtime.handle(status(14_074_000, transmitting=False), NOW + timedelta(seconds=1.3), ENDPOINT)
    assert cat.tune_started
    assert runtime._swr_recovery is not None
    assert runtime._swr_recovery.phase == "tuning"


def test_swr_tune_success_restores_power_and_keeps_control_armed(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeTuneCat([5.0, 5.0, 0.0], [1.5, 1.4, 1.4])
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_swr_limit=2.0, ftx1_swr_trip_samples=1),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=1.3), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=1.9), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=2.5), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=3.1), ENDPOINT)

    assert cat.tune_started
    assert cat.restored_power == [0.5]
    assert runtime._swr_recovery is None
    assert not runtime._hamlib_swr_fault_latched
    assert control.armed
    assert runtime.antenna_safety_level == "warning"
    assert "Accord réussi" in "\n".join(runtime.antenna_safety_events)


def test_ftx1_swr_tune_uses_ac_state_and_never_writes_temporary_five_watts(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeFtx1TuneCat(
        [1.2, 3.0, 4.0, 0.0],
        [5.0, 1.49, 1.56, 1.0],
        [3, 3, 3, 1],
    )
    control = WsjtxUdpControl(FakeTransport(), 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_swr_limit=2.0, ftx1_swr_trip_samples=1),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(21_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(21_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    for seconds in (1.3, 1.9, 2.5, 3.1):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert runtime._swr_recovery is None
    assert runtime._swr_excluded_bands == set()
    assert control.armed
    assert cat.restored_power == []
    assert "restaurée automatiquement à 50 W" in "\n".join(runtime.antenna_safety_events)


def test_ftx1_ignores_early_stale_tuner_completion_until_rf_is_observed(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeFtx1TuneCat(
        [0.0, 0.0, 1.2, 3.0, 0.0],
        [5.0, 5.0, 1.60, 1.50, 1.0],
        [3, 1, 3, 3, 1],
    )
    control = WsjtxUdpControl(FakeTransport(), 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_swr_limit=2.0, ftx1_swr_trip_samples=1),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(21_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(21_074_000), NOW + timedelta(seconds=1), ENDPOINT)

    runtime.handle(None, NOW + timedelta(seconds=1.3), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=1.9), ENDPOINT)

    assert runtime._swr_recovery is not None
    assert runtime._swr_recovery.phase == "tuning"
    assert control.armed

    for seconds in (2.5, 3.1, 3.7):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert runtime._swr_recovery is None
    assert runtime._swr_excluded_bands == set()
    assert control.armed


def test_ftx1_single_bad_rf_sample_excludes_band_and_tries_next_band(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeFtx1TuneCat(
        [2.78, 0.0, 0.0, 0.0],
        [5.0, 1.0, 1.0, 1.0],
        [3, 1, 1, 1],
    )
    control = WsjtxUdpControl(FakeTransport(), 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_hamlib_enabled=True,
            ftx1_swr_limit=2.0,
            ftx1_swr_trip_samples=1,
            allowed_auto_hop_bands=("30m", "20m"),
        ),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(10_136_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(5.0, NOW)
    runtime.handle(status(10_136_000), NOW + timedelta(seconds=1), ENDPOINT)
    for seconds in (1.3, 1.9, 2.5, 3.1):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert runtime._swr_excluded_bands == {"30m"}
    assert runtime._pending_dial_frequency is not None
    assert runtime._pending_dial_frequency.band == "20m"
    assert runtime._swr_recovery is not None
    assert runtime._swr_recovery.phase == "changing_band"
    assert control.armed
    assert "Bande 30m exclue" in "\n".join(runtime.antenna_safety_events)


def test_swr_tune_accepts_low_final_sample_after_high_sweep(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeTuneCat([5.0, 5.0, 5.0, 5.0, 0.0], [5.0, 5.0, 1.2, 1.2, 1.2])
    control = WsjtxUdpControl(FakeTransport(), 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_swr_limit=2.0, ftx1_swr_trip_samples=1),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    for seconds in (1.3, 1.9, 2.5, 3.1, 3.7):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert runtime._swr_recovery is None
    assert runtime._swr_excluded_bands == set()
    assert cat.tune_start_count == 1
    assert control.armed


def test_swr_tune_does_not_accept_one_isolated_low_sample(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeTuneCat(
        [5.0, 5.0, 5.0, 0.0, 5.0, 5.0, 0.0],
        [5.0, 5.0, 5.0, 5.0, 1.3, 1.2, 1.2],
    )
    control = WsjtxUdpControl(FakeTransport(), 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(ftx1_hamlib_enabled=True, ftx1_swr_limit=2.0, ftx1_swr_trip_samples=1),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    for seconds in (1.3, 1.9, 2.5, 3.1, 3.4, 4.0, 4.6):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert runtime._swr_recovery is None
    assert runtime._swr_excluded_bands == {"20m"}
    assert cat.tune_start_count == 1
    assert not control.armed


def test_tune_that_never_produces_rf_stops_without_excluding_band(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeTuneCat([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_hamlib_enabled=True,
            ftx1_swr_limit=2.0,
            allowed_auto_hop_bands=("20m", "15m"),
        ),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=1.3), ENDPOINT)
    runtime.handle(None, NOW + timedelta(seconds=4.2), ENDPOINT)

    assert runtime._swr_recovery is None
    assert runtime._swr_excluded_bands == set()
    assert not control.armed
    assert "accord automatique non démarré" in runtime.last_qso_notice


def test_failed_swr_tune_excludes_band_and_resumes_on_next_band_without_retune(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeTuneCat(
        [5.0, 5.0, 0.0, 5.0, 5.0, 0.0],
        [2.6, 2.4, 2.4, 1.5, 1.4, 1.4],
    )
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_hamlib_enabled=True,
            ftx1_swr_limit=2.0,
            allowed_auto_hop_bands=("20m", "15m"),
        ),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    for seconds in (1.3, 1.9, 2.5, 2.8, 3.4, 4.0):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert "20m" in runtime._swr_excluded_bands
    assert runtime._pending_dial_frequency is not None
    assert runtime._pending_dial_frequency.band == "15m"

    runtime.handle(status(21_074_000), NOW + timedelta(seconds=4.3), ENDPOINT)

    assert runtime._swr_recovery is None
    assert cat.tune_start_count == 1
    assert control.armed
    assert runtime.antenna_safety_level == "warning"
    events = "\n".join(runtime.antenna_safety_events)
    assert "Bande 20m exclue" in events
    assert "Bande 15m confirmée, reprise sans nouvel accord automatique" in events


def test_unrecoverable_swr_disarms_and_sets_red_fault(monkeypatch) -> None:
    monkeypatch.setattr("wsjtx_autopilot.runtime._SWR_AUTOMATIC_RECOVERY_ENABLED", True)
    cat = FakeTuneCat(
        [5.0, 5.0, 0.0, 5.0, 5.0, 0.0],
        [2.8, 2.7, 2.7, 2.8, 2.7, 2.7],
    )
    transport = FakeTransport()
    control = WsjtxUdpControl(transport, 15, None, armed=True)
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_hamlib_enabled=True,
            ftx1_swr_limit=2.0,
            allowed_auto_hop_bands=("20m",),
        ),
        control=control,
        ftx1_band_drive=FTX1BandDriveController(cat),
    )
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000, transmitting=True), NOW, ENDPOINT)
    runtime._trip_hamlib_swr(2.5, NOW)
    runtime.handle(status(14_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    for seconds in (1.3, 1.9, 2.5, 2.8, 3.4, 4.0):
        runtime.handle(None, NOW + timedelta(seconds=seconds), ENDPOINT)

    assert runtime._swr_recovery is None
    assert not control.armed
    assert runtime.antenna_safety_level == "fault"
    assert runtime.last_qso_notice.startswith("SWR SAFETY FINAL")
    assert "ARRÊT DÉFINITIF" in "\n".join(runtime.antenna_safety_events)


def test_non_ftx1_runtime_is_unchanged() -> None:
    runtime = AutopilotRuntime(AppConfig())
    assert runtime.ftx1_band_drive is None
    assert not runtime.save_ftx1_current_band_profile()


def test_save_current_band_updates_confirmed_band_profile_only() -> None:
    cat = FakeCat(30)
    controller = FTX1BandDriveController(cat)
    runtime = AutopilotRuntime(AppConfig(), ftx1_band_drive=controller)
    runtime.current_tx_audio_attenuation = 24
    runtime.handle(status(14_074_000), NOW, ENDPOINT)

    assert runtime.save_ftx1_current_band_profile()

    assert controller.profiles["20m"].usb_mod_gain == 0
    assert controller.profiles["20m"].tx_audio_attenuation == 24
    assert "40m" not in controller.profiles


def test_save_current_band_refuses_pending_band_or_attenuation_changes() -> None:
    controller = FTX1BandDriveController(FakeCat(30))
    runtime = AutopilotRuntime(AppConfig(), ftx1_band_drive=controller)
    runtime.current_tx_audio_attenuation = 24
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    runtime._pending_dial_frequency = PendingDialFrequency("WSJT-X", 7_074_000, "40m", NOW + timedelta(seconds=5), "20m")

    assert not runtime.save_ftx1_current_band_profile()
    assert controller.profiles == {}

    runtime._pending_dial_frequency = None
    runtime.pending_tx_audio_attenuation = 25
    assert not runtime.save_ftx1_current_band_profile()
    assert controller.profiles == {}


def test_forced_hop_restores_destination_profile_and_waits_for_attenuation_confirmation() -> None:
    cat = FakeCat(35)
    transport = FakeTransport()
    controller = FTX1BandDriveController(cat, {
        "20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118},
        "40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145},
    })
    runtime = AutopilotRuntime(
        AppConfig(
            candidate_collection_seconds=0,
            ftx1_auto_apply_band_profiles=True,
            forced_band_hopping_enabled=True,
            forced_band_hop_bands=("20m", "40m"),
            forced_band_hop_interval_minutes=10,
            forced_band_hop_settle_seconds=5,
        ),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=controller,
    )
    runtime.current_tx_audio_attenuation = 118
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    transport.sent.clear()

    runtime.handle(None, NOW + timedelta(minutes=10), ENDPOINT)
    assert parse_datagram(transport.sent[-1][0]).header.packet_type == 20
    runtime.handle(status(7_074_000), NOW + timedelta(minutes=10, seconds=1), ENDPOINT)

    assert cat.gains == []
    assert parse_datagram(transport.sent[-1][0]).header.packet_type == 21
    assert runtime.pending_tx_audio_attenuation == 145
    assert runtime._band_resume_at is None

    runtime.handle(TxAudioAttenuationStatePacket(PacketHeader(3, 1, "WSJT-X"), 144), NOW + timedelta(minutes=10, seconds=2), ENDPOINT)
    assert runtime.pending_tx_audio_attenuation == 145
    assert runtime._band_resume_at is None
    cq = DecodePacket(PacketHeader(3, 2, "WSJT-X"), True, NOW.time(), -8, 0.2, 900, "~", "CQ OH2ZZ KP20", False, False)
    assert runtime.handle(cq, NOW + timedelta(minutes=10, seconds=2), ENDPOINT) is None

    runtime.handle(TxAudioAttenuationStatePacket(PacketHeader(3, 1, "WSJT-X"), 145), NOW + timedelta(minutes=10, seconds=3), ENDPOINT)
    assert runtime.pending_tx_audio_attenuation is None
    ready_at = NOW + timedelta(minutes=10, seconds=3)
    assert runtime._band_resume_at == ready_at + timedelta(seconds=5)
    assert runtime.handle(None, ready_at + timedelta(seconds=1), ENDPOINT) is None
    assert runtime.handle(None, ready_at + timedelta(seconds=5), ENDPOINT) is not None


def test_forced_hop_blocks_calls_when_destination_profile_is_missing() -> None:
    cat = FakeCat(35)
    transport = FakeTransport()
    controller = FTX1BandDriveController(cat, {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}})
    runtime = AutopilotRuntime(
        AppConfig(
            candidate_collection_seconds=0,
            ftx1_auto_apply_band_profiles=True,
            forced_band_hopping_enabled=True,
            forced_band_hop_bands=("20m", "40m"),
            forced_band_hop_interval_minutes=10,
        ),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=controller,
    )
    runtime.current_tx_audio_attenuation = 118
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    transport.sent.clear()
    runtime.handle(None, NOW + timedelta(minutes=10), ENDPOINT)
    runtime.handle(status(7_074_000), NOW + timedelta(minutes=10, seconds=1), ENDPOINT)
    cq = DecodePacket(PacketHeader(3, 2, "WSJT-X"), True, NOW.time(), -8, 0.2, 900, "~", "CQ OH2ZZ KP20", False, False)

    assert runtime._blocked_band_drive_change is not None
    assert runtime.handle(cq, NOW + timedelta(minutes=10, seconds=2), ENDPOINT) is None
    assert not any(parse_datagram(data).header.packet_type == 0 for data, _ in transport.sent)


def test_manual_band_change_restores_saved_profile_once() -> None:
    cat = FakeCat(35)
    transport = FakeTransport()
    controller = FTX1BandDriveController(cat, {
        "20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118},
        "40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145},
    })
    runtime = AutopilotRuntime(
        AppConfig(ftx1_auto_apply_band_profiles=True),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=controller,
    )
    runtime.current_tx_audio_attenuation = 118
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    transport.sent.clear()

    runtime.handle(status(7_074_000), NOW + timedelta(seconds=1), ENDPOINT)
    runtime.handle(status(7_074_000), NOW + timedelta(seconds=2), ENDPOINT)

    assert cat.gains == []
    assert sum(parse_datagram(data).header.packet_type == 21 for data, _ in transport.sent) == 1


def test_forced_hop_attenuation_timeout_blocks_automatic_tx() -> None:
    cat = FakeCat(35)
    transport = FakeTransport()
    controller = FTX1BandDriveController(cat, {
        "20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118},
        "40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145},
    })
    runtime = AutopilotRuntime(
        AppConfig(
            ftx1_auto_apply_band_profiles=True,
            forced_band_hopping_enabled=True,
            forced_band_hop_bands=("20m", "40m"),
            forced_band_hop_interval_minutes=10,
        ),
        control=WsjtxUdpControl(transport, 15, None, armed=True),
        ftx1_band_drive=controller,
    )
    runtime.current_tx_audio_attenuation = 118
    runtime.handle(heartbeat(), NOW, ENDPOINT)
    runtime.handle(status(14_074_000), NOW, ENDPOINT)
    runtime.handle(None, NOW + timedelta(minutes=10), ENDPOINT)
    runtime.handle(status(7_074_000), NOW + timedelta(minutes=10, seconds=1), ENDPOINT)
    runtime._tx_audio_attenuation_deadline = NOW + timedelta(minutes=10, seconds=2)

    runtime.handle(None, NOW + timedelta(minutes=10, seconds=3), ENDPOINT)

    assert runtime._blocked_band_drive_change is not None
    assert runtime._band_resume_at is None
