"""Safe first-run migration from a normal WSJT-X profile to the bundled AP1 one."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


@dataclass(frozen=True, slots=True)
class WsjtxProfileMigration:
    state: str
    message: str


def migrate_existing_wsjtx_profile(
    local_appdata: Path | None = None,
) -> WsjtxProfileMigration:
    """Seed AP1's settings once, without changing a user's WSJT-X profile.

    WSJT-X and the AP1 build deliberately have different Qt application names,
    hence different configuration directories. Only the preferences file is
    copied: logs, decodes, databases and recordings remain where they are.
    """

    root = local_appdata or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    source = root / "WSJT-X" / "WSJT-X.ini"
    target = root / "WSJT-X - AP1" / "WSJT-X - AP1.ini"
    if target.is_file():
        return WsjtxProfileMigration("existing", "Le profil AP1 existant est conservé.")
    if not source.is_file():
        return WsjtxProfileMigration("nothing_to_copy", "Aucun profil WSJT-X existant à reprendre.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError as exc:
        return WsjtxProfileMigration("failed", f"Copie de la configuration WSJT-X impossible : {exc}")
    return WsjtxProfileMigration(
        "migrated",
        "Configuration WSJT-X reprise dans le profil AP1 ; le profil d'origine est inchangé.",
    )

