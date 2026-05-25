"""Suite-wide path resolution for log files.

Single source of truth for *where* logs land. Used by gateway.py,
hal_reader.py, hal_watchdog.py — and shellable from the lcnc-suite
launcher (`python3 -c "import lcnc_paths; print(lcnc_paths.resolve()[0])"`)
so the bash side and Python side cannot disagree.

Precedence (first hit wins):
  1. LCNC_WEBUI_LOG_DIR env var
  2. INI [DISPLAY] WEBUI_LOG_DIR (read via configparser from
     LCNC_INI_FILE env — cannot use `linuxcnc.ini` because
     hal_watchdog.py starts before STAT exists)
  3. Default: ~/linuxcnc/lcnc-suite/logs/
  4. Fallback: /tmp/lcnc-suite/ if the resolved dir is missing/
     unwritable OR free disk < 500 MB (SD-card targets)

resolve() never raises. It returns `(path, fallback_reason)` where
`fallback_reason` is None if the requested path was honored, or a short
string like "permission_denied" / "low_disk" / "mkdir_failed" so the
caller can emit a deferred warn AFTER its logger is up.
"""
from __future__ import annotations

import configparser
import os
import shutil
from typing import Optional, Tuple

_DEFAULT_DIR = "~/linuxcnc/lcnc-suite/logs"
_FALLBACK_DIR = "/tmp/lcnc-suite"
_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB


def _ini_log_dir() -> Optional[str]:
    """Read [DISPLAY] WEBUI_LOG_DIR from $LCNC_INI_FILE. Returns None on
    any failure — INI is optional and absence is the common case."""
    ini = os.environ.get("LCNC_INI_FILE")
    if not ini or not os.path.isfile(ini):
        return None
    try:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.read(ini)
    except Exception:
        return None
    if not cp.has_section("DISPLAY"):
        return None
    val = cp.get("DISPLAY", "WEBUI_LOG_DIR", fallback="").strip()
    # Strip inline `# comment` text (inivar in the bash launcher does this
    # too; configparser doesn't by default).
    if "#" in val:
        val = val.split("#", 1)[0].strip()
    return val or None


def _requested() -> str:
    """Pick the requested path BEFORE fallback rules. Env > INI > default."""
    env = os.environ.get("LCNC_WEBUI_LOG_DIR", "").strip()
    if env:
        return os.path.expanduser(env)
    ini = _ini_log_dir()
    if ini:
        return os.path.expanduser(ini)
    return os.path.expanduser(_DEFAULT_DIR)


def _try_use(path: str) -> Optional[str]:
    """Returns None if `path` is usable, else a short reason string."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return "mkdir_failed"
    if not os.access(path, os.W_OK):
        return "permission_denied"
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return "stat_failed"
    if free < _MIN_FREE_BYTES:
        return "low_disk"
    return None


def resolve() -> Tuple[str, Optional[str]]:
    """Resolve the log directory. Returns `(path, fallback_reason)`.

    `fallback_reason` is None on success, or a short reason string
    ("permission_denied", "low_disk", "mkdir_failed", "stat_failed")
    when the requested path was unusable and we fell back to /tmp."""
    requested = _requested()
    reason = _try_use(requested)
    if reason is None:
        return requested, None
    # Fallback. Don't recurse — if /tmp fails the system is unusable.
    try:
        os.makedirs(_FALLBACK_DIR, exist_ok=True)
    except OSError:
        pass
    return _FALLBACK_DIR, reason


if __name__ == "__main__":
    # Shell entry point: prints just the path. The launcher captures
    # this in $LCNC_RESOLVED_LOG_DIR. Fallback reason is dropped here —
    # Python callers get it via resolve(), bash doesn't need it.
    path, _ = resolve()
    print(path)
