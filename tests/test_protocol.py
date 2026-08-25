import struct
from datetime import datetime, time, timezone

import pytest

from wsjtx_autopilot.wsjtx.models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    HaltTxPacket,
    QsoLoggedPacket,
    ReplyPacket,
    StatusPacket,
    SetTxDfPacket,
    UnknownPacket,
)
from wsjtx_autopilot.wsjtx.protocol import (
    MAGIC,
    ProtocolError,
    parse_datagram,
    serialize_halt_tx,
    serialize_reply,
    serialize_set_tx_df,
)


def qbytearray(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def qdatetime(value: datetime) -> bytes:
    julian_day = value.date().toordinal() + 1_721_425
    milliseconds = (
        value.hour * 3_600_000
        + value.minute * 60_000
        + value.second * 1_000
        + value.microsecond // 1_000
    )
    return struct.pack(">qIB", julian_day, milliseconds, 1)


def decode_datagram(schema: int) -> bytes:
    header = struct.pack(">III", MAGIC, schema, 2) + qbytearray("WSJT-X")
    payload = (
        struct.pack(">BIidI", 1, 12 * 3_600_000, -8, 0.2, 850)
        + qbytearray("FT8")
        + qbytearray("F4NJU ON4ABC -11")
        + struct.pack(">BB", 0, 0)
    )
    return header + payload


def status_datagram(schema: int) -> bytes:
    header = struct.pack(">III", MAGIC, schema, 1) + qbytearray("WSJT-X")
    payload = struct.pack(">Q", 14_074_000)
    payload += b"".join(qbytearray(value) for value in ("FT8", "ON4ABC", "-08", "FT8"))
    payload += struct.pack(">BBBII", 0, 0, 1, 850, 850)
    payload += b"".join(qbytearray(value) for value in ("F4NJU", "JN18", "JO20"))
    return header + payload


def assert_decode(packet: object, schema: int) -> None:
    assert isinstance(packet, DecodePacket)
    assert packet.header.schema == schema
    assert packet.is_new
    assert packet.decode_time.hour == 12
    assert packet.snr == -8
    assert packet.message == "F4NJU ON4ABC -11"


def assert_status(packet: object, schema: int) -> None:
    assert isinstance(packet, StatusPacket)
    assert packet.header.schema == schema
    assert packet.dial_frequency == 14_074_000
    assert packet.mode == "FT8"
    assert packet.tr_period == 0xFFFFFFFF


def test_parses_schema_3_decode_packet() -> None:
    assert_decode(parse_datagram(decode_datagram(3)), 3)


def test_parses_schema_2_decode_packet() -> None:
    assert_decode(parse_datagram(decode_datagram(2)), 2)


def test_parses_schema_3_heartbeat_packet() -> None:
    data = (
        struct.pack(">III", MAGIC, 3, 0)
        + qbytearray("WSJT-X")
        + struct.pack(">I", 3)
        + qbytearray("2.7.0")
        + qbytearray("r1")
    )

    packet = parse_datagram(data)

    assert isinstance(packet, HeartbeatPacket)
    assert packet.header.schema == 3
    assert packet.max_schema == 3
    assert packet.version == "2.7.0"


def test_parses_schema_2_heartbeat_with_max_schema_field() -> None:
    data = (
        struct.pack(">III", MAGIC, 2, 0)
        + qbytearray("WSJT-X")
        + struct.pack(">I", 2)
        + qbytearray("2.6.1")
        + qbytearray("r2")
        + qbytearray("future trailing field")
    )

    packet = parse_datagram(data)

    assert isinstance(packet, HeartbeatPacket)
    assert packet.header.schema == 2
    assert packet.max_schema == 2
    assert packet.version == "2.6.1"


def test_parses_schema_2_heartbeat_without_max_schema_field() -> None:
    data = (
        struct.pack(">III", MAGIC, 2, 0)
        + qbytearray("WSJT-X")
        + qbytearray("2.3.1")
        + qbytearray("r3")
    )

    packet = parse_datagram(data)

    assert isinstance(packet, HeartbeatPacket)
    assert packet.header.schema == 2
    assert packet.max_schema == 2
    assert packet.version == "2.3.1"


def test_parses_schema_3_status_packet_with_optional_tail_absent() -> None:
    assert_status(parse_datagram(status_datagram(3)), 3)


def test_parses_schema_2_status_packet_with_optional_tail_absent() -> None:
    assert_status(parse_datagram(status_datagram(2)), 2)


def test_unknown_packet_type_is_safely_ignored() -> None:
    data = struct.pack(">III", MAGIC, 3, 99) + qbytearray("WSJT-X") + b"ignored"

    assert isinstance(parse_datagram(data), UnknownPacket)


@pytest.mark.parametrize("schema", [2, 3])
def test_serializes_reply_for_supported_schemas(schema: int) -> None:
    datagram = serialize_reply(
        schema,
        "WSJT-X",
        time(12, 34, 56, 789000),
        -8,
        0.2,
        1460,
        "~",
        "CQ OH2ZZ KP20",
        False,
        0,
    )

    packet = parse_datagram(datagram)

    assert isinstance(packet, ReplyPacket)
    assert packet.header.schema == schema
    assert packet.header.packet_type == 4
    assert packet.header.instance_id == "WSJT-X"
    assert packet.decode_time.isoformat() == "12:34:56.789000"
    assert packet.snr == -8
    assert packet.delta_time == 0.2
    assert packet.delta_frequency == 1460
    assert packet.mode == "~"
    assert packet.message == "CQ OH2ZZ KP20"
    assert not packet.low_confidence
    assert packet.modifiers == 0


@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("auto_tx_only", [False, True])
def test_serializes_halt_tx_for_supported_schemas(schema: int, auto_tx_only: bool) -> None:
    datagram = serialize_halt_tx(schema, "WSJT-X", auto_tx_only)
    packet = parse_datagram(datagram)

    assert isinstance(packet, HaltTxPacket)
    assert packet.header.schema == schema
    assert packet.header.packet_type == 8
    assert packet.header.instance_id == "WSJT-X"
    assert packet.auto_tx_only is auto_tx_only
    assert datagram == struct.pack(">III", MAGIC, schema, 8) + qbytearray("WSJT-X") + bytes([auto_tx_only])


def test_parses_clear_without_optional_window() -> None:
    data = struct.pack(">III", MAGIC, 2, 3) + qbytearray("WSJT-X")

    packet = parse_datagram(data)

    assert isinstance(packet, ClearPacket)
    assert packet.window is None


@pytest.mark.parametrize("schema", [2, 3])
def test_serializes_patched_set_tx_df(schema: int) -> None:
    datagram = serialize_set_tx_df(schema, "WSJT-X", 1740)
    packet = parse_datagram(datagram)
    assert isinstance(packet, SetTxDfPacket)
    assert packet.header.packet_type == 18
    assert packet.header.instance_id == "WSJT-X"
    assert packet.tx_df == 1740


@pytest.mark.parametrize("schema", [2, 3])
def test_parses_qso_logged_packet(schema: int) -> None:
    time_off = datetime(2026, 8, 24, 12, 34, 56, 789000, tzinfo=timezone.utc)
    time_on = datetime(2026, 8, 24, 12, 33, tzinfo=timezone.utc)
    data = struct.pack(">III", MAGIC, schema, 5) + qbytearray("WSJT-X")
    data += qdatetime(time_off)
    data += qbytearray("ON4ABC") + qbytearray("JO20") + struct.pack(">Q", 14_074_000)
    data += b"".join(qbytearray(value) for value in ("FT8", "-08", "-10", "50", "", "Operator"))
    data += qdatetime(time_on)
    data += b"".join(
        qbytearray(value)
        for value in ("F4NJU", "F4NJU", "JN18", "", "", "", "", "", "")
    )

    packet = parse_datagram(data)

    assert isinstance(packet, QsoLoggedPacket)
    assert packet.header.schema == schema
    assert packet.time_off == time_off
    assert packet.time_on == time_on
    assert packet.dx_call == "ON4ABC"
    assert packet.tx_frequency == 14_074_000
    assert packet.mode == "FT8"


def test_rejects_truncated_packet() -> None:
    with pytest.raises(ProtocolError):
        parse_datagram(struct.pack(">I", MAGIC))


def test_rejects_unknown_schema() -> None:
    data = struct.pack(">IIII", MAGIC, 99, 0, 0)

    with pytest.raises(ProtocolError, match="unsupported"):
        parse_datagram(data)
