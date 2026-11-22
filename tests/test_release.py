import importlib.util
import json
from hashlib import sha256
from pathlib import Path

import pytest

from wsjtx_autopilot import __version__
from wsjtx_autopilot.updates import (
    CHECKSUMS_NAME,
    INSTALLER_NAME,
    GitHubUpdateClient,
    ReleaseAsset,
    UpdateRelease,
    _checksum_for,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("ft8_release_tools", REPO_ROOT / "tools" / "release.py")
assert _SPEC is not None and _SPEC.loader is not None
release_tools = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_tools)


def test_single_version_source_is_consistent() -> None:
    assert release_tools.read_version(REPO_ROOT) == __version__
    assert release_tools.check_version_sources(REPO_ROOT) == __version__


def test_release_script_validates_the_final_artifact() -> None:
    script = (REPO_ROOT / "packaging" / "release.ps1").read_text(encoding="utf-8")

    assert "tools\\release.py' check-artifact --dist $distDir" in script


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


def _artifact_tree(tmp_path: Path, version: str = __version__) -> Path:
    bundle = tmp_path / release_tools.BUNDLE_NAME
    bundle.mkdir()
    (bundle / release_tools.APP_EXE_NAME).write_bytes(b"exe payload")
    (bundle / release_tools.BUILD_INFO_NAME).write_text(
        json.dumps(
            {
                "app_version": f"v{version}",
                "git_commit": "0123456789abcdef",
                "build_time": "2026-11-22T20:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / release_tools.INSTALLER_NAME).write_bytes(b"installer payload")
    release_tools.write_checksums(tmp_path)
    return bundle


def test_built_artifact_carries_the_source_version_and_a_valid_checksum(tmp_path: Path) -> None:
    _artifact_tree(tmp_path)

    details = release_tools.check_artifact(tmp_path)

    assert details["version"] == __version__
    assert details["git_commit"] == "0123456789abcdef"
    assert details["sha256"] == sha256(b"installer payload").hexdigest()


def test_artifact_with_a_foreign_version_is_rejected(tmp_path: Path) -> None:
    bundle = _artifact_tree(tmp_path, version="9.9.9")
    assert bundle.is_dir()

    with pytest.raises(release_tools.ReleaseError, match="differs from source version"):
        release_tools.check_artifact(tmp_path)


def test_artifact_without_build_info_is_rejected(tmp_path: Path) -> None:
    bundle = _artifact_tree(tmp_path)
    (bundle / release_tools.BUILD_INFO_NAME).unlink()

    with pytest.raises(release_tools.ReleaseError, match="Build info not found"):
        release_tools.check_artifact(tmp_path)


def test_release_checksums_are_accepted_by_the_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"installer payload"
    installer = tmp_path / INSTALLER_NAME
    installer.write_bytes(payload)
    checksums = release_tools.write_checksums(tmp_path).read_text(encoding="utf-8")
    client = GitHubUpdateClient(timeout_seconds=1.0)
    monkeypatch.setattr(client, "_text", lambda url: checksums)
    monkeypatch.setattr(client, "_download", lambda url, target: target.write_bytes(payload))
    release = UpdateRelease(
        version=__version__,
        release_url="https://example.invalid/release",
        notes="",
        installer=ReleaseAsset(INSTALLER_NAME, "https://example.invalid/setup.exe"),
        checksums=ReleaseAsset(CHECKSUMS_NAME, "https://example.invalid/checksums.txt"),
    )

    staged = client.download_verified_installer(release, tmp_path / "staging")

    assert staged.read_bytes() == payload
    assert sha256(staged.read_bytes()).hexdigest() == _checksum_for(checksums, INSTALLER_NAME)
