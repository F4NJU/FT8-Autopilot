import socket
from datetime import datetime, time, timedelta, timezone

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox, QScrollArea

from wsjtx_autopilot.config import SettingsStore, UserSettings
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, EngineEvent, EngineEventKind, IntendedAction, OriginalDecode
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.ftx1 import FTX1CatController
from wsjtx_autopilot.gui.worker import BackendWorker
from wsjtx_autopilot.gui.main_window import MainWindow, SettingsDialog
import wsjtx_autopilot.runtime as runtime_module
from wsjtx_autopilot.wsjtx.models import DecodePacket, HeartbeatPacket, PacketHeader, ReplyPacket, StatusPacket, TxAudioAttenuationStatePacket
from wsjtx_autopilot.wsjtx.protocol import parse_datagram

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def decode(message: str, *, schema: int = 2, sequence: int = 0) -> DecodePacket:
    return DecodePacket(
        PacketHeader(schema, 2, "WSJT-X"),
        True,
        time(12, 0, sequence),
        -8,
        0.2,
        1200 + sequence,
        "~",
        message,
        False,
        False,
    )


def worker(tmp_path, monkeypatch, *, direct_patched: bool = False) -> BackendWorker:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    backend = BackendWorker(
        UserSettings(
            udp_port=0,
            allow_dupes=True,
            worked_store_path=str(tmp_path / "worked.sqlite3"),
            direct_reply_patched=direct_patched,
        ),
    )
    backend._open_backend()
    return backend


class FakeFtx1Serial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def read_until(self, expected: bytes = b";") -> bytes:
        assert expected == b";"
        return b"ID0840;"

    def close(self) -> None:
        self.closed = True


def test_gui_worker_opens_identified_ftx1_cat2_from_persisted_settings(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        UserSettings(
            udp_port=0,
            worked_store_path=str(tmp_path / "worked.sqlite3"),
            ftx1_cat2_enabled=True,
            ftx1_cat2_confirmed_ftx1=True,
            ftx1_cat2_port="COM6",
            ftx1_cat2_baudrate=4_800,
        )
    )
    serial = FakeFtx1Serial()
    original_controller = FTX1CatController

    def create_controller(port: str, baudrate: int, timeout_seconds: float) -> FTX1CatController:
        assert (port, baudrate) == ("COM6", 4_800)
        return original_controller(port, baudrate, timeout_seconds, lambda: serial)

    monkeypatch.setattr(runtime_module, "FTX1CatController", create_controller)
    caplog.set_level("INFO")
    backend = BackendWorker(store.load())
    try:
        backend._open_backend()

        assert backend._runtime is not None
        assert backend._runtime.ftx1_band_drive is not None
        cat = backend._runtime.ftx1_band_drive.cat
        assert cat.is_ready
        assert backend._runtime.adaptive_snapshot()["ftx1"]["connected"] is True
        assert serial.writes == [b"ID;"]
        assert "CAT-2 config enabled=True port=COM6 baud=4800" in caplog.text
        assert "FTX-1 identified" in caplog.text
        assert "CAT-2 ready" in caplog.text
    finally:
        backend._close_backend()


def test_gui_arm_uses_same_unlimited_control_instance(tmp_path, monkeypatch) -> None:
    backend = worker(tmp_path, monkeypatch)
    try:
        assert backend._runtime is not None
        control = backend._runtime.control
        assert isinstance(control, WsjtxUdpControl)
        assert not control.armed
        assert control.max_actions is None

        backend.arm()

        assert backend._runtime.control is control
        assert control.armed
    finally:
        backend._close_backend()


def test_arm_request_reaches_runtime_and_stays_armed_across_wsjtx_packets(tmp_path, monkeypatch, caplog) -> None:
    backend = worker(tmp_path, monkeypatch)
    try:
        assert backend._runtime is not None
        control = backend._runtime.control
        assert isinstance(control, WsjtxUdpControl)
        caplog.set_level("INFO")

        backend.arm()
        for seconds in range(3):
            backend._runtime.handle(
                HeartbeatPacket(PacketHeader(3, 0, "WSJT-X"), 3, "3.2.0", "260818-AP1"),
                NOW + timedelta(seconds=seconds),
                ("127.0.0.1", 2237),
            )
            backend._runtime.handle(
                StatusPacket(PacketHeader(3, 1, "WSJT-X"), 14_074_000, "FT8", "", "", "FT8", False, False, False, 1000, 1000, "", "", "", False, "", False, 0, 0xFFFFFFFF, 15, "Default", ""),
                NOW + timedelta(seconds=seconds),
                ("127.0.0.1", 2237),
            )
            backend._runtime.handle(
                TxAudioAttenuationStatePacket(PacketHeader(3, 23, "WSJT-X"), 118),
                NOW + timedelta(seconds=seconds),
                ("127.0.0.1", 2237),
            )

        assert control.armed
        assert "[WORKER] ARM request received" in caplog.text
        assert "[RUNTIME] ARM request received" in caplog.text
        assert "[CONTROL] arm() called previous=False" in caplog.text
        assert "[CONTROL] ARMED implementation=WsjtxUdpControl" in caplog.text
    finally:
        backend._close_backend()


