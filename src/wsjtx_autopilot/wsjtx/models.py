from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True, slots=True)
class PacketHeader:
    schema: int
    packet_type: int
    instance_id: str


@dataclass(frozen=True, slots=True)
class HeartbeatPacket:
    header: PacketHeader
    max_schema: int
    version: str
    revision: str


@dataclass(frozen=True, slots=True)
class StatusPacket:
    header: PacketHeader
    dial_frequency: int
    mode: str
    dx_call: str
    report: str
    tx_mode: str
    tx_enabled: bool
    transmitting: bool
    decoding: bool
    rx_df: int
    tx_df: int
    de_call: str
    de_grid: str
    dx_grid: str
    tx_watchdog: bool
    sub_mode: str
    fast_mode: bool
    special_operation_mode: int
    frequency_tolerance: int
    tr_period: int
    configuration_name: str
    tx_message: str


@dataclass(frozen=True, slots=True)
class DecodePacket:
    header: PacketHeader
    is_new: bool
    decode_time: time
    snr: int
    delta_time: float
    delta_frequency: int
    mode: str
    message: str
    low_confidence: bool
    off_air: bool


@dataclass(frozen=True, slots=True)
class ClearPacket:
    header: PacketHeader
    window: int | None


@dataclass(frozen=True, slots=True)
class ReplyPacket:
    header: PacketHeader
    decode_time: time
    snr: int
    delta_time: float
    delta_frequency: int
    mode: str
    message: str
    low_confidence: bool
    modifiers: int


@dataclass(frozen=True, slots=True)
class QsoLoggedPacket:
    header: PacketHeader
    time_off: datetime
    dx_call: str
    dx_grid: str
    tx_frequency: int
    mode: str
    report_sent: str
    report_received: str
    tx_power: str
    comments: str
    name: str
    time_on: datetime
    operator_call: str
    my_call: str
    my_grid: str
    exchange_sent: str
    exchange_received: str
    propagation_mode: str
    satellite: str = ""
    satellite_mode: str = ""
    rx_frequency: str = ""


@dataclass(frozen=True, slots=True)
class HaltTxPacket:
    header: PacketHeader
    auto_tx_only: bool


@dataclass(frozen=True, slots=True)
class SetTxPeriodPacket:
    header: PacketHeader
    tx_first: bool


@dataclass(frozen=True, slots=True)
class SetDialFrequencyPacket:
    header: PacketHeader
    frequency_hz: int


@dataclass(frozen=True, slots=True)
class TxAudioAttenuationStatePacket:
    header: PacketHeader
    attenuation: int


@dataclass(frozen=True, slots=True)
class UnknownPacket:
    header: PacketHeader


WsjtxPacket = (
    HeartbeatPacket
    | StatusPacket
    | DecodePacket
    | ClearPacket
    | ReplyPacket
    | QsoLoggedPacket
    | HaltTxPacket
    | SetTxPeriodPacket
    | SetDialFrequencyPacket
    | TxAudioAttenuationStatePacket
    | UnknownPacket
)
