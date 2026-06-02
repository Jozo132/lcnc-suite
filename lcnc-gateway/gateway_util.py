#!/usr/bin/env python3
"""Pure, dependency-light helpers shared by the gateway.

This module deliberately imports NOTHING from ``linuxcnc`` (or any other
machine-coupled binding) so it can be imported and unit-tested on a plain
developer machine — ``gateway.py`` itself does ``import linuxcnc`` at module
top and is therefore unimportable under pytest without the binding.

Keep this file pure: stdlib only, no side effects at import time.
"""

import math
import os
import hmac
from urllib.parse import urlsplit
from typing import Iterable, Optional


# File-upload allow-list. Lives here (not gateway.py) so validate_extension is
# self-contained and testable.
ALLOWED_EXTENSIONS = {".ngc", ".nc", ".gcode", ".tap", ".txt"}


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name)
    name = name.replace("\x00", "")
    name = name.lstrip(".")
    if not name:
        name = "uploaded.ngc"
    return name


def validate_extension(filename: str) -> bool:
    _, ext = os.path.splitext(filename)
    return ext.lower() in ALLOWED_EXTENSIONS


def validate_path_within(path: str, root: str) -> bool:
    # Use abspath (not realpath) so symlinked subdirectories are allowed.
    # (See issue #20 for the realpath hardening discussion.)
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(root)
    return abs_path.startswith(abs_root + os.sep) or abs_path == abs_root


def token_ok(presented: Optional[str], configured: str) -> bool:
    """Constant-time pre-shared-token check.

    When no token is configured (empty string) auth is disabled and every
    caller is allowed — this keeps loopback/dev setups frictionless. When a
    token IS configured, the caller must present a matching one.
    """
    if not configured:
        return True  # auth disabled
    if not presented:
        return False
    return hmac.compare_digest(str(presented), str(configured))


def _origin_host_matches(origin: str, host: Optional[str]) -> bool:
    """True if the Origin header's host[:port] equals the request Host header."""
    if not host:
        return False
    netloc = urlsplit(origin).netloc
    return bool(netloc) and netloc.lower() == host.lower()


def origin_allowed(
    origin: Optional[str],
    host: Optional[str],
    allowlist: Optional[Iterable[str]] = None,
    extra_allowed: Optional[Iterable[str]] = None,
) -> bool:
    """Decide whether a WebSocket/CORS Origin is acceptable.

    Policy (Origin defends against *browser* drive-by; the token is the real
    gate for everything else):

    - No Origin header  → allow. Browsers ALWAYS send Origin on WS handshakes,
      so a missing one means a non-browser client, which the token gates.
    - Same host as the request (Origin host[:port] == Host header) → allow.
      This is the gateway's own served page on whatever LAN IP was browsed to,
      and needs no configuration.
    - Origin listed in the explicit allowlist or dev extras → allow.
    - Otherwise → reject.

    The explicit allowlist ADDS to the same-host default rather than replacing
    it, so configuring it can never lock out the gateway's own page.
    """
    if not origin:
        return True
    if _origin_host_matches(origin, host):
        return True
    if allowlist and origin in set(allowlist):
        return True
    if extra_allowed and origin in set(extra_allowed):
        return True
    return False


def finite_float(x, default=0.0) -> float:
    """float() that rejects NaN/Infinity.

    Used for machine-motion values (velocity, distance, override scale) where a
    non-finite number is dangerous. Raises ValueError/TypeError on bad input so
    the dispatch-boundary handler can turn it into a structured error reply.
    """
    if x is None:
        x = default
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"non-finite numeric value: {x!r}")
    return v
