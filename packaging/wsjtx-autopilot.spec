# Build from the repository root with: pyinstaller packaging/wsjtx-autopilot.spec
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PROJECT_ROOT = Path(SPECPATH).parent
try:
    GIT_COMMIT = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
except (OSError, subprocess.CalledProcessError):
    GIT_COMMIT = "unknown"
BUILD_INFO_PATH = Path(tempfile.gettempdir()) / "ft8-autopilot-build-info.json"
version_source = (PROJECT_ROOT / "src" / "wsjtx_autopilot" / "__init__.py").read_text(
    encoding="utf-8"
)
version_match = re.search(r'^__version__\s*=\s*"([^"]+)"', version_source, re.MULTILINE)
if version_match is None:
    raise SystemExit("Unable to determine AutoPilot version from __init__.py")
APP_VERSION = f"v{version_match.group(1)}"
BUILD_INFO_PATH.write_text(
    json.dumps(
        {
            "app_version": os.environ.get("FT8_AUTOPILOT_VERSION")
            or os.environ.get("GITHUB_REF_NAME")
            or APP_VERSION,
            "git_commit": os.environ.get("FT8_AUTOPILOT_GIT_COMMIT")
            or os.environ.get("GITHUB_SHA")
            or GIT_COMMIT,
            "build_time": datetime.now(timezone.utc).isoformat(),
        }
    ),
    encoding="utf-8",
)


def bundled_ap1_data() -> list[tuple[str, str]]:
    """Collect a complete AP1 runtime when the integrated release is built."""

    raw_runtime = os.environ.get("WSJTX_AP1_RUNTIME_DIR", "").strip()
    required = os.environ.get("FT8_REQUIRE_AP1_BUNDLE", "").strip() == "1"
    if not raw_runtime:
        if required:
            raise SystemExit("FT8_REQUIRE_AP1_BUNDLE=1 requires WSJTX_AP1_RUNTIME_DIR")
        return []
    runtime = Path(raw_runtime).expanduser().resolve()
    executable = runtime / "bin" / "wsjtx.exe"
    if not executable.is_file():
        raise SystemExit(f"Invalid AP1 runtime: missing {executable}")
    source_url = os.environ.get("WSJTX_AP1_SOURCE_URL", "").strip()
    if required and not source_url:
        raise SystemExit("FT8_REQUIRE_AP1_BUNDLE=1 requires WSJTX_AP1_SOURCE_URL")
    notice = Path(tempfile.gettempdir()) / "AP1-SOURCE.txt"
    notice.write_text(
        "Bundled FT8 engine: WSJT-X Improved / AP1\n"
        f"Runtime source directory: {runtime.name}\n"
        f"Corresponding source: {source_url or 'not bundled in this developer build'}\n"
        "AutoPilot AP1 changes: patches/wsjtx-3.2-direct-reply.patch in the AutoPilot source tree.\n"
        "Licence: GNU GPL version 3 or later.\n",
        encoding="utf-8",
    )
    files = [
        (str(path), str(Path("wsjtx-ap1") / path.relative_to(runtime).parent))
        for path in runtime.rglob("*")
        if path.is_file()
    ]
    files.append((str(notice), "wsjtx-ap1"))
    return files


DATA_FILES = [
    (str(BUILD_INFO_PATH), "."),
    (str(PROJECT_ROOT / "LICENSE"), "licenses"),
    (
        str(PROJECT_ROOT / "src" / "wsjtx_autopilot" / "gui" / "assets" / "naturalearth-110m-countries.geojson"),
        "wsjtx_autopilot/gui/assets",
    ),
    *bundled_ap1_data(),
]

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "gui_entry.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Qt's Windows build imports the unsuffixed ICU API provided by Windows.
# A foreign ICU build found on PATH (for example Poppler's versioned ICU)
# exports different symbols and makes QtWidgets fail with "procedure not found".
a.binaries = [
    binary for binary in a.binaries
    if Path(binary[0]).name.lower() not in {"icuuc.dll", "icudt78.dll"}
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WSJTX-AutoPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="WSJTX-AutoPilot",
)

