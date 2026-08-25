import socket
from datetime import datetime, time, timedelta, timezone

from PySide6.QtWidgets import QApplication, QMessageBox

from wsjtx_autopilot.config import SettingsStore, UserSettings
from wsjtx_autopilot.control.wsjtx_udp import WsjtxUdpControl
from wsjtx_autopilot.engine.models import ActionKind, ActionOutcome, IntendedAction, OriginalDecode
from wsjtx_autopilot.engine.state import QsoState
from wsjtx_autopilot.gui.worker import BackendWorker
from wsjtx_autopilot.gui.main_window import MainWindow
from wsjtx_autopilot.wsjtx.models import DecodePacket, PacketHeader, ReplyPacket, SetTxDfPacket, StatusPacket
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
        set_tx_df = parse_datagram(data)

        assert action is not None and action.kind is ActionKind.DIRECT_REPLY
        assert isinstance(set_tx_df, SetTxDfPacket)
        confirmation = StatusPacket(
            PacketHeader(2, 1, "WSJT-X"),
            14_074_000,
            "FT8",
            "",
            "",
            "FT8",
            False,
            False,
            False,
            packet.delta_frequency,
            set_tx_df.tx_df,
            "F4NJU",
            "JN18",
            "",
            False,
            "",
            False,
            0,
            0xFFFFFFFF,
            15,
            "Default",
            "",
        )
        backend._runtime.handle(confirmation, NOW + timedelta(seconds=1), (str(endpoint[0]), int(endpoint[1])))
        data, _ = receiver.recvfrom(65_535)
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
