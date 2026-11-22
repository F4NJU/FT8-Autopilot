import importlib.util
from hashlib import sha256
from pathlib import Path

import pytest

from wsjtx_autopilot import __version__
from wsjtx_autopilot.updates import _checksum_for

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("ft8_release_tools", REPO_ROOT / "tools" / "release.py")
assert _SPEC is not None and _SPEC.loader is not None
release_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_tools)


def test_single_version_source_is_consistent() -> None:
    assert release_tools.read_version(REPO_ROOT) == __version__
    assert release_tools.check_version_sources(REPO_ROOT) == __version__


def test_checksums_are_generated_and_verified_with_the_updater_parser(tmp_path: Path) -> None:
    installer = tmp_path / release_tools.INSTALLER_NAME
    installer.write_bytes(b"installer payload")
    expected = sha256(installer.read_bytes()).hexdigest()

    target = release_tools.write_checksums(tmp_path)

    assert target.read_text(encoding="utf-8") == f"{expected} *{installer.name}\n"
    assert _checksum_for(target.read_text(encoding="utf-8"), installer.name) == expected
    assert release_tools.verify_checksums(tmp_path) == expected


def test_corrupted_checksum_is_rejected(tmp_path: Path) -> None:
    installer = tmp_path / release_tools.INSTALLER_NAME
    installer.write_bytes(b"installer payload")
    release_tools.write_checksums(tmp_path)
    (tmp_path / release_tools.CHECKSUMS_NAME).write_text(
        f"{'0' * 64} *{installer.name}\n", encoding="utf-8"
    )

    with pytest.raises(release_tools.ReleaseError, match="SHA-256 mismatch"):
        release_tools.verify_checksums(tmp_path)


def test_missing_checksum_entry_is_rejected(tmp_path: Path) -> None:
    installer = tmp_path / release_tools.INSTALLER_NAME
    installer.write_bytes(b"installer payload")
    (tmp_path / release_tools.CHECKSUMS_NAME).write_text(
        f"{'a' * 64} *other.exe\n", encoding="utf-8"
    )

    with pytest.raises(release_tools.ReleaseError, match="no valid entry"):
        release_tools.verify_checksums(tmp_path)


def test_missing_installer_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(release_tools.ReleaseError, match="Installer not found"):
        release_tools.write_checksums(tmp_path)