def test_gui_offline_disarm_logs_reason_and_source(tmp_path, monkeypatch, caplog) -> None:
    backend = worker(tmp_path, monkeypatch)
    try:
        backend.arm()
        backend._set_connected(True)

        backend._set_connected(False)

        assert backend._runtime is not None
        assert not backend._runtime.control.armed
        assert "DISARM reason=wsjtx_offline source=BackendWorker._set_connected" in caplog.text
    finally:
        backend._close_backend()


def test_gui_startup_auto_imports_standard_wsjtx_adif(tmp_path, monkeypatch) -> None:
    adif = tmp_path / "Local" / "WSJT-X" / "wsjtx_log.adi"
    adif.parent.mkdir(parents=True)
    adif.write_text(
        "<CALL:5>YO6LM<QSO_DATE:8>20260824<BAND:3>20M<MODE:3>FT8<EOR>",
        encoding="ascii",
    )

    backend = worker(tmp_path, monkeypatch)
    try:
        assert backend._store is not None
        assert backend._store.count_for_date(NOW.date()) == 1
        assert backend._store.source_for(NOW.date(), "YO6LM", "20m") == "WSJTX_ADIF"
    finally:
        backend._close_backend()


def test_gui_confirmation_emits_arm_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    application = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(udp_port=0, worked_store_path=str(tmp_path / "worked.sqlite3")))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window = MainWindow(store)
    requested = []
    window.arm_requested.connect(lambda: requested.append(True))
    application.processEvents()
    try:
        window._toggle_arm()

        assert requested == [True]
    finally:
        window.close()
        application.processEvents()


def test_gui_resynchronizes_armed_visual_state_from_backend_signal(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    application = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(udp_port=0, worked_store_path=str(tmp_path / "worked.sqlite3")))
    window = MainWindow(store)
    application.processEvents()
    try:
        caplog.set_level("INFO")
        window._show_armed(True)
        assert window._armed
        window._show_armed(False)

        assert not window._armed
        assert window.arm_button.text() == "ARM CONTROL"
        assert "ARM state divergence backend_armed=False ui_armed=True; resynchronizing" in caplog.text
        assert "control state backend_armed=False ui_armed=False" in caplog.text
    finally:
        window.close()
        application.processEvents()


def test_gui_cq_sends_exact_reply_to_decode_source(tmp_path, monkeypatch) -> None:
    backend = worker(tmp_path, monkeypatch)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(1)
    endpoint = receiver.getsockname()
    packet = decode("CQ DL1ABC JO40", schema=3)
    try:
        backend.arm()
        assert backend._runtime is not None
        action = backend._runtime.handle(packet, NOW, (str(endpoint[0]), int(endpoint[1])))
        assert action is None
        action = backend._runtime.handle(None, NOW + timedelta(seconds=1))
        data, source = receiver.recvfrom(65_535)
        reply = parse_datagram(data)

        assert action is not None
        assert action.original_decode is not None
        assert action.original_decode.message == packet.message
        assert isinstance(reply, ReplyPacket)
        assert reply.header.schema == 3
        assert reply.header.instance_id == "WSJT-X"
        assert reply.message == packet.message
        assert source[1] == backend._listener._socket.getsockname()[1]
        assert backend._runtime.engine.state.session.state is QsoState.CALLING_STATION
    finally:
        receiver.close()
        backend._close_backend()


def test_gui_direct_call_sends_exact_reply_to_decode_source(tmp_path, monkeypatch) -> None:
    backend = worker(tmp_path, monkeypatch, direct_patched=True)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(1)
    endpoint = receiver.getsockname()
    packet = decode("F4NJU YO6LM KN25", schema=2)
    try:
        backend.arm()
        assert backend._runtime is not None
        assert backend._runtime.handle(packet, NOW, (str(endpoint[0]), int(endpoint[1]))) is None
        action = backend._runtime.handle(None, NOW + timedelta(milliseconds=250))
        data, _ = receiver.recvfrom(65_535)
        assert action is not None and action.kind is ActionKind.DIRECT_REPLY
        reply = parse_datagram(data)
        assert isinstance(reply, ReplyPacket)
        assert reply.header.schema == 2
        assert reply.header.instance_id == "WSJT-X"
        assert reply.message == "F4NJU YO6LM KN25"
        assert backend._runtime.engine.state.session.state is QsoState.DIRECT_REPLY_SENT
    finally:
        receiver.close()
        backend._close_backend()


