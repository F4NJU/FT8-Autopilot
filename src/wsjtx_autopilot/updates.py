"""Safe GitHub release discovery and installer staging for Windows builds.

The radio-control process never performs network I/O here.  GUI code calls
this module from a short-lived worker thread, and a user confirmation remains
mandatory before an installer is launched.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GITHUB_RELEASES_URL = "https://api.github.com/repos/F4NJU/FT8-Autopilot/releases/latest"
RELEASES_PAGE_URL = "https://github.com/F4NJU/FT8-Autopilot/releases"
INSTALLER_NAME = "WSJTX-AutoPilot-Setup.exe"
CHECKSUMS_NAME = "SHA256SUMS.txt"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)


class UpdateError(RuntimeError):
    """The release source could not provide a safe, usable update."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    download_url: str


@dataclass(frozen=True, slots=True)
class UpdateRelease:
    version: str
    release_url: str
    notes: str
    installer: ReleaseAsset | None
    checksums: ReleaseAsset | None


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    current_version: str
    release: UpdateRelease | None = None
    warning: str = ""

    @property
    def available(self) -> bool:
        return self.release is not None

    @property
    def installable(self) -> bool:
        return bool(self.release and self.release.installer and self.release.checksums)


def version_key(value: str) -> tuple[int, int, int] | None:
    """Return a comparable stable SemVer tuple, rejecting prereleases."""

    match = _VERSION.fullmatch(str(value).strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def parse_latest_release(payload: object, current_version: str) -> UpdateCheckResult:
    """Validate GitHub's release JSON before it reaches any UI decision."""

    if not isinstance(payload, dict):
        return UpdateCheckResult(current_version, warning="Réponse GitHub invalide.")
    if payload.get("draft") or payload.get("prerelease"):
        return UpdateCheckResult(current_version, warning="La dernière release GitHub n'est pas stable.")
    latest = version_key(str(payload.get("tag_name", "")))
    current = version_key(current_version)
    if latest is None or current is None:
        return UpdateCheckResult(current_version, warning="Version GitHub ou version locale non reconnue.")
    if latest <= current:
        return UpdateCheckResult(current_version)
    assets: dict[str, ReleaseAsset] = {}
    raw_assets = payload.get("assets", [])
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("browser_download_url", "")).strip()
            if name and url.startswith("https://"):
                assets[name.casefold()] = ReleaseAsset(name, url)
    version = str(payload.get("tag_name", "")).strip().lstrip("vV")
    return UpdateCheckResult(
        current_version,
        UpdateRelease(
            version=version,
            release_url=str(payload.get("html_url", RELEASES_PAGE_URL)).strip() or RELEASES_PAGE_URL,
            notes=str(payload.get("body", "")).strip(),
            installer=assets.get(INSTALLER_NAME.casefold()),
            checksums=assets.get(CHECKSUMS_NAME.casefold()),
        ),
    )


class GitHubUpdateClient:
    """Small standard-library client; no token and no telemetry are used."""

    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def check(self, current_version: str) -> UpdateCheckResult:
        try:
            payload = self._json(GITHUB_RELEASES_URL)
        except UpdateError as exc:
            return UpdateCheckResult(current_version, warning=str(exc))
        return parse_latest_release(payload, current_version)

    def download_verified_installer(self, release: UpdateRelease, destination_dir: Path) -> Path:
        if release.installer is None or release.checksums is None:
            raise UpdateError("La release ne fournit pas l'installateur signé par somme SHA-256.")
        checksums = self._text(release.checksums.download_url)
        expected = _checksum_for(checksums, release.installer.name)
        if expected is None:
            raise UpdateError("La somme SHA-256 de l'installateur est absente de la release.")
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / release.installer.name
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            self._download(release.installer.download_url, temporary)
            actual = _sha256_file(temporary)
            if actual.casefold() != expected.casefold():
                raise UpdateError("La somme SHA-256 de la mise à jour ne correspond pas.")
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _json(self, url: str) -> object:
        try:
            with urlopen(_request(url), timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError(f"Vérification GitHub indisponible : {exc}") from exc

    def _text(self, url: str) -> str:
        try:
            with urlopen(_request(url), timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, OSError, UnicodeDecodeError) as exc:
            raise UpdateError(f"Téléchargement des sommes SHA-256 impossible : {exc}") from exc

    def _download(self, url: str, target: Path) -> None:
        try:
            with urlopen(_request(url), timeout=self.timeout_seconds) as response, target.open("wb") as output:
                while chunk := response.read(128 * 1024):
                    output.write(chunk)
        except (HTTPError, URLError, OSError) as exc:
            raise UpdateError(f"Téléchargement de la mise à jour impossible : {exc}") from exc


def _request(url: str) -> Request:
    return Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "WSJTX-AutoPilot-Updater"})


def _checksum_for(contents: str, filename: str) -> str | None:
    for line in contents.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, listed_name = parts
        if listed_name.lstrip("*").strip() == filename and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest
    return None


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

