"""Small length-prefixed format for recording and replaying UDP datagrams."""

import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CAPTURE_MAGIC = b"WSJTX-CAPTURE\x01"
_FRAME = struct.Struct(">dI")
_MAX_DATAGRAM_SIZE = 65_535


class CaptureError(ValueError):
    """Raised when a capture file is malformed."""


@dataclass(frozen=True, slots=True)
class CapturedDatagram:
    timestamp: float
    data: bytes


class DatagramRecorder:
    def __init__(self, path: Path) -> None:
        self._file: BinaryIO = path.open("wb")
        self._file.write(CAPTURE_MAGIC)
        self._file.flush()

    def __call__(self, data: bytes) -> None:
        self.record(data)

    def record(self, data: bytes, timestamp: float | None = None) -> None:
        if len(data) > _MAX_DATAGRAM_SIZE:
            raise ValueError("datagram exceeds UDP maximum size")
        self._file.write(_FRAME.pack(timestamp if timestamp is not None else time.time(), len(data)))
        self._file.write(data)
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "DatagramRecorder":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def replay_datagrams(path: Path) -> Iterator[CapturedDatagram]:
    """Yield captured datagrams with their original wall-clock timestamps."""
    with path.open("rb") as capture:
        if capture.read(len(CAPTURE_MAGIC)) != CAPTURE_MAGIC:
            raise CaptureError("invalid WSJT-X capture header")
        while frame := capture.read(_FRAME.size):
            if len(frame) != _FRAME.size:
                raise CaptureError("truncated WSJT-X capture frame")
            timestamp, length = _FRAME.unpack(frame)
            if length > _MAX_DATAGRAM_SIZE:
                raise CaptureError("invalid captured datagram size")
            data = capture.read(length)
            if len(data) != length:
                raise CaptureError("truncated captured datagram")
            yield CapturedDatagram(timestamp, data)