def test_gui_control_remains_armed_after_ten_replies(tmp_path, monkeypatch) -> None:
    backend = worker(tmp_path, monkeypatch)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    endpoint = (str(receiver.getsockname()[0]), int(receiver.getsockname()[1]))
    try:
        backend.arm()
        assert backend._runtime is not None
        control = backend._runtime.control
        assert isinstance(control, WsjtxUdpControl)
        for index in range(10):
            original = OriginalDecode(
                "WSJT-X",
                2,
                time(12, 0, index),
                -8,
                0.2,
                1200 + index,
                "~",
                f"CQ K{index}ABC FN31",
                False,
                True,
                endpoint,
            )
            control.observe(decode(original.message, sequence=index), endpoint)
            outcome = control.execute(
                IntendedAction(ActionKind.CQ_REPLY, f"K{index}ABC", "test", original, NOW),
                NOW,
            )
            assert outcome is ActionOutcome.SENT

        assert control.actions_used == 10
        assert control.max_actions is None
        assert control.armed
    finally:
        receiver.close()
        backend._close_backend()


def test_preferences_dialog_is_resizable_scrollable_and_keeps_buttons_visible() -> None:
    QApplication.instance() or QApplication([])
    dialog = SettingsDialog(UserSettings(), ap1_available=True)

    assert dialog.minimumWidth() == 650
    assert dialog.minimumHeight() == 450
    assert isinstance(dialog.scroll_area, QScrollArea)
    assert dialog.scroll_area.widget() is dialog.tabs
    assert not dialog.scroll_area.isAncestorOf(dialog.buttons)
    assert dialog._size_for_available_geometry(QSize(1366, 768)) == QSize(750, 700)
    dialog.resize(650, 450)
    assert dialog.size() == QSize(650, 450)


def test_preferences_apply_ok_and_cancel_are_wired() -> None:
    QApplication.instance() or QApplication([])
    applied = []
    dialog = SettingsDialog(UserSettings(), ap1_available=True)
    dialog.settings_applied.connect(applied.append)

    dialog.buttons.button(QDialogButtonBox.StandardButton.Apply).click()
    assert len(applied) == 1
    dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    assert len(applied) == 2
    assert dialog.result() == QDialog.DialogCode.Accepted

    cancelled = SettingsDialog(UserSettings(), ap1_available=True)
    cancelled.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
    assert cancelled.result() == QDialog.DialogCode.Rejected


def test_adaptive_controls_require_ap1_and_preserve_band_order() -> None:
    QApplication.instance() or QApplication([])
    dialog = SettingsDialog(
        UserSettings(direct_reply_patched=True, allowed_auto_hop_bands=["40m", "20m"]),
        ap1_available=False,
    )

    assert list(dialog.auto_band_checkboxes) == ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
    assert dialog.auto_band_checkboxes["40m"].isChecked()
    assert not dialog.auto_band_hop.isEnabled()
    assert dialog.auto_band_hop.toolTip() == "Requires WSJT-X AutoPilot AP1"
    assert dialog.settings().allowed_auto_hop_bands == ["40m", "20m"]


def test_ftx1_cat2_preferences_store_manual_band_profiles() -> None:
    QApplication.instance() or QApplication([])
    dialog = SettingsDialog(UserSettings(), ap1_available=True)

    assert not dialog.ftx1_cat2_port.isEnabled()
    dialog.ftx1_cat2_enabled.setChecked(True)
    dialog.ftx1_cat2_confirmed_ftx1.setChecked(True)
    dialog.ftx1_cat2_port.setText("COM5")
    dialog.ftx1_cat2_baudrate.setValue(38_400)
    dialog.ftx1_auto_apply_band_profiles.setChecked(True)
    dialog.ftx1_profile_fields["20m"][0].setText("35")
    dialog.ftx1_profile_fields["20m"][1].setText("118")
    saved = dialog.settings()

    assert saved.ftx1_cat2_enabled
    assert saved.ftx1_cat2_confirmed_ftx1
    assert saved.ftx1_cat2_port == "COM5"
    assert saved.ftx1_auto_apply_band_profiles
    assert saved.ftx1_band_profiles == {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}}


