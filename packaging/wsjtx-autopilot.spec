# Build from the repository root with: pyinstaller packaging/wsjtx-autopilot.spec
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
BUILD_INFO_PATH.write_text(
    json.dumps(
        {
            "app_version": os.environ.get("FT8_AUTOPILOT_VERSION")
            or os.environ.get("GITHUB_REF_NAME")
            or "v0.1.0-wip.5",
            "git_commit": os.environ.get("FT8_AUTOPILOT_GIT_COMMIT")
            or os.environ.get("GITHUB_SHA")
            or GIT_COMMIT,
            "build_time": datetime.now(timezone.utc).isoformat(),
        }
    ),
    encoding="utf-8",
)

a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "gui_entry.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=[(str(BUILD_INFO_PATH), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
