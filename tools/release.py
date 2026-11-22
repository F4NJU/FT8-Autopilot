"""Release helpers with one version source and one checksum format.

The published checksum file must stay byte-compatible with the updater in
``wsjtx_autopilot.updates``.  This module is used by the release script and by
CI; it never performs network I/O or launches an installer.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_NAME = "WSJTX-AutoPilot-Setup.exe"
CHECKSUMS_NAME = "SHA256SUMS.txt"
_VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"', re.MULTILINE)


class ReleaseError(RuntimeError):
    """A release input is missing or inconsistent."""


def version_file(root: Path = ROOT) -> Path:
    return root / "src" / "wsjtx_autopilot" / "__init__.py"


def read_version(root: Path = ROOT) -> str:
    source = version_file(root).read_text(encoding="utf-8")
    match = _VERSION_ASSIGNMENT.search(source)
    if match is None:
        raise ReleaseError(f"Cannot read __version__ from {version_file(root)}")
    return match.group(1)


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_checksums(installer_path: Path) -> str:
    """Render the exact ``SHA256SUMS.txt`` line expected by the updater."""

    return f"{sha256_file(installer_path)} *{installer_path.name}\n"


def write_checksums(
    dist_dir: Path,
    installer_name: str = INSTALLER_NAME,
) -> Path:
    installer = dist_dir / installer_name
    if not installer.is_file():
        raise ReleaseError(f"Installer not found: {installer}")
    target = dist_dir / CHECKSUMS_NAME
    target.write_text(render_checksums(installer), encoding="utf-8", newline="\n")
    return target


def verify_checksums(
    dist_dir: Path,
    installer_name: str = INSTALLER_NAME,
) -> str:
    """Verify a generated checksum file with the updater's own parser."""

    checksums = dist_dir / CHECKSUMS_NAME
    installer = dist_dir / installer_name
    if not checksums.is_file():
        raise ReleaseError(f"Checksum file not found: {checksums}")
    if not installer.is_file():
        raise ReleaseError(f"Installer not found: {installer}")
    sys.path.insert(0, str(ROOT / "src"))
    from wsjtx_autopilot.updates import _checksum_for  # noqa: PLC0415

    expected = _checksum_for(checksums.read_text(encoding="utf-8"), installer_name)
    if expected is None:
        raise ReleaseError(f"{CHECKSUMS_NAME} has no valid entry for {installer_name}")
    actual = sha256_file(installer)
    if actual.casefold() != expected.casefold():
        raise ReleaseError(f"SHA-256 mismatch for {installer_name}")
    return actual


def check_version_sources(root: Path = ROOT) -> str:
    """Fail when a second hard-coded version reappears in build inputs."""

    version = read_version(root)
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    if 'dynamic = ["version"]' not in pyproject:
        raise ReleaseError("pyproject.toml must declare version as dynamic")
    if re.search(r"^version\s*=\s*[\"']", pyproject, re.MULTILINE):
        raise ReleaseError("pyproject.toml still contains a static version")
    if 'version = {attr = "wsjtx_autopilot.__version__"}' not in pyproject:
        raise ReleaseError("pyproject.toml must derive its version from __init__.py")

    iss = (root / "packaging" / "WSJTX-AutoPilot.iss").read_text(encoding="utf-8")
    if "AppVersion={#AppVersion}" not in iss:
        raise ReleaseError("Inno Setup script must use the AppVersion define")
    if re.search(r"^AppVersion=\d", iss, re.MULTILINE):
        raise ReleaseError("Inno Setup script still contains a hard-coded version")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the single source version")
    sub.add_parser("check", help="verify that build inputs share one version source")
    for command in ("checksums", "verify"):
        item = sub.add_parser(command, help=f"{command} the release installer")
        item.add_argument("--dist", type=Path, default=ROOT / "dist")
        item.add_argument("--installer", default=INSTALLER_NAME)
    args = parser.parse_args(argv)

    try:
        if args.command == "version":
            print(read_version())
        elif args.command == "check":
            print(check_version_sources())
        elif args.command == "checksums":
            print(write_checksums(args.dist, args.installer))
        else:
            print(verify_checksums(args.dist, args.installer))
    except (OSError, ReleaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
