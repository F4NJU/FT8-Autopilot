import logging
import socket
from collections.abc import Iterator
from collections.abc import Callable
from dataclasses import dataclass

from .models import WsjtxPacket
from .protocol import ProtocolError, parse_datagram

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReceivedPacket:
    packet: WsjtxPacket
    endpoint: tuple[str, int]


class UdpListener:
    def __init__(
        self,
        bind_address: str,
        port: int,
        timeout: float = 0.2,
        record: Callable[[bytes], None] | None = None,
    ) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(timeout)
        self._socket.bind((bind_address, port))
        self._record = record

    def packets(self) -> Iterator[ReceivedPacket | None]:
        """Yield packets, or None on idle ticks so the engine can run timers."""
        while True:
            yield self.receive()

    def receive(self) -> ReceivedPacket | None:
        """Receive at most one packet, returning None when the socket is idle."""
        try:
            data, source = self._socket.recvfrom(65_535)
        except (TimeoutError, BlockingIOError):
            return None
        if self._record is not None:
            self._record(data)
        try:
            packet = parse_datagram(data)
        except ProtocolError:
            LOGGER.exception("Rejected malformed WSJT-X datagram")
            return None
        LOGGER.debug("Received WSJT-X packet type=%d", packet.header.packet_type)
        return ReceivedPacket(packet, (str(source[0]), int(source[1])))

    def sendto(self, data: bytes, endpoint: tuple[str, int]) -> int:
        return self._socket.sendto(data, endpoint)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "UdpListener":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
