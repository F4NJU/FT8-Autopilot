from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from wsjtx_autopilot.updates import (
    CHECKSUMS_NAME,
    INSTALLER_NAME,
    NEW_ISSUE_URL,
    GitHubUpdateClient,
    ReleaseAsset,
    UpdateError,
    UpdateRelease,
    _checksum_for,
    feedback_issue_url,
    parse_latest_release,
    version_key,
)


def test_stable_github_release_with_verified_installer_is_offered() -> None:
    result = parse_latest_release(
        {
            "tag_name": "v1.1.0",
            "html_url": "https://github.com/F4NJU/FT8-Autopilot/releases/tag/v1.1.0",
            "body": "Correction AP1",
            "assets": [
                {"name": INSTALLER_NAME, "browser_download_url": "https://example.invalid/setup.exe"},
                {"name": CHECKSUMS_NAME, "browser_download_url": "https://example.invalid/checksums.txt"},
            ],
        },
        "v1.0.0",
    )

    assert result.available
    assert result.installable
    assert result.release is not None
    assert result.release.version == "1.1.0"


def test_feedback_issue_url_prefills_diagnostic_metadata() -> None:
    url = feedback_issue_url({"app_version": "v1.0.4", "os": "Windows-10", "python": "3.12.0"})
    parts = urlparse(url)
    query = parse_qs(parts.query)

    assert parts.scheme == "https"
    assert f"{parts.netloc}{parts.path}" == "github.com/F4NJU/FT8-Autopilot/issues/new"
    assert url.startswith(NEW_ISSUE_URL)
    assert query["title"] == ["[Bug / Suggestion] "]
    body = query["body"][0]
    assert "Version AutoPilot : v1.0.4" in body
    assert "Système : Windows-10" in body
    assert "Python : 3.12.0" in body
    assert "Étapes pour reproduire" in body


def test_feedback_issue_url_tolerates_missing_metadata() -> None:
    body = parse_qs(urlparse(feedback_issue_url({})).query)["body"][0]

    assert "Version AutoPilot : inconnue" in body
    assert "Système : inconnu" in body


def test_prerelease_and_unsupported_versions_are_never_offered() -> None:
    prerelease = parse_latest_release({"tag_name": "v1.1.0", "prerelease": True}, "1.0.0")

    assert not prerelease.available
    assert version_key("v1.1.0-rc1") is None
    assert version_key("1.1.0") == (1, 1, 0)


def test_checksum_file_requires_the_exact_installer_name() -> None:
    digest = "a" * 64
    contents = f"{digest} *{INSTALLER_NAME}\n{'b' * 64} other.exe\n"

    assert _checksum_for(contents, INSTALLER_NAME) == digest
    assert _checksum_for(contents, "other.exe") == "b" * 64


def _release(tmp_path: Path) -> UpdateRelease:
    return UpdateRelease(
        version="1.1.0",
        release_url="https://example.invalid/release",
        notes="Notes",
        installer=ReleaseAsset(INSTALLER_NAME, "https://example.invalid/setup.exe"),
        checksums=ReleaseAsset(CHECKSUMS_NAME, "https://example.invalid/checksums.txt"),
    )


def test_verified_installer_is_staged_with_its_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"installer payload"
    client = GitHubUpdateClient(timeout_seconds=1.0)
    monkeypatch.setattr(client, "_text", lambda url: f"{sha256(payload).hexdigest()} *{INSTALLER_NAME}\n")
    monkeypatch.setattr(client, "_download", lambda url, target: target.write_bytes(payload))

    installed = client.download_verified_installer(_release(tmp_path), tmp_path / "staging")

    assert installed.is_file()
    assert installed.read_bytes() == payload
    assert not installed.with_suffix(".exe.part").exists()


def test_checksum_mismatch_discards_the_partial_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GitHubUpdateClient(timeout_seconds=1.0)
    monkeypatch.setattr(client, "_text", lambda url: f"{'0' * 64} *{INSTALLER_NAME}\n")
    monkeypatch.setattr(
        client, "_download", lambda url, target: target.write_bytes(b"corrupted payload")
    )
    staging = tmp_path / "staging"

    with pytest.raises(UpdateError, match="ne correspond pas"):
        client.download_verified_installer(_release(tmp_path), staging)

    assert list(staging.iterdir()) == []


def test_installer_without_checksum_asset_is_never_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _release(tmp_path)
    incomplete = replace(release, checksums=None)
    client = GitHubUpdateClient(timeout_seconds=1.0)
    monkeypatch.setattr(client, "_download", lambda url, target: pytest.fail("download attempted"))

    with pytest.raises(UpdateError, match="pas l'installateur signé"):
        client.download_verified_installer(incomplete, tmp_path / "staging")


def test_check_returns_a_warning_when_github_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GitHubUpdateClient(timeout_seconds=1.0)
    monkeypatch.setattr(client, "_json", lambda url: (_ for _ in ()).throw(UpdateError("offline")))

    result = client.check("1.0.4")

    assert not result.available
    assert not result.installable
    assert result.warning == "offline"

