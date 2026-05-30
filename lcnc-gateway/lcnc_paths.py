"""Suite-wide path resolution for log files.

Single source of truth for *where* logs land. Used by gateway.py,
hal_reader.py, hal_watchdog.py — and shellable from the lcnc-suite
launcher (`python3 -c "import lcnc_paths; print(lcnc_paths.resolve()[0])"`)
so the bash side and Python side cannot disagree.

Precedence (first hit wins):
  1. LCNC_LOG_DIR env var
  2. INI [DISPLAY] LOG_DIR (read via configparser from
     LCNC_INI_FILE env — cannot use `linuxcnc.ini` because
     hal_watchdog.py starts before STAT exists)
  3. Default: <install-dir>/runlogs — derived from this module's own
     location so it follows the install and matches restart.sh. This is
     the same dir all four processes log into out of the box.

There is no `/tmp` fallback. A log dir that can't be written is a broken
install, caught loudly at the launcher boundary (`lcnc-suite` write-tests
the resolved dir and aborts before any process starts). resolve() itself
never raises — the safety supervisor (hal_watchdog.py) must boot even if
logging is degraded — and always returns the requested path. The second
tuple element is an informational reason string ("permission_denied" /
"mkdir_failed" / "stat_failed") or None; it no longer changes the path.
"""
from __future__ import annotations

import configparser
import os
from typing import Optional, Tuple

# <install-dir>/runlogs — this file lives in <install-dir>/lcnc-gateway/,
# so the grandparent dir is the install root (same path the launcher
# computes via `readlink -f`).
_INSTALL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DIR = os.path.join(_INSTALL_ROOT, "runlogs")


def _ini_log_dir() -> Optional[str]:
    """Read [DISPLAY] LOG_DIR from $LCNC_INI_FILE. Returns None on
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
    val = cp.get("DISPLAY", "LOG_DIR", fallback="").strip()
    # Strip inline `# comment` text (inivar in the bash launcher does this
    # too; configparser doesn't by default).
    if "#" in val:
        val = val.split("#", 1)[0].strip()
    return val or None


def _requested() -> str:
    """Pick the requested path. Env > INI > default."""
    env = os.environ.get("LCNC_LOG_DIR", "").strip()
    if env:
        return os.path.expanduser(env)
    ini = _ini_log_dir()
    if ini:
        return os.path.expanduser(ini)
    return _DEFAULT_DIR


def _try_use(path: str) -> Optional[str]:
    """Returns None if `path` is usable, else a short reason string.
    Informational only — resolve() returns the requested path regardless;
    the launcher is what acts on an unusable dir (loud abort)."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return "mkdir_failed"
    if not os.access(path, os.W_OK):
        return "permission_denied"
    return None


def resolve() -> Tuple[str, Optional[str]]:
    """Resolve the log directory. Returns `(path, reason)`.

    Always returns the requested path (env > INI > <install-dir>/runlogs);
    there is no `/tmp` fallback. `reason` is None when the dir is usable,
    or an informational string ("permission_denied", "mkdir_failed") when
    it isn't — callers may surface it, but the path does not change.
    Never raises."""
    requested = _requested()
    return requested, _try_use(requested)


if __name__ == "__main__":
    # Shell entry point: prints just the path. The launcher captures
    # this in $LCNC_RESOLVED_LOG_DIR, then write-tests it itself.
    path, _ = resolve()
    print(path)
