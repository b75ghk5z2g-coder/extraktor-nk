"""
Stable, writable, per-user directory for this appka's persisted data
(history.json, slovlex_history.json), INDEPENDENT of where the app
bundle/executable itself happens to be running from.

WHY THIS EXISTS: on macOS, a downloaded, unsigned .app that hasn't been
moved out of its quarantined location gets launched by macOS "App
Translocation" from a randomized, READ-ONLY copy (a path under
/private/var/folders/.../AppTranslocation/...) instead of its real location
-- confirmed live 2026-07-27 via the exact error this produces: writing
history next to the backend's own source files (the original design) then
fails with "[Errno 30] Read-only file system". This only affects the .app
bundle specifically -- the .command/.bat launcher scripts are not
translocated -- which is why this wasn't caught until the .app was actually
used. Storing data in a stable per-user OS location sidesteps the problem
regardless of where/how the app is launched from, rather than relying on
the user always moving the app out of quarantine first.
"""

import os
import sys
from pathlib import Path


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "NKU Extraktor"
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        base = (Path(appdata) if appdata else Path.home()) / "NKU Extraktor"
    else:
        base = Path.home() / ".nku-extraktor"
    base.mkdir(parents=True, exist_ok=True)
    return base
