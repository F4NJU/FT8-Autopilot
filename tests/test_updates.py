from wsjtx_autopilot.updates import CHECKSUMS_NAME, INSTALLER_NAME, _checksum_for, parse_latest_release, version_key


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

