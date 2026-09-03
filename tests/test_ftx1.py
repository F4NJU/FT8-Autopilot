from datetime import datetime, timedelta, timezone

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.ftx1 import FTX1BandDriveController, FTX1CatController
from wsjtx_autopilot.runtime import AutopilotRuntime, PendingDialFrequency
from wsjtx_autopilot.wsjtx.models import HeartbeatPacket, PacketHeader, StatusPacket, TxAudioAttenuationStatePacket
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
    def __init__(self, gain: int | None = 35) -> None:
        self.gain = gain
        self.gains: list[int] = []
        self.alc_reads = 0
        self.power_reads = 0
        self.is_ready = True

    def set_usb_mod_gain(self, gain: int) -> bool:
        self.gain = gain
        self.gains.append(gain)
        return True

    def read_usb_mod_gain(self) -> int | None:
        return self.gain

    def read_alc(self) -> int:
        self.alc_reads += 1
        return 63

    def read_power(self) -> int:
        self.power_reads += 1
        return 120

    def close(self) -> None:
        pass


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


def test_ftx1_usb_mod_gain_command_is_exact_and_confirmed() -> None:
    serial = FakeSerial([b"ID0840;", b"EX010414035;"])
    controller = FTX1CatController("COM5", 38_400, 0.25, lambda: serial)

    assert controller.set_usb_mod_gain(35)
    assert serial.writes == [b"ID;", b"EX010414035;", b"EX010414;"]


def test_save_current_profile_captures_actual_usb_gain_and_attenuation() -> None:
    saved: list[dict[str, dict[str, int]]] = []
    controller = FTX1BandDriveController(FakeCat(35), changed=saved.append)

    assert controller.save_current_profile("20m", 118)
    assert controller.profiles["20m"].usb_mod_gain == 35
    assert controller.profiles["20m"].tx_audio_attenuation == 118
    assert saved[-1] == {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}}


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

    assert controller.profiles["20m"].usb_mod_gain == 35
    assert controller.profiles["20m"].tx_audio_attenuation == 118


def test_failed_usb_read_does_not_overwrite_existing_profile() -> None:
    controller = FTX1BandDriveController(FakeCat(None), {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}})

    assert not controller.save_current_profile("20m", 120)
    assert controller.profiles["20m"].tx_audio_attenuation == 118


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

    assert cat.gains == [32]
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


def test_non_ftx1_runtime_is_unchanged() -> None:
    runtime = AutopilotRuntime(AppConfig())
    assert runtime.ftx1_band_drive is None
    assert not runtime.save_ftx1_current_band_profile()
