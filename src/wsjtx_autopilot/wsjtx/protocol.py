"""Reader for documented WSJT-X NetworkMessage datagrams.

WSJT-X serializes fields with a big-endian Qt QDataStream. The reader supports
the packet types used by observation, control, and QSO lifecycle tracking.
"""

import struct
from datetime import date, datetime, time, timedelta, timezone

from .models import (
    ClearPacket,
    DecodePacket,
    HeartbeatPacket,
    HaltTxPacket,
    PacketHeader,
    QsoLoggedPacket,
    ReplyPacket,
    SetTxPeriodPacket,
    SetDialFrequencyPacket,
    TxAudioAttenuationStatePacket,
    StatusPacket,
    UnknownPacket,
    WsjtxPacket,
)

MAGIC = 0xADBCCBDA
# Schema 2 uses Qt_5_2 and schema 3 uses Qt_5_4. The packet fields parsed here
# (integers, bool, QTime, double, and QByteArray) have the same wire encoding.
SUPPORTED_SCHEMAS = frozenset({2, 3})


class ProtocolError(ValueError):
    """Raised when a datagram is malformed or unsupported."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def _unpack(self, fmt: str) -> int | float:
        size = struct.calcsize(fmt)
        if self.offset + size > len(self.data):
            raise ProtocolError("truncated WSJT-X datagram")
        value = struct.unpack_from(fmt, self.data, self.offset)[0]
        self.offset += size
        return value

    def u8(self) -> int:
        return int(self._unpack(">B"))

    def u16(self) -> int:
        return int(self._unpack(">H"))

    def u32(self) -> int:
        return int(self._unpack(">I"))

    def i32(self) -> int:
        return int(self._unpack(">i"))

    def i64(self) -> int:
        return int(self._unpack(">q"))

    def u64(self) -> int:
        return int(self._unpack(">Q"))

    def f64(self) -> float:
        return float(self._unpack(">d"))

    def boolean(self) -> bool:
        value = self.u8()
        if value not in (0, 1):
            raise ProtocolError("invalid boolean value")
        return bool(value)

    def byte_array(self) -> bytes:
        length = self.u32()
        if length == 0xFFFFFFFF:
            return b""
        if self.offset + length > len(self.data):
            raise ProtocolError("truncated byte array")
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value

    def utf8(self) -> str:
        raw = self.byte_array()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid UTF-8 field") from exc

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def optional_boolean(self, default: bool = False) -> bool:
        if not self.remaining():
            return default
        return self.boolean()

    def optional_u8(self, default: int = 0) -> int:
        if not self.remaining():
            return default
        return self.u8()

    def optional_u32(self, default: int = 0) -> int:
        if not self.remaining():
            return default
        return self.u32()

    def optional_utf8(self) -> str:
        if not self.remaining():
            return ""
        return self.utf8()

    def qdatetime(self) -> datetime:
        """Read the Qt 5.2/5.4 QDateTime representation used by WSJT-X."""
        julian_day = self.i64()
        milliseconds = self.u32()
        time_spec = self.u8()
        if milliseconds >= 86_400_000:
            raise ProtocolError("invalid QDateTime time")
        try:
            calendar_date = date.fromordinal(julian_day - 1_721_425)
        except ValueError as exc:
            raise ProtocolError("invalid QDateTime date") from exc
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        if time_spec == 0:
            tz = None
        elif time_spec == 1:
            tz = timezone.utc
        elif time_spec == 2:
            tz = timezone(timedelta(seconds=self.i32()))
        elif time_spec == 3:
            raise ProtocolError("named-timezone QDateTime is not supported")
        else:
            raise ProtocolError("invalid QDateTime time spec")
        return datetime.combine(calendar_date, time(hours, minutes, seconds, millis * 1000), tz)


def parse_datagram(data: bytes) -> WsjtxPacket:
    """Parse one WSJT-X UDP datagram, rejecting unknown packet types."""
    reader = _Reader(data)
    if reader.u32() != MAGIC:
        raise ProtocolError("invalid WSJT-X magic number")
    schema = reader.u32()
    if schema not in SUPPORTED_SCHEMAS:
        raise ProtocolError(f"unsupported WSJT-X schema {schema}")
    packet_type = reader.u32()
    try:
        instance_id = reader.byte_array().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid instance identifier") from exc
    header = PacketHeader(schema, packet_type, instance_id)

    if packet_type == 0:
        start = reader.offset
        try:
            # Maximum schema was introduced with schema 3, but some senders
            # include this backward-compatible field in a schema 2 datagram.
            max_schema = reader.u32()
            version = reader.utf8()
            revision = reader.utf8()
        except ProtocolError:
            reader.offset = start
            max_schema = 2
            version = reader.utf8()
            revision = reader.utf8()
        return HeartbeatPacket(header, max_schema, version, revision)
    if packet_type == 1:
        dial_frequency = reader.u64()
        mode = reader.utf8()
        dx_call = reader.utf8()
        report = reader.utf8()
        tx_mode = reader.utf8()
        tx_enabled = reader.boolean()
        transmitting = reader.boolean()
        decoding = reader.boolean()
        rx_df = reader.u32()
        tx_df = reader.u32()
        de_call = reader.utf8()
        de_grid = reader.utf8()
        dx_grid = reader.utf8()
        return StatusPacket(
            header=header,
            dial_frequency=dial_frequency,
            mode=mode,
            dx_call=dx_call,
            report=report,
            tx_mode=tx_mode,
            tx_enabled=tx_enabled,
            transmitting=transmitting,
            decoding=decoding,
            rx_df=rx_df,
            tx_df=tx_df,
            de_call=de_call,
            de_grid=de_grid,
            dx_grid=dx_grid,
            tx_watchdog=reader.optional_boolean(),
            sub_mode=reader.optional_utf8(),
            fast_mode=reader.optional_boolean(),
            special_operation_mode=reader.optional_u8(),
            frequency_tolerance=reader.optional_u32(0xFFFFFFFF),
            tr_period=reader.optional_u32(0xFFFFFFFF),
            configuration_name=reader.optional_utf8(),
            tx_message=reader.optional_utf8(),
        )
    if packet_type == 2:
        is_new = reader.boolean()
        milliseconds = reader.u32()
        if milliseconds >= 86_400_000:
            raise ProtocolError("invalid decode time")
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return DecodePacket(
            header=header,
            is_new=is_new,
            decode_time=time(hours, minutes, seconds, millis * 1000),
            snr=reader.i32(),
            delta_time=reader.f64(),
            delta_frequency=reader.u32(),
            mode=reader.utf8(),
            message=reader.utf8(),
            low_confidence=reader.optional_boolean(),
            off_air=reader.optional_boolean(),
        )
    if packet_type == 3:
        return ClearPacket(header, reader.u8() if reader.remaining() else None)
    if packet_type == 4:
        milliseconds = reader.u32()
        if milliseconds >= 86_400_000:
            raise ProtocolError("invalid reply time")
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return ReplyPacket(
            header=header,
            decode_time=time(hours, minutes, seconds, millis * 1000),
            snr=reader.i32(),
            delta_time=reader.f64(),
            delta_frequency=reader.u32(),
            mode=reader.utf8(),
            message=reader.utf8(),
            low_confidence=reader.boolean(),
            modifiers=reader.u8(),
        )
    if packet_type == 5:
        time_off = reader.qdatetime()
        dx_call = reader.utf8()
        dx_grid = reader.utf8()
        tx_frequency = reader.u64()
        mode = reader.utf8()
        report_sent = reader.utf8()
        report_received = reader.utf8()
        tx_power = reader.utf8()
        comments = reader.utf8()
        name = reader.utf8()
        time_on = reader.qdatetime()
        operator_call = reader.utf8()
        my_call = reader.utf8()
        my_grid = reader.utf8()
        exchange_sent = reader.utf8()
        exchange_received = reader.utf8()
        propagation_mode = reader.utf8()
        return QsoLoggedPacket(
            header,
            time_off,
            dx_call,
            dx_grid,
            tx_frequency,
            mode,
            report_sent,
            report_received,
            tx_power,
            comments,
            name,
            time_on,
            operator_call,
            my_call,
            my_grid,
            exchange_sent,
            exchange_received,
            propagation_mode,
            reader.optional_utf8(),
            reader.optional_utf8(),
            reader.optional_utf8(),
        )
    if packet_type == 8:
        return HaltTxPacket(header, reader.boolean())
    if packet_type == 19:
        return SetTxPeriodPacket(header, reader.boolean())
    if packet_type == 20:
        return SetDialFrequencyPacket(header, reader.u64())
    if packet_type == 23:
        return TxAudioAttenuationStatePacket(header, reader.u16())
    return UnknownPacket(header)


def serialize_reply(
    schema: int,
    instance_id: str,
    decode_time: time,
    snr: int,
    delta_time: float,
    delta_frequency: int,
    mode: str,
    message: str,
    low_confidence: bool,
    modifiers: int = 0,
) -> bytes:
    """Serialize documented NetworkMessage Reply (type 4)."""
    if schema not in SUPPORTED_SCHEMAS:
        raise ProtocolError(f"unsupported WSJT-X schema {schema}")
    if not 0 <= modifiers <= 0xFF:
        raise ProtocolError("invalid Reply modifiers")

    def utf8(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack(">I", len(encoded)) + encoded

    milliseconds = (
        decode_time.hour * 3_600_000
        + decode_time.minute * 60_000
        + decode_time.second * 1_000
        + decode_time.microsecond // 1_000
    )
    return (
        struct.pack(">III", MAGIC, schema, 4)
        + utf8(instance_id)
        + struct.pack(">IidI", milliseconds, snr, delta_time, delta_frequency)
        + utf8(mode)
        + utf8(message)
        + struct.pack(">BB", int(low_confidence), modifiers)
    )


def serialize_halt_tx(schema: int, instance_id: str, auto_tx_only: bool = False) -> bytes:
    """Serialize documented NetworkMessage Halt Tx (type 8)."""
    if schema not in SUPPORTED_SCHEMAS:
        raise ProtocolError(f"unsupported WSJT-X schema {schema}")
    encoded_id = instance_id.encode("utf-8")
    return (
        struct.pack(">III", MAGIC, schema, 8)
        + struct.pack(">I", len(encoded_id))
        + encoded_id
        + struct.pack(">B", int(auto_tx_only))
    )


def serialize_set_tx_period(schema: int, instance_id: str, tx_first: bool) -> bytes:
    """Serialize the AP1 SetTxPeriod extension (type 19)."""
    if schema not in SUPPORTED_SCHEMAS:
        raise ProtocolError(f"unsupported WSJT-X schema {schema}")
    encoded_id = instance_id.encode("utf-8")
    return (
        struct.pack(">III", MAGIC, schema, 19)
        + struct.pack(">I", len(encoded_id))
        + encoded_id
        + struct.pack(">B", int(tx_first))
    )


def serialize_set_dial_frequency(schema: int, instance_id: str, frequency_hz: int) -> bytes:
    """Serialize the AP1 SetDialFrequency extension (type 20)."""
    if schema not in SUPPORTED_SCHEMAS:
        raise ProtocolError(f"unsupported WSJT-X schema {schema}")
    if not 0 <= frequency_hz <= 0xFFFFFFFFFFFFFFFF:
        raise ProtocolError("invalid dial frequency")
    encoded_id = instance_id.encode("utf-8")
    return (
        struct.pack(">III", MAGIC, schema, 20)
        + struct.pack(">I", len(encoded_id))
        + encoded_id
        + struct.pack(">Q", frequency_hz)
    )


def serialize_set_tx_audio_attenuation(schema: int, instance_id: str, attenuation: int) -> bytes:
    if schema not in SUPPORTED_SCHEMAS or not 0 <= attenuation <= 450:
        raise ProtocolError("invalid TX audio attenuation")
    encoded_id = instance_id.encode("utf-8")
    return struct.pack(">III", MAGIC, schema, 21) + struct.pack(">I", len(encoded_id)) + encoded_id + struct.pack(">H", attenuation)


def serialize_query_tx_audio_attenuation(schema: int, instance_id: str) -> bytes:
    if schema not in SUPPORTED_SCHEMAS:
        raise ProtocolError(f"unsupported WSJT-X schema {schema}")
    encoded_id = instance_id.encode("utf-8")
    return struct.pack(">III", MAGIC, schema, 22) + struct.pack(">I", len(encoded_id)) + encoded_id