def test_ftx1_auto_apply_preferences_default_off_and_round_trip() -> None:
    QApplication.instance() or QApplication([])
    dialog = SettingsDialog(UserSettings(), ap1_available=True)

    assert not dialog.ftx1_auto_apply_band_profiles.isChecked()
    dialog.ftx1_cat2_enabled.setChecked(True)
    dialog.ftx1_cat2_confirmed_ftx1.setChecked(True)
    dialog.ftx1_auto_apply_band_profiles.setChecked(True)
    saved = dialog.settings()

    assert saved.ftx1_auto_apply_band_profiles


def test_band_hop_requires_at_least_one_selected_band(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    dialog = SettingsDialog(UserSettings(), ap1_available=True)
    dialog.auto_band_hop.setChecked(True)
    for checkbox in dialog.auto_band_checkboxes.values():
        checkbox.setChecked(False)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args))

    dialog.accept()

    assert messages
    assert "Select at least one allowed band" in messages[0][2]


def test_main_window_displays_adaptive_state_and_emits_reset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    application = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(udp_port=0))
    window = MainWindow(store)
    resets = []
    window.reset_adaptive_requested.connect(lambda: resets.append(True))
    application.processEvents()
    try:
        window._worker.adaptive_changed.emit(
            {
                "ap1_available": True,
                "state": "BAND_CHANGING",
                "current_tx_first": True,
                "requested_tx_period": False,
                "current_band": "20m",
                "next_band": "17m",
                "band_changing_to": "17m",
            }
        )
        application.processEvents()
        assert window.ap1_status.text() == "WSJT-X AP1: Available"
        assert window.adaptive_status.text() == "Adaptive: Changing 20m -> 17m"
        assert window.tx_period_status.text() == "TX period: Second (Auto)"
        assert window.band_status.text() == "Band: 20m"
        assert window.next_band_status.text() == "Next adaptive band: 17m"
        next(button for button in window.findChildren(type(window.arm_button)) if button.text() == "RESET ADAPTIVE STRATEGY").click()
        assert resets == [True]
    finally:
        window.close()
        application.processEvents()


def test_main_window_exposes_ftx1_manual_profile_actions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    application = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(udp_port=0, ftx1_cat2_enabled=True, ftx1_cat2_confirmed_ftx1=True))
    window = MainWindow(store)
    saves = []
    deletes = []
    resets = []
    window.save_ftx1_current_band_profile_requested.connect(lambda: saves.append(True))
    window.delete_ftx1_current_band_profile_requested.connect(lambda: deletes.append(True))
    window.reset_all_ftx1_band_profiles_requested.connect(lambda: resets.append(True))
    application.processEvents()
    try:
        window._show_adaptive(
            {
                "ap1_available": True,
                "state": "NORMAL",
                "current_tx_first": None,
                "requested_tx_period": None,
                "current_band": "20m",
                "next_band": None,
                "band_changing_to": None,
                "ftx1": {"connected": True, "current_usb_mod_gain": 35, "current_tx_audio_attenuation": 118, "saved_usb_mod_gain": 35, "saved_tx_audio_attenuation": 118},
            }
        )
        assert not window.ftx1_status.isHidden()
        window.save_ftx1_current_band_profile_button.click()
        window.delete_ftx1_current_band_profile_button.click()
        window.reset_all_ftx1_band_profiles_button.click()
        assert saves == [True]
        assert deletes == [True]
        assert resets == [True]
    finally:
        window.close()
        application.processEvents()


def test_dark_theme_has_no_tx_df_planner_controls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    application = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(udp_port=0, direct_reply_patched=True))
    window = MainWindow(store)
    application.processEvents()
    try:
        style = window.styleSheet()
        assert "QRadioButton::indicator" not in style
        assert "Smart TX" not in style
    finally:
        window.close()
        application.processEvents()


def test_gui_displays_pending_direct_call_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    application = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    store.save(UserSettings(udp_port=0))
    window = MainWindow(store)
    application.processEvents()
    try:
        window._handle_engine_event(
            EngineEvent(EngineEventKind.PENDING_DIRECT_ADDED, "S51DD", "Waiting for current QSO to finish")
        )
        assert window.pending_direct_label.text() == "Pending Direct Calls: 1\nNext: S51DD"

        window._handle_engine_event(EngineEvent(EngineEventKind.PENDING_DIRECT_REMOVED, "S51DD", "selected"))
        assert window.pending_direct_label.text() == "Pending Direct Calls: 0"
    finally:
        window.close()
        application.processEvents()
