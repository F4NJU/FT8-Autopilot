from pathlib import Path

import pytest

from wsjtx_autopilot.wsjtx.capture import CaptureError, DatagramRecorder, replay_datagrams


def test_records_and_replays_timestamped_datagrams(tmp_path: Path) -> None:
    path = tmp_path / "wsjtx.capture"
    with DatagramRecorder(path) as recorder:
        recorder.record(b"first", 100.0)
        recorder.record(b"second", 101.5)

    frames = list(replay_datagrams(path))

    assert [(frame.timestamp, frame.data) for frame in frames] == [(100.0, b"first"), (101.5, b"second")]


def test_rejects_invalid_capture(tmp_path: Path) -> None:
    path = tmp_path / "invalid.capture"
    path.write_bytes(b"not a capture")

    with pytest.raises(CaptureError):
        list(replay_datagrams(path))
