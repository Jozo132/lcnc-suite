#!/usr/bin/env python3
import asyncio
import gzip
import json
import math
import time
import os
import subprocess
import tempfile
import threading
from pathlib import Path
import linuxcnc

from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple
from fastapi.staticfiles import StaticFiles
import re
import shutil
from urllib.parse import urlsplit
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Body, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse, JSONResponse, FileResponse, Response

import logging
import click
from contextlib import asynccontextmanager

import lcnc_trace as _trace
_trace.init("gateway")
_trace.install_crash_hooks("gateway")

# Pure, linuxcnc-free helpers (importable under pytest without the binding).
from gateway_util import (
    ALLOWED_EXTENSIONS,
    sanitize_filename,
    validate_extension,
    validate_path_within,
    origin_allowed,
    token_ok,
    finite_float,
    finite_int,
    atomic_write_bytes,
    evaluate_trip_latch,
)
from command_policy import (
    MachineState as _PolicyMachineState,
    evaluate_permissions,
    check_command,
)
from tool_table import (
    parse_tool_table,
    write_tool_table,
    _merge_tool_data,
    _TOOL_META_FIELDS,
)
from settings_store import SettingsStore, VALID_SECTIONS as _VALID_SETTINGS_SECTIONS
from tool_store import ToolLibraryStore


# === TEMP LIFECYCLE PROBE (remove after debugging) ===
# Single timeline anchor: every probe line carries +Nms from this anchor,
# so boot, per-client connect, and shutdown all sit on one ruler. See
# /home/cnc/.claude/plans/can-you-plan-for-jolly-leaf.md for the full plan.
#
# REMOVAL MAP — when stripping this probe later, delete:
#   1. This whole block (lines 27 .. END TEMP LIFECYCLE PROBE marker below):
#        _T0, _dbg, _UvicornTimingFilter (+ filter install loop),
#        _format_conn_state, _install_shutdown_probe (incl. the _patched
#        monkey-patch of Server._wait_tasks_to_complete).
#   2. All scattered call sites — `git grep -n '_dbg('` and `git grep -n
#      '_T0'` and `git grep -n '_conn_t0'` in this file. As of this
#      writing the call sites are at module-level (BOOT markers around
#      try_connect_lcnc / _hal_connect / _load_machine_config / FastAPI
#      app instantiated / lifespan startup yield) and inside ws_endpoint
#      (CONN markers anchored on _conn_t0).
#   3. The lifespan teardown `[SHUTDOWN]` lines (search `_dt(` inside
#      `lifespan`) — re-anchored to _T0 here; either leave as permanent
#      (recommended — they're cheap and the only signal we have for
#      shutdown timing) or delete if going fully clean.
#
# DO KEEP after removal: the `--timeout-graceful-shutdown 1` flag in
# `lcnc-suite` (the real fix that bounds shutdown drain) and the bug
# fix in _patched's `except (Exception, asyncio.CancelledError)` IF the
# probe block is kept around at all. If removing the probe entirely,
# the `_patched` monkey-patch goes with it and the bug becomes moot.
_T0 = time.monotonic()


# Boot start emitted to trace bus only — gateway terminal already shows
# uvicorn's own startup banner. lcnc_trace.py records monotonic_ms so
# subsequent boot.* events can be plotted on a shared timeline.
_trace.emit("boot.start", pid=os.getpid())


class _UvicornTimingFilter(logging.Filter):
    """Prepend [+Nms] (offset from gateway boot) to every uvicorn log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        offset_ms = (time.monotonic() - _T0) * 1000
        prefix = f"[+{offset_ms:.0f}ms] "
        # uvicorn passes its colorised template via `color_message`; prefix
        # both forms so the on-screen and on-file rendering both show it.
        if isinstance(record.msg, str) and not record.msg.startswith("[+"):
            record.msg = prefix + record.msg
        cm = record.__dict__.get("color_message")
        if isinstance(cm, str) and not cm.startswith("[+"):
            record.__dict__["color_message"] = prefix + cm
        return True


_uv_timing_filter = _UvicornTimingFilter()
for _logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_logger_name).addFilter(_uv_timing_filter)


# === TEMP GC-PROBE === log generation-0/1/2 garbage-collector events
# with duration. Python GC mark-sweep can stall the main thread for
# hundreds of ms on a large object graph (e.g. accumulated msgpack
# envelopes / status snapshots / error queues during a 12-client
# storm). If a multi-second event-loop stall coincides with a [GC]
# line showing generation=2 and high duration_ms, GC is the culprit.
# Gen-0 fires very frequently (every few hundred allocations) and is
# usually <1 ms; only logged when slow. Gen-2 is logged unconditionally.
import gc as _gc
_gc_start_t: Dict[int, float] = {}


def _gc_callback(phase: str, info: Dict[str, Any]) -> None:
    gen = info.get("generation", -1)
    if phase == "start":
        _gc_start_t[gen] = time.monotonic()
        return
    if phase != "stop":
        return
    t0 = _gc_start_t.pop(gen, None)
    if t0 is None:
        return
    dt_ms = (time.monotonic() - t0) * 1000
    if gen == 2 or dt_ms > 10:
        offset_ms = (time.monotonic() - _T0) * 1000
        print(
            f"[GC] +{offset_ms:.0f}ms gen={gen} duration={dt_ms:.1f}ms "
            f"collected={info.get('collected', 0)} "
            f"uncollectable={info.get('uncollectable', 0)}",
            flush=True,
        )


_gc.callbacks.append(_gc_callback)
# === END TEMP LIFECYCLE PROBE ===


class _UvicornUrlColorFilter(logging.Filter):
    """Tint the URL in uvicorn's startup log cyan instead of plain bold."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.__dict__.get("color_message")
        if isinstance(msg, str) and msg.startswith("Uvicorn running on "):
            record.__dict__["color_message"] = (
                "Uvicorn running on "
                + click.style("%s://%s:%d", fg="cyan", bold=True)
                + " (Press CTRL+C to quit)"
            )
        return True


logging.getLogger("uvicorn").addFilter(_UvicornUrlColorFilter())
logging.getLogger("uvicorn.error").addFilter(_UvicornUrlColorFilter())


class _UvicornAccessTelemetryFilter(logging.Filter):
    """Drop /telemetry access-log lines — the browser POSTs these on a
    steady cadence (tab visibility, send-buffer pressure, JS errors) and
    each one would otherwise print an INFO line, drowning real requests.
    The event payload still flows through the trace bus as browser.* —
    only the duplicated access-log noise is suppressed."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and path.startswith("/telemetry"):
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_UvicornAccessTelemetryFilter())


# ---- Config ----
POLL_HZ = 30  # status update rate
BASE_DIR = Path(__file__).resolve().parent
MACHINE_DIR = BASE_DIR / "machine"

# ---- Perf experiment flags (INI-sourced via lcnc-suite launcher) ----
# WIRE_FORMAT defaults to msgpack: smaller payload than JSON, faster
# C-accelerated encode, and unlocks the per-tick shared-encode path under
# fan-out when delta is off (one encode per tick instead of N). Set
# WEBUI_WIRE_FORMAT=json explicitly to debug status frames in browser
# DevTools. The remaining flags default OFF.
_WIRE_FORMAT = (os.environ.get("WEBUI_WIRE_FORMAT") or "msgpack").strip().lower()
if _WIRE_FORMAT not in ("json", "msgpack"):
    _WIRE_FORMAT = "msgpack"

# Per-send timeout for ws.send_* calls. A backgrounded browser tab that
# stops reading back-pressures the kernel TCP write buffer; `await
# ws.send_bytes` then holds the asyncio loop long enough (1+ s observed)
# to break the 500 ms HAL heartbeat budget and trigger a safety trip.
# We bound any single send to this many seconds and force-close the
# offending client on timeout — bug fix, not trip suppression: the
# heartbeat task is unchanged and still trips on any other loop hang.
# 0.2 s leaves ~300 ms of remaining budget for the rest of the cycle.
# See plan in /home/cnc/.claude/plans/can-you-plan-for-jolly-leaf.md.
_WS_SEND_TIMEOUT_S = 0.2
_STATUS_DELTA_ENABLED = os.environ.get("WEBUI_STATUS_DELTA") == "1"
_ADAPTIVE_POLL_ENABLED = os.environ.get("WEBUI_ADAPTIVE_POLL") == "1"
try:
    _IDLE_POLL_HZ = max(1, int(os.environ.get("WEBUI_IDLE_POLL_HZ") or "5"))
except ValueError:
    _IDLE_POLL_HZ = 5
# Full-snapshot cadence when delta mode is on: force a full every N cycles so
# any drift-bug self-heals within ~3s at 30 Hz.
_DELTA_FULL_INTERVAL = 100

# ---- Wire-format encoders ----
# msgspec.json avoids per-call Encoder setup cost; msgspec.msgpack produces
# compact binary frames for float-heavy StatusPayload. Both encoders are
# thread-safe for reuse. msgspec is a hard dep (see requirements.txt).
import msgspec as _msgspec


def _wire_enc_hook(obj):
    # Any object msgspec doesn't know how to encode gets stringified
    # (covers Path, datetime, and stray non-primitive payloads).
    return str(obj)


_json_encoder = _msgspec.json.Encoder(enc_hook=_wire_enc_hook)
_msgpack_encoder = _msgspec.msgpack.Encoder(enc_hook=_wire_enc_hook)

# NOTE: status-tick envelope encoding is done INLINE on the event loop (see
# ws_send_measured). An earlier dedicated ThreadPoolExecutor for it was measured
# and removed — encode_qsize stayed 0 while encode_ms still climbed, proving the
# cost is GIL contention, not pool dispatch; a worker pool only added overhead.

# Optional WS-init concurrency limiter. Default of 20 is a no-op for the
# documented 10–13-tab scenario. If a real overload manifests (HAL trip
# even after viewer_init caching + dedicated encode pool), set
# WEBUI_WS_INIT_CONCURRENCY=4 (or similar) in [DISPLAY] to serialize
# per-client init at accept time. NOT wired in by default — the cache
# (build_viewer_init) already drops per-connect cost to microseconds on
# 2nd+ client, making this throttle redundant in practice. Wire-up when
# needed: `async with _ws_init_sem:` around the accept→status_task span
# at the top of ws_endpoint(). Keep the wrap-window short (release after
# status_task spawn) so concurrent live connections aren't capped at the
# init-concurrency limit.
_WS_INIT_LIMIT = int(os.environ.get("WEBUI_WS_INIT_CONCURRENCY", "20"))
_ws_init_sem = asyncio.Semaphore(_WS_INIT_LIMIT)


# ── Auth / origin controls (issue #17) ──
# Pre-shared token: empty string disables auth (loopback/dev). The launcher
# fail-closed rule guarantees a token is set whenever HOST is non-loopback.
WEBUI_TOKEN = os.environ.get("LCNC_WEBUI_TOKEN", "").strip()
# Explicit Origin allow-list (comma/space-separated). Empty ⇒ same-host only.
_ALLOWED_ORIGINS = {
    o.strip() for o in re.split(r"[,\s]+", os.environ.get("LCNC_WEBUI_ALLOWED_ORIGINS", "").strip()) if o.strip()
}
_DEV = os.environ.get("LCNC_WEBUI_DEV", "0").strip() not in ("", "0", "false", "False")
# In dev the page is served by Vite on :5173 and proxied to the gateway, so the
# Origin won't match the gateway's host:port. Admit the standard local Vite
# origins explicitly; the port rule in _ws_origin_ok also covers LAN dev.
_DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"} if _DEV else set()
_VITE_DEV_PORT = 5173


def _ws_origin_ok(origin: Optional[str], host: Optional[str]) -> bool:
    if origin_allowed(origin, host, _ALLOWED_ORIGINS, _DEV_ORIGINS):
        return True
    # Dev convenience: the Vite dev server is reachable on any LAN IP, and the
    # proxied WS may not preserve the Host header for a same-host match. So in
    # dev mode admit any origin on the Vite port — the token still gates the
    # connection. Never active in production (_DEV is false).
    if _DEV and origin:
        try:
            if urlsplit(origin).port == _VITE_DEV_PORT:
                return True
        except ValueError:
            pass
    return False


def _peer_of(ws: WebSocket) -> str:
    return ws.client.host if ws.client else "?"


async def require_token(request: Request):
    """FastAPI dependency gating mutation routes (issue #17).

    No-op when no token is configured (loopback/dev). Accepts the token from the
    ``X-Auth-Token`` header OR a ``token`` query param — the latter is needed
    because ``navigator.sendBeacon`` (settings flush on page exit) cannot set
    headers.
    """
    if not WEBUI_TOKEN:
        return
    presented = request.headers.get("x-auth-token") or request.query_params.get("token")
    if not token_ok(presented, WEBUI_TOKEN):
        _trace.emit("rest.auth_rejected", level="warn",
                    peer=(request.client.host if request.client else "?"),
                    path=request.url.path)
        raise HTTPException(status_code=401, detail="Unauthorized")


def _json_encoder_encode(obj):
    return _json_encoder.encode(obj)  # returns bytes


def _encode_ws_frame(obj):
    """Encode a WS payload with the active wire format. Returns bytes for
    msgpack (→ ws.send_bytes) or str for json (→ ws.send_text). Used for
    shared payloads (viewer_gcode, surface_points, comp_grid) that are
    encoded once and broadcast verbatim to every client."""
    if _WIRE_FORMAT == "msgpack":
        return _msgpack_encoder.encode(obj)
    return _json_encoder_encode(obj).decode("utf-8")

# ---- LinuxCNC handles (nullable for auto-reconnect) ----
STAT: Optional[linuxcnc.stat] = None
CMD: Optional[linuxcnc.command] = None
ERR: Optional[linuxcnc.error_channel] = None
lcnc_connected = False
_lcnc_pid: Optional[int] = None  # tracks linuxcncsvr PID

# ---- WCS offset cache (populated from STAT at 30Hz) ----
_WCS_BASES = [5220, 5240, 5260, 5280, 5300, 5320, 5340, 5360, 5380]
_WCS_NAMES = ["G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3"]
_G5X_MAP = {"G54": 1, "G55": 2, "G56": 3, "G57": 4, "G58": 5, "G59": 6, "G59.1": 7, "G59.2": 8, "G59.3": 9}
_WCS_AXIS_KEYS = ["x", "y", "z", "a", "b", "c", "u", "v", "w"]
_wcs_cache = [{"name": n, "x": 0.0, "y": 0.0, "z": 0.0, "a": 0.0, "b": 0.0, "c": 0.0, "u": 0.0, "v": 0.0, "w": 0.0, "r": 0.0} for n in _WCS_NAMES]
_wcs_var_file_mtime: Optional[float] = None  # None means "not yet seeded"

# ---- Connected WebSocket clients ----
@dataclass
class ClientState:
    """Per-client server-side state. Replaces the prior dict-of-dict
    registry; the field set is small and stable, so a typed shape
    catches typos at write time and avoids `.get()` defaults entirely
    for known fields.

    The broadcast `clients` envelope reads only `ip` and `armed`; the
    rest are server-internal."""
    ip: str
    ws: "WebSocket"
    armed: bool = False
    halshow_live: bool = False
    last_hb: float = 0.0       # wall-clock time of last heartbeat from this client
    hb_mono: float = 0.0       # monotonic ts of last hb (0 = never seen)
    send_pending: bool = False # status fan-out is in-flight to this client
    hidden: bool = False       # client tab is backgrounded (visibilityState)
    session_id: Optional[str] = None  # tab-scoped UUID; basis for armed-resume across brief reconnects


_clients: Dict[int, ClientState] = {}
_next_client_id = 0

# ---- Armed-resume holds (Layer D, session-id resume) ----
# When an armed client's WebSocket closes, we register a short-lived hold
# keyed by session_id. If the same session reconnects within the window via
# `cmd:"hello"`, the new connection silently inherits armed=true. This makes
# Ctrl-R, Wi-Fi blips, and brief screen-lock-induced WS closes invisible.
#
# tab close → new sessionId on next open → no resume (intentional)
# Ctrl-R    → same sessionId → resume granted
_ARMED_RESUME_GRACE_SEC = 10.0
_armed_resume_holds: Dict[str, float] = {}  # session_id → expiry monotonic ts
# Armed-resume is for a CLEAN brief reconnect (Ctrl-R, wifi blip) — the client was
# beating normally right up to the drop. If the heartbeat was already lagging at
# disconnect (a struggling/overloaded client), it's NOT a clean reconnect: do not
# auto-restore armed, require an explicit operator re-arm. 2.0 s = at most ~1
# missed 1 Hz beat; the disarm timeout is 3 s.
_RESUME_MAX_HB_AGE = 2.0


def _register_armed_resume_hold(session_id: Optional[str], client_id: int) -> None:
    """Register an armed-resume hold for a session_id. No-op if no session_id."""
    if not session_id:
        return
    _prune_armed_resume_holds()
    expiry = time.monotonic() + _ARMED_RESUME_GRACE_SEC
    _armed_resume_holds[session_id] = expiry
    _trace.emit(
        "session.resume_hold_registered",
        client_id=client_id, session_id=session_id,
        grace_sec=_ARMED_RESUME_GRACE_SEC,
    )


def _peek_armed_resume_hold(session_id: Optional[str]) -> str:
    """Inspect a session_id without consuming. Returns one of:
      "granted" — hold exists and within grace window
      "expired" — hold exists but past expiry
      "no_match" — no hold registered for this session_id (or no id at all)
    Used by the hello handler to decide which trace event to emit before
    actually consuming the hold via _consume_armed_resume_hold."""
    if not session_id:
        return "no_match"
    expiry = _armed_resume_holds.get(session_id)
    if expiry is None:
        return "no_match"
    if time.monotonic() > expiry:
        return "expired"
    return "granted"


def _consume_armed_resume_hold(session_id: Optional[str]) -> None:
    """Remove the hold for this session_id (single-use). Caller should have
    already peeked to decide whether to grant. Safe to call even if no hold
    exists — pops with default."""
    if not session_id:
        return
    _armed_resume_holds.pop(session_id, None)


def _prune_armed_resume_holds() -> None:
    """Drop expired holds. Bounded size in practice (one entry per recently-
    disconnected armed client) but cheap to keep tidy."""
    now = time.monotonic()
    expired = [sid for sid, exp in _armed_resume_holds.items() if now > exp]
    for sid in expired:
        del _armed_resume_holds[sid]

# ---- HAL watchdog socket client ----
# The watchdog is loaded by LinuxCNC HAL config (loadusr -W hal_watchdog.py).
# Gateway connects to its Unix socket to send heartbeat/connected updates.
import sys
import signal
import socket as _socket


_HAL_SOCK_PATH = "/tmp/webui-safety.sock"
_hal_sock: Optional[_socket.socket] = None
_hal_last_hb = False
_disconnect_grace_task: Optional[asyncio.Task] = None
_DISCONNECT_GRACE_SEC = 3.0  # covers 2s frontend reconnect delay
_estop_hold = False  # hold connected=FALSE during UI e-stop

# Bound for CMD.wait_complete() on short mode/teleop/abort transitions.
# Prevents an unbounded block from tying up an executor thread or delaying
# the next command on the same client's receive path. MDI/program_open keep
# their own larger timeouts (5s) because the interpreter can legitimately
# take longer to acknowledge a parsed block.
_CMD_WAIT_TIMEOUT = 2.0

def _hal_connect():
    """Connect to the HAL watchdog Unix socket. Non-fatal if unavailable.

    Socket is set to a tight (50 ms) sendall timeout so that, if the kernel
    Unix-socket send buffer fills (e.g. watchdog process scheduling-delayed
    during a cold-start handshake storm), our heartbeat task does NOT block
    the entire asyncio loop waiting for buffer space. A timed-out sendall
    raises socket.timeout, which we catch and treat as a dropped heartbeat
    — the watchdog will correctly trip if heartbeats stop reaching it. The
    timeout is generous compared to the 33 ms heartbeat cadence and tiny
    compared to the 500 ms HAL trip threshold; we lose at most one
    heartbeat to detect backpressure.
    """
    global _hal_sock
    if _hal_sock is not None:
        return  # already connected
    try:
        _hal_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        _hal_sock.connect(_HAL_SOCK_PATH)
        _hal_sock.settimeout(0.05)
        _trace.emit("hal.socket_connected")
    except Exception as e:
        _trace.emit("hal.socket_connect_failed", level="error",
                    exc=type(e).__name__, msg=str(e))
        _hal_sock = None

def _hal_disconnect():
    """Disconnect from the HAL watchdog socket."""
    global _hal_sock
    if _hal_sock is not None:
        try:
            _hal_sock.close()
        except Exception:
            pass  # safe-silent: socket cleanup, already-closed is fine
        _hal_sock = None

# hal.send_summary is emitted once per N sends (N=30 ≈ 1 s at heartbeat
# cadence) by the shared Aggregator. `slow_count` is tallied locally
# (it isn't an avg/max metric) and reset by the extra-fields callable
# at emit time. `tcp_outq` is read fresh on each emit so the kernel
# buffer state is captured at summary-publication time, not at
# Aggregator construction.
_hal_send_slow_count = 0


def _hal_send_extras() -> dict:
    global _hal_send_slow_count
    out = {"slow_count": _hal_send_slow_count, "tcp_outq": _hal_tcp_outq()}
    _hal_send_slow_count = 0
    return out


_hal_send_agg = _trace.Aggregator(
    "hal.send_summary", every=30, extra_fields=_hal_send_extras
)


def _hal_send(msg: dict):
    """Send a pin-update message to the HAL watchdog via socket."""
    global _hal_sock, _hal_send_slow_count
    if _hal_sock is None:
        _hal_connect()
    if _hal_sock is not None:
        _send_t0 = time.monotonic()
        try:
            _hal_sock.sendall((json.dumps(msg) + "\n").encode())
        except _socket.timeout:
            # Kernel send buffer full — watchdog process is scheduling-
            # delayed (or hung). Drop this message and DON'T block the loop
            # waiting for buffer space. Heartbeat task continues firing; if
            # the watchdog truly isn't reading, the trip will fire correctly
            # via oneshot.0.out going FALSE on its own. This converts a
            # multi-second loop stall into a logged drop of one heartbeat.
            _send_dt_to = (time.monotonic() - _send_t0) * 1000
            _trace.emit("hal.send_timeout", level="warn",
                        send_ms=round(_send_dt_to, 1),
                        msg_keys=list(msg.keys()))
            return
        except (OSError, BrokenPipeError):
            _hal_sock = None  # will reconnect on next send
            _trace.emit("hal.send_disconnect", level="warn",
                        msg_keys=list(msg.keys()))
            return
        _send_dt = (time.monotonic() - _send_t0) * 1000
        if _send_dt > 30:
            _trace.emit("hal.send_slow", level="warn",
                        send_ms=round(_send_dt, 1),
                        msg_keys=list(msg.keys()))
            _hal_send_slow_count += 1
        _hal_send_agg.record(ms=_send_dt)


try:
    import fcntl as _fcntl
    import struct as _struct
    _SIOCOUTQ = 0x5411  # Linux: bytes in TCP send buffer not yet acked
    _SIOCINQ = 0x541B   # Linux: bytes in TCP recv buffer not yet read
except Exception:
    _fcntl = None
    _struct = None
    _SIOCOUTQ = 0
    _SIOCINQ = 0


def _snapshot_trip(trip_ts_ns: int) -> None:
    """Write a forensic bundle when a HAL safety trip fires. Runs in a
    worker thread (asyncio.to_thread) so it can't block the loop. Best
    effort: any failure is logged but doesn't propagate."""
    base = _trace.log_dir()
    if not base:
        # No silent scatter: a trip bundle written to cwd/`/tmp` is a lost
        # forensic record. Surface the bug instead.
        _trace.emit("safety.snapshot_no_log_dir", level="error",
                    trip_ts_ns=trip_ts_ns)
        return
    out_dir = os.path.join(base, f"trips/{trip_ts_ns}/")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        _trace.emit("safety.snapshot_mkdir_failed", level="error",
                    out_dir=out_dir, exc=type(e).__name__, msg=str(e))
        return
    try:
        # The bundler is best-effort: 10 s timeout protects against the
        # rare case where /tmp is on a slow filesystem (e.g. tmpfs full).
        rc = subprocess.run(
            [
                "python3",
                "/home/cnc/lcnc-suite/scripts/trace-bundle.py",
                "--trip",
                "--out", out_dir,
            ],
            timeout=10,
            capture_output=True,
            check=False,
        )
        _trace.emit("safety.trip_snapshot_done",
                    out_dir=out_dir, rc=rc.returncode)
    except subprocess.TimeoutExpired:
        _trace.emit("safety.trip_snapshot_timeout", level="warn",
                    out_dir=out_dir, timeout_s=10)
    except Exception as e:
        _trace.emit("safety.trip_snapshot_failed", level="warn",
                    out_dir=out_dir, exc=type(e).__name__, msg=str(e))


def _hal_tcp_outq() -> int:
    """Bytes queued in the kernel send buffer for _hal_sock. Linux only.
    Returns -1 on any failure. Cheap (one ioctl, ~1 us)."""
    if _hal_sock is None or _fcntl is None or _struct is None:
        return -1
    try:
        buf = bytearray(4)
        _fcntl.ioctl(_hal_sock.fileno(), _SIOCOUTQ, buf)
        return _struct.unpack("I", bytes(buf))[0]
    except Exception:
        return -1


# ---- HAL reader (sibling process: webui-reader) ----
# hal_reader.py owns the `webui-reader` HAL component and pushes a snapshot
# of the pins poll_status() needs at 30 Hz. The gateway never imports `hal`;
# all HAL access goes over this socket.  set_p() and halshow_dump() are
# served as request/reply on the same socket.
_READER_SOCK_PATH = "/tmp/webui-reader.sock"
# Single-rebind state: (snapshot, monotonic_ts). Both halves always come from
# the same tick — no torn reads even if a future caller reads both values
# across an `await`.
_reader_state: Optional[Tuple[dict, float]] = None
_reader_writer: Optional[asyncio.StreamWriter] = None
_reader_lock = asyncio.Lock()
_reader_pending: Dict[int, asyncio.Future] = {}
_reader_next_id = 0
# A snapshot is "stale" if no message has arrived within this window.
# The reader pushes at 30 Hz (~33 ms) so 2 s = ~60 missed ticks.
_READER_STALE_SEC = 2.0


async def _reader_recv_loop():
    """Connect to hal_reader.py and dispatch incoming messages.

    Snapshots update _reader_state. Replies resolve pending futures keyed by
    request id. Reconnects on socket close with a 1 s backoff.
    """
    global _reader_writer, _reader_state
    while True:
        _set_phase("reader_recv.connecting")
        try:
            reader, writer = await asyncio.open_unix_connection(_READER_SOCK_PATH)
        except Exception as e:
            _trace.emit("reader.connect_failed", level="warn",
                        exc=type(e).__name__, msg=str(e))
            await asyncio.sleep(1.0)
            continue
        _reader_writer = writer
        _trace.emit("reader.connected")
        # Push extra-pin config (e.g. user-configured spindle load pin) so the
        # reader includes it in subsequent snapshots. Spawned as a separate
        # task because this loop dispatches the reply — awaiting here would
        # deadlock.
        asyncio.create_task(_reader_configure_extra_pins())
        try:
            while True:
                _set_phase("reader_recv.readline")
                line = await reader.readline()
                if not line:
                    break
                _set_phase("reader_recv.process_line")
                try:
                    msg = json.loads(line.decode())
                except Exception as e:
                    _trace.emit("reader.bad_json", level="warn",
                                exc=type(e).__name__, msg=str(e))
                    continue
                mtype = msg.get("type")
                if mtype == "snapshot":
                    _reader_state = (msg, time.monotonic())
                elif mtype == "reply":
                    fut = _reader_pending.pop(msg.get("id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except Exception as e:
            _trace.emit("reader.recv_loop_error", level="warn",
                        exc=type(e).__name__, msg=str(e))
        finally:
            _set_phase("reader_recv.cleanup")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass  # safe-silent: async socket cleanup, peer may have vanished
            _reader_writer = None
            # Fail any pending requests so callers don't hang.
            for fut in _reader_pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("HAL reader disconnected"))
            _reader_pending.clear()
        await asyncio.sleep(1.0)


async def _reader_configure_extra_pins() -> None:
    """Push the current extra-pin config to the reader.

    Called on reader reconnect and when settings change. Reads settings
    directly so it's correct regardless of whether status_loop has run yet
    (race-free at startup). Fire-and-forget; failures log but don't propagate.
    """
    if _reader_writer is None:
        return
    pins: Dict[str, str] = {}
    try:
        _settings = await asyncio.to_thread(load_settings)  # file read off the loop (B3)
        slp = _settings.get("machine", {}).get("spindleLoadPin", "")
    except Exception:
        slp = ""
    if isinstance(slp, str) and _HAL_PIN_RE.match(slp):
        pins["spindle_load"] = slp
    try:
        await _reader_request("set_extra_pins", pins=pins)
    except Exception as e:
        _trace.emit("reader.configure_extra_pins_failed", level="warn",
                    exc=type(e).__name__, msg=str(e))


async def _reader_request(req: str, timeout: float = 2.0, **kwargs) -> dict:
    """Send a request to hal_reader.py and await the reply.

    Raises ConnectionError if the reader is not connected, TimeoutError if
    the reply doesn't arrive in time, RuntimeError if the reader returned ok=False.
    """
    global _reader_next_id
    if _reader_writer is None:
        raise ConnectionError("HAL reader not connected")
    # Lock guards only the ID-increment + future-registration handshake.
    # The actual write+drain happens unlocked: each call writes one complete
    # `{...}\n` framed message in a single StreamWriter.write() (atomic on
    # the buffer), so concurrent senders can't interleave bytes.
    async with _reader_lock:
        _reader_next_id += 1
        req_id = _reader_next_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _reader_pending[req_id] = fut
    try:
        _reader_writer.write((json.dumps({"id": req_id, "req": req, **kwargs}) + "\n").encode())
        await _reader_writer.drain()
    except Exception:
        _reader_pending.pop(req_id, None)
        raise
    try:
        reply = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _reader_pending.pop(req_id, None)
        raise
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "reader request failed"))
    return reply.get("result")


def _reader_get(field: str):
    """Return field from latest snapshot, or None if snapshot absent / field missing.

    No `default` param by design — every absent value must surface as None all
    the way to the frontend so consumers see "no data" honestly. See
    feedback_no_silent_fallbacks.md.
    """
    state = _reader_state
    if state is None:
        return None
    return state[0].get(field)


def _reader_is_stale() -> bool:
    """True if no snapshot has arrived within _READER_STALE_SEC."""
    state = _reader_state
    if state is None:
        return True
    return (time.monotonic() - state[1]) > _READER_STALE_SEC


async def _disconnect_grace():
    """Keep heartbeat alive while waiting for reconnect, then drop pins."""
    _set_phase("disconnect_grace.entry")
    global _hal_last_hb
    ticks = int(_DISCONNECT_GRACE_SEC * POLL_HZ)
    for _i in range(ticks):
        if _clients:
            _set_phase("disconnect_grace.client_returned_exit")
            return  # client reconnected during grace
        _set_phase(f"disconnect_grace.tick {_i}/{ticks}")
        _hal_last_hb = not _hal_last_hb
        _hal_send({"heartbeat": _hal_last_hb, "connected": True})
        await asyncio.sleep(1.0 / POLL_HZ)
    if not _clients:
        _set_phase("disconnect_grace.final_drop")
        _hal_send({"connected": False, "heartbeat": False})
    _set_phase("disconnect_grace.exit")


def _start_disconnect_grace():
    global _disconnect_grace_task
    if _disconnect_grace_task and not _disconnect_grace_task.done():
        return  # already running
    _disconnect_grace_task = register_bg_task(asyncio.get_event_loop().create_task(_disconnect_grace()))


async def _cancel_disconnect_grace():
    """Cancel and await the grace task before returning.

    Both _disconnect_grace and _heartbeat_loop toggle _hal_last_hb. If we
    only call .cancel() (returns immediately), the grace task can still run
    one more tick and flip the heartbeat pin while the new heartbeat loop
    is also flipping it — three flips in two ticks can trip oneshot.0.out.
    Awaiting the cancellation guarantees the grace task is done before the
    heartbeat loop starts.
    """
    global _disconnect_grace_task
    task = _disconnect_grace_task
    _disconnect_grace_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # safe-silent: we just cancelled it, CancelledError is the success signal
        except Exception as e:
            _trace.emit("disconnect_grace.cancel_error", level="warn",
                        error=f"{type(e).__name__}: {e}")


_heartbeat_task: Optional[asyncio.Task] = None


async def _heartbeat_loop():
    """Independent HAL heartbeat — decoupled from status processing.

    Toggles at POLL_HZ while clients are connected.  When no clients remain
    the loop yields to _disconnect_grace which manages the grace period.
    E-Stop is handled via the independent command path (ws.receive_text →
    handle_command) so a stuck status_loop does not block safety controls.
    """
    global _hal_last_hb
    _hb_expected = 1.0 / POLL_HZ
    _hb_last = time.monotonic()
    # === TEMP HB-TRACE PROBE === fires only when a 30-iteration window (~1 s
    # at POLL_HZ=30) shows an anomaly that [HB-STALL]'s per-tick drift detector
    # misses. Two cases:
    #   1. Monotonic clock froze (VM pause / cgroup throttle / scheduler
    #      lockout): wall delta ≫ monotonic delta between consecutive samples.
    #      [HB-STALL] stays silent because post-sleep drift in monotonic terms
    #      looks normal — the clock itself stopped.
    #   2. Many sub-200ms stalls accumulating: each one is below [HB-STALL]'s
    #      threshold but together push the 30-iter window well past 1 s.
    # Healthy operation = silent. Anomaly = one log line with full context.
    _hb_iter = 0
    _hb_sample_mono = time.monotonic()
    _hb_sample_wall = time.time()
    # Thresholds: skew >100ms means wall and monotonic disagree by enough to
    # rule out routine scheduling jitter; mono_dt >1.5s (50% over expected 1s)
    # means accumulated stall worth surfacing.
    _HB_SKEW_THRESH = 0.1
    _HB_MONO_THRESH = 1.5
    # === TEMP HB-WAKE PROBE === pre-send anchor to catch a hidden gap that
    # [HB-STALL] cannot see: scheduling delay between asyncio.sleep returning
    # and the heartbeat task actually being selected to run its body. If a
    # busy event loop holds the heartbeat task ready-but-not-running for
    # 100+ ms, this fires while [HB-STALL] (which measures post-sleep drift)
    # stays silent because the elapsed-vs-expected math at sleep return is
    # normal. Threshold 100ms — well under the 200 ms HB-STALL threshold so
    # we catch sub-stall scheduling jitter that otherwise compounds silently.
    _hb_last_pre_send = time.monotonic()
    while True:
        _hb_pre_send = time.monotonic()
        _hb_pre_send_gap = (_hb_pre_send - _hb_last_pre_send) * 1000
        if _hb_pre_send_gap > 100:
            print(
                f"[HB-WAKE] +{(_hb_pre_send - _T0) * 1000:.0f}ms "
                f"pre-send-to-pre-send gap {_hb_pre_send_gap:.0f}ms "
                f"(expected {_hb_expected*1000:.0f}ms)",
                flush=True,
            )
        _hb_last_pre_send = _hb_pre_send
        if _clients:
            _hal_last_hb = not _hal_last_hb
            _hal_send({"heartbeat": _hal_last_hb, "connected": not _estop_hold})
        await asyncio.sleep(_hb_expected)
        _hb_now = time.monotonic()
        # Any gap >200ms between heartbeat ticks is within the 500ms watchdog
        # budget but worth surfacing — lets us correlate stalls with parse /
        # scan / encode events in the logs.
        _hb_drift = _hb_now - _hb_last - _hb_expected
        if _hb_drift > 0.2:
            print(f"[HB-STALL] heartbeat gap {_hb_drift*1000:.0f}ms (expected {_hb_expected*1000:.0f}ms)", flush=True)
        _hb_last = _hb_now
        _hb_iter += 1
        # Sample wall/mono every 30 iterations (~1 s at POLL_HZ=30). Log only
        # when the window shows clock skew or accumulated stall — see TEMP
        # HB-TRACE PROBE comment above the loop for the two failure modes.
        if _hb_iter % 30 == 0:
            _hb_now_wall = time.time()
            _hb_mono_dt = _hb_now - _hb_sample_mono
            _hb_wall_dt = _hb_now_wall - _hb_sample_wall
            _hb_skew = abs(_hb_wall_dt - _hb_mono_dt)
            if _hb_skew > _HB_SKEW_THRESH or _hb_mono_dt > _HB_MONO_THRESH:
                print(
                    f"[HB] +{(_hb_now - _T0) * 1000:.0f}ms iter={_hb_iter} "
                    f"mono_dt={_hb_mono_dt*1000:.0f}ms "
                    f"wall_dt={_hb_wall_dt*1000:.0f}ms "
                    f"skew={_hb_skew*1000:.0f}ms clients={len(_clients)}",
                    flush=True,
                )
            _hb_sample_mono = _hb_now
            _hb_sample_wall = _hb_now_wall


# Phase ring buffer — replaces last-writer-wins single global. Each
# _set_phase call appends (mono_s, name) to the ring. When [LAG] fires we
# dump the slice of the ring that overlaps the stall window, plus compute
# the phase that occupied the largest fraction of it ("dominant_phase" =
# the actual culprit). The previous single-global design always reported
# whoever woke first after the stall, which is innocent.
_PHASE_RING_LEN = 256
_phase_ring: List[Tuple[float, str]] = []
_phase_ring_idx = 0
_current_phase: str = "idle"
_phase_started_at: float = 0.0


def _set_phase(name: str) -> None:
    """Append a phase marker to the ring and update the current pointer.
    Cheap: append + 2 globals + monotonic()."""
    global _current_phase, _phase_started_at, _phase_ring_idx
    now = time.monotonic()
    _current_phase = name
    _phase_started_at = now
    if len(_phase_ring) < _PHASE_RING_LEN:
        _phase_ring.append((now, name))
    else:
        _phase_ring[_phase_ring_idx] = (now, name)
        _phase_ring_idx = (_phase_ring_idx + 1) % _PHASE_RING_LEN


def _phase_ring_snapshot() -> List[Tuple[float, str]]:
    """Return phases in chronological order. Caller must not mutate."""
    if len(_phase_ring) < _PHASE_RING_LEN:
        return list(_phase_ring)
    # Already filled; reorder so oldest is first.
    return _phase_ring[_phase_ring_idx:] + _phase_ring[:_phase_ring_idx]


def _phase_window(start_mono: float, end_mono: float) -> List[dict]:
    """Phases that overlap [start, end]. Each entry has start_ms, dur_ms,
    name. Durations clipped to the window so dominant_phase math is honest."""
    snap = _phase_ring_snapshot()
    out: List[dict] = []
    for i, (t_begin, name) in enumerate(snap):
        # End of this phase is the start of the next, or `now` for the last.
        t_end = snap[i + 1][0] if i + 1 < len(snap) else end_mono
        # Skip phases entirely before the window or entirely after.
        if t_end <= start_mono:
            continue
        if t_begin >= end_mono:
            continue
        clipped_begin = max(t_begin, start_mono)
        clipped_end = min(t_end, end_mono)
        out.append({
            "start_ms": round((clipped_begin - start_mono) * 1000, 2),
            "dur_ms": round((clipped_end - clipped_begin) * 1000, 2),
            "name": name,
        })
    return out


def _dominant_phase(window: List[dict]) -> Optional[str]:
    """Phase name that occupied the largest total duration in the window."""
    if not window:
        return None
    totals: Dict[str, float] = {}
    for ph in window:
        totals[ph["name"]] = totals.get(ph["name"], 0.0) + ph["dur_ms"]
    return max(totals.items(), key=lambda kv: kv[1])[0]


async def _loop_lag_monitor():
    """Fire-and-forget task that measures event-loop scheduling lag.

    Prints + emits a structured `lag.window` event whenever the loop takes
    >50 ms longer than the requested sleep to wake back up. The structured
    event includes the ring-buffer slice that overlaps the stall window
    plus the computed `dominant_phase` (the actual culprit). The legacy
    [LAG] line is preserved for grep familiarity.
    """
    TICK = 0.05
    THRESHOLD = 0.05  # 50 ms — was 100; lower for sub-trip visibility
    last = time.monotonic()
    while True:
        await asyncio.sleep(TICK)
        now = time.monotonic()
        drift = now - last - TICK
        if drift > THRESHOLD:
            # Stall window: the period between when we expected to wake
            # (last + TICK) and when we actually woke (now). Pad backward
            # by a small margin so we catch the phase that started just
            # before the stall.
            window_start = last + TICK - 0.005
            window_end = now
            window = _phase_window(window_start, window_end)
            dominant = _dominant_phase(window)
            phase_age_ms = (now - _phase_started_at) * 1000 if _phase_started_at else 0
            _trace.emit(
                "lag.window",
                level="warn",
                drift_ms=round(drift * 1000, 1),
                window_ms=round((window_end - window_start) * 1000, 1),
                dominant_phase=dominant,
                last_phase=_current_phase,
                last_phase_age_ms=round(phase_age_ms, 1),
                phases=window,
            )
        last = now


_lag_monitor_task: Optional[asyncio.Task] = None
_loop_tick_task: Optional[asyncio.Task] = None


_rss_read_warned = False


def _read_rss_kb() -> int:
    """Read VmRSS from /proc/self/status. Returns 0 on any failure (Linux-
    only path; harmless on other OSes since the gateway runs on Linux)."""
    global _rss_read_warned
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
    except Exception as e:
        # One-shot warn — /proc failure is a fundamental OS issue, not
        # a transient. Rate-limited at one event per process lifetime so
        # the poll loop doesn't spam.
        if not _rss_read_warned:
            _rss_read_warned = True
            _trace.emit_exc("proc.vmrss_read_failed", e)
    return 0


def _executor_qsize() -> int:
    """Pending items in the default thread-pool executor. Best-effort:
    accesses a private attribute (_default_executor._work_queue) since
    asyncio doesn't expose a stable getter. Returns -1 if unavailable."""
    try:
        loop = asyncio.get_event_loop()
        ex = getattr(loop, "_default_executor", None)
        if ex is None:
            return -1
        wq = getattr(ex, "_work_queue", None)
        if wq is None:
            return -1
        return wq.qsize()
    except Exception:
        return -1


# /proc/stat aggregate CPU breakdown — first line is "cpu  user nice system
# idle iowait irq softirq steal guest guest_nice". The `steal` field is what
# we care about: time the VM was runnable but the hypervisor was running
# something else. Inside a VM under host load, this delta jumps. Outside a
# VM (or when the host has plenty of CPU) it stays at 0. Counter unit is
# USER_HZ (100 jiffies/s on Linux), so we convert to ms for readability.
_USER_HZ = 100  # standard on Linux x86; sysconf(_SC_CLK_TCK) confirms but
                # the value has been 100 forever.
_last_proc_stat: Optional[Tuple[float, dict]] = None  # (mono_s, parsed)


def _read_proc_stat() -> Optional[dict]:
    """Read /proc/stat aggregate cpu line. Returns a dict of jiffy counters
    or None on parse failure. Only the aggregate `cpu` line is parsed —
    per-core breakdown is not needed for steal-time detection."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        if not line.startswith("cpu "):
            return None
        parts = line.split()
        # parts[0] = 'cpu', parts[1..] = counters in jiffies
        names = ["user", "nice", "system", "idle", "iowait", "irq",
                 "softirq", "steal", "guest", "guest_nice"]
        out: Dict[str, int] = {}
        for i, name in enumerate(names):
            if i + 1 < len(parts):
                try:
                    out[name] = int(parts[i + 1])
                except ValueError:
                    out[name] = 0
            else:
                out[name] = 0
        return out
    except Exception:
        return None


def _proc_stat_delta() -> Dict[str, Any]:
    """Compute per-second jiffy deltas from the last `loop.tick` call.
    Returns dict with `steal_ms`, `iowait_ms`, `idle_pct`, plus raw jiffy
    deltas. First call after start returns absolute values (warm-up)."""
    global _last_proc_stat
    now = time.monotonic()
    cur = _read_proc_stat()
    if cur is None:
        return {"steal_ms": -1}
    if _last_proc_stat is None:
        _last_proc_stat = (now, cur)
        return {"steal_ms": 0, "warmup": True}
    prev_t, prev = _last_proc_stat
    dt_s = max(now - prev_t, 1e-6)
    _last_proc_stat = (now, cur)
    deltas = {k: max(0, cur.get(k, 0) - prev.get(k, 0)) for k in cur}
    total = sum(deltas.values())
    # Convert jiffies → ms via USER_HZ. If the system actually used 250 Hz
    # this would be off by 2.5×, but USER_HZ=100 is universal on x86_64.
    jiffy_ms = 1000.0 / _USER_HZ
    return {
        "steal_ms": round(deltas.get("steal", 0) * jiffy_ms, 1),
        "iowait_ms": round(deltas.get("iowait", 0) * jiffy_ms, 1),
        "user_ms": round(deltas.get("user", 0) * jiffy_ms, 1),
        "system_ms": round(deltas.get("system", 0) * jiffy_ms, 1),
        "idle_ms": round(deltas.get("idle", 0) * jiffy_ms, 1),
        "total_ms": round(total * jiffy_ms, 1),
        "dt_s": round(dt_s, 3),
    }


async def _loop_tick():
    """Emit one `loop.tick` event every 1 s with snapshot of loop health:
    pending tasks, executor queue depth, GC counts, RSS, plus host CPU
    steal time (definitive signal for hypervisor preemption inside a VM).
    Cheap (~80 us total)."""
    import gc
    while True:
        try:
            await asyncio.sleep(1.0)
            try:
                pending = sum(1 for t in asyncio.all_tasks() if not t.done())
            except Exception:
                pending = -1
            ps = _proc_stat_delta()
            _trace.emit(
                "loop.tick",
                pending_tasks=pending,
                executor_qsize=_executor_qsize(),
                gc_counts=list(gc.get_count()),
                rss_kb=_read_rss_kb(),
                clients=len(_clients) if "_clients" in globals() else 0,
                **ps,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            # Never let this task die quietly; loop.
            await asyncio.sleep(1.0)


def _start_heartbeat():
    global _heartbeat_task, _lag_monitor_task, _loop_tick_task
    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = register_bg_task(asyncio.get_event_loop().create_task(_heartbeat_loop()))
    if _lag_monitor_task is None or _lag_monitor_task.done():
        _lag_monitor_task = register_bg_task(asyncio.get_event_loop().create_task(_loop_lag_monitor()))
    if _loop_tick_task is None or _loop_tick_task.done():
        _loop_tick_task = register_bg_task(asyncio.get_event_loop().create_task(_loop_tick()))


# ---- Shared status poller ----
# Single global task that calls poll_status() + read_errors_nonblocking() once
# per cycle for all clients, eliminating redundant STAT.poll() / GIL contention
# when multiple clients are connected.

_shared_status: Optional["StatusPayload"] = None
_shared_status_dict: Optional[dict] = None  # cached asdict(_shared_status)

# ---- Program-elapsed timer (server-authoritative) ----
# Tracked across status polls so every connected client (including ones that
# joined mid-program) sees the same elapsed time. Anchored to time.monotonic()
# so wall-clock NTP corrections don't jump the counter.
_program_start_mono: Optional[float] = None       # monotonic seconds when current run began
_program_paused_accum_ms: int = 0                  # accumulated paused ms in current run
_program_pause_start_mono: Optional[float] = None  # monotonic seconds of current freeze (pause or end)
_program_active_last: bool = False                 # interp != IDLE last tick
_program_paused_last: bool = False                 # paused last tick
# Pre-encoded msgpack bytes of _shared_status_dict. When the wire format is
# msgpack and no per-client mutation applies (tool_meta injection, delta), each
# client's envelope encode splices these bytes verbatim via msgspec.Raw — one
# encode per tick instead of one per client. None when JSON wire format or
# when the poller has not run yet.
_shared_status_data_msgpack: Optional[bytes] = None
_shared_errors: list = []
_shared_probe_updates: dict = {}
_shared_timing: dict = {}  # poll_ms, errors_ms, parse_ms, poller_ts
# Per-cycle snapshot of connected clients — rebuilt once by the poller so
# every status_loop doesn't repeat the O(N) list-comp (avoids O(N²)/cycle).
# Reference is swapped atomically before _status_event.set(), so readers
# always see a consistent list for the duration of their iteration.
_shared_clients_list: list = []
_surface_points_pending: list | None = None  # latest surface scan points; None = never scanned
_surface_points_version: int = int(time.time())  # bumped each time new data is ready; seeded from startup so ?v= URLs don't collide across restarts
_surface_initialized: bool = False           # True after startup file-read attempted
_comp_grid_pending: dict | None = None       # latest parsed probe-results-grid.json
_comp_grid_version: int = int(time.time())   # bumped each time new grid is ready; seeded from startup so ?v= URLs don't collide across restarts
_comp_grid_initialized: bool = False         # True after startup file-read attempted
_last_comp_hal_ver: int | None = None        # last seen compensation.grid-version HAL value
_caches_ini: Optional[str] = None            # INI path the surface/comp caches were populated for; on change they're invalidated (issue #29)
_tool_table_version: int = int(time.time())  # bumped after every CMD.load_tool_table(); per-client trackers in status_loop send a `tool_table_changed` ping so other clients refetch

# Halshow live loop — pushes value deltas to clients viewing the Settings → Halshow tab.
# Topology (pin/signal/param structure, links) is built once via halcmd subprocess on subscribe;
# values are diffed every 200 ms via hal.get_info_*, which is ~1 ms for the full HAL.
_halshow_loop_task: Optional[asyncio.Task] = None
_halshow_last_values: dict = {}                # "section/name" → last broadcast value (delta source)
_halshow_topology_sent: dict = {}              # client_id → True once topology has been delivered
_halshow_topology_cache: Optional[dict] = None  # built-once HAL graph (P5); HAL graph is static post-boot

# Shared gcode preview — parsed once in a subprocess (gcode_parse_worker.py)
# on file/rotation change. The parsed result (multi-MB polylines) is NOT
# broadcast over the WS — N × ws.send_bytes(2.7 MB) saturates the event-loop
# writer and trips the heartbeat watchdog. Instead: pre-encode the bytes, serve
# them via GET /preview (uvicorn streamed response runs off the WS writer),
# and broadcast a tiny JSON ping per version so clients know to fetch.
_gcode_preview_pending: Optional[dict] = None   # {"file"} metadata only — the polyline lives in _gcode_preview_bytes (worker passthrough); consumers only read .get("file")
_gcode_preview_version: int = int(time.time()) # bumps on file change (or unload); seeded from startup so ?v= URLs don't collide across restarts
_gcode_last_file: Optional[str] = None          # edge detection in poller
_gcode_last_mtime: Optional[float] = None       # re-parse on in-place edits of the same path
_gcode_refresh_running: bool = False            # single-flight guard
# The parse worker's msgpack output, published verbatim (passthrough — never
# decoded on the gateway). Served over HTTP by GET /preview so the 2.7 MB
# polyline payload never touches the WS writer.
_gcode_preview_bytes: Optional[bytes] = None
# Same payload pre-compressed once per parse so each client doesn't pay the
# gzip cost on every fetch. /preview picks one based on Accept-Encoding.
_gcode_preview_bytes_gz: Optional[bytes] = None

# Pre-encoded msgpack bytes of the surface_points / comp_grid data dicts.
# Served over HTTP by GET /surface_points and GET /comp_grid so the 10-80 KB
# per-client fan-out never touches the single-threaded WS writer. Clients
# receive a tiny *_ready JSON ping on version bump and fetch the cached
# bytes out of band.
_surface_points_bytes: Optional[bytes] = None
_comp_grid_bytes: Optional[bytes] = None

# Path to the subprocess parse worker (spawned via subprocess.Popen in a worker
# thread — see _run_gcode_worker_blocking, B7).
_GCODE_WORKER_PATH = str(BASE_DIR / "gcode_parse_worker.py")

# Safety-trip sticky notification: populated when _status_poller() observes the
# servo-thread HAL latch (webui-hb-latch.fault-out) go FALSE→TRUE after a known-
# good baseline (issue #34). Broadcast to all clients in every status message
# until a client sends {cmd:"safety_trip_ack"}. Lives server-side (not per-client)
# so reload / multi-tab all see the same trip.
_unacked_trip: Optional[dict] = None  # {"reason": str}
_last_fault_latched: Optional[bool] = None  # webui-hb-latch.fault-out at last poll
_trip_baseline_seen: bool = False  # have we ever seen the latch clear (FALSE)?

# Warn-once flags for STAT field absence — the cascade itself is correct (different
# LinuxCNC versions expose different fields), but exhausting all candidates without
# a log leaves the operator staring at a blank DRO with no idea why. Reset on every
# successful try_connect_lcnc() so a real failure isn't masked across reconnects.
_machine_pos_warned = False
_spindle_warned = False
_err_poll_warned = False

_status_gen = 0  # incremented each poll; clients compare to skip redundant sends
_status_poller_task: Optional[asyncio.Task] = None
# Broadcast event replaced on every poll cycle. Per-client loops snapshot the
# current event and `await event.wait()` instead of tight-polling _status_gen.
# Lazily allocated on first use so module import doesn't require an event loop.
_status_event: Optional[asyncio.Event] = None

# Per-tick aggregate stats for [STATUS] log. Accumulated as each client's
# status_loop finishes its send; snapshotted + logged on the next poller tick.
# All writers run on the single event loop, so no lock is required.
_status_tick_stats: Dict[str, Any] = {
    "gen": 0, "tick_start": 0.0, "expected": 0, "done": 0,
    "encode_sum": 0.0, "send_sum": 0.0, "send_max": 0.0,
    # === TEMP STATUS-PAYLOAD PROBE === aggregate per-tick wire metrics so
    # the [STATUS] outlier log can attribute encode cost to actual payload
    # size. tool_meta_count: how many clients got a tool_meta block this
    # tick (suspected reconnect-storm inflator).
    "bytes_sum": 0, "bytes_max": 0, "tool_meta_count": 0,
}

# Serializes all CMD.* access. The LinuxCNC NML command channel is not
# thread-safe; concurrent handle_command coroutines with >=2 clients
# corrupted NML state and segfaulted the process before this lock existed.
# Must be held for every CMD.* call — direct, via _cmd_blocking, or via
# asyncio.to_thread. Non-reentrant: helpers (_cmd_blocking, set_mode) assume
# the caller already holds the lock.
_cmd_lock: Optional[asyncio.Lock] = None


def _get_cmd_lock() -> asyncio.Lock:
    global _cmd_lock
    if _cmd_lock is None:
        _cmd_lock = asyncio.Lock()
    return _cmd_lock

# Timing log (toggled via "timing_log" WS command from Debug tab)
_timing_log_enabled = False
_timing_log_path: Optional[str] = None


def _log_timing(timing: dict):
    """Append one JSON line to the current timestamped log file.

    Dev-only path (Debug tab toggle). Open-per-write keeps the file handle
    out of module state — simpler shutdown, no handle to leak if the toggle
    is turned on/off repeatedly. If log volume becomes a concern, migrate to
    a logging.FileHandler.
    """
    if not _timing_log_enabled or not _timing_log_path:
        return
    try:
        timing["ts"] = time.time()
        with open(_timing_log_path, "a") as f:
            f.write(json.dumps(timing) + "\n")
    except OSError as e:
        _trace.emit("timing.log_write_failed", level="warn",
                    exc=type(e).__name__, msg=str(e))


_PID_CHECK_INTERVAL = 5.0  # seconds between pgrep PID checks


def _read_probe_results_file() -> list:
    """Read probe-results.txt and return list of [x, y, z] triples."""
    ini_path = getattr(STAT, "ini_filename", None)
    if not ini_path:
        return []
    path = os.path.join(os.path.dirname(ini_path), "probe-results.txt")
    points = []
    skipped = 0
    sample_err: Optional[str] = None
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3:
                    try:
                        points.append([float(parts[0]), float(parts[1]), float(parts[2])])
                    except ValueError as e:
                        skipped += 1
                        if sample_err is None:
                            sample_err = str(e)[:200]
    if skipped:
        _trace.emit("surface.point_parse_failed", level="warn",
                    path=path, skipped=skipped, sample_err=sample_err,
                    parsed=len(points))
    return points


def _read_comp_grid_file() -> "dict | None":
    """Read probe-results-grid.json and return parsed dict, or None if unavailable."""
    ini_path = getattr(STAT, "ini_filename", None)
    if not ini_path:
        return None
    path = os.path.join(os.path.dirname(ini_path), "probe-results-grid.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            _trace.emit("probe.results_grid_corrupt", level="warn",
                        exc=type(e).__name__, msg=str(e))
            return None


def _poll_and_serialize():
    """Executor-thread helper: poll STAT + serialize to dict in one hop.

    Combines poll_status() and the dataclass→dict conversion so neither
    touches the event loop. Returns (StatusPayload, dict) — the dict is
    cached as _shared_status_dict and consumed (via .copy()) by every
    per-client status_loop.

    The conversion uses `__dict__.copy()` rather than dataclasses.asdict().
    asdict() recursively deep-copies every field; for StatusPayload (no
    nested dataclasses, only primitives + flat lists) the deep copy
    produces the same shape as the shallow copy but cost 100–200 ms under
    storm-time GIL contention (measured 2026-05-02). Shallow copy is
    correct because no consumer mutates the dict's list values.

    Emits poll_status.slow on >50 ms total so we keep visibility on
    regressions.
    """
    _t0 = time.monotonic()
    st = poll_status()
    _t1 = time.monotonic()
    out = st.__dict__.copy()
    _t2 = time.monotonic()
    poll_ms = (_t1 - _t0) * 1000
    serialize_ms = (_t2 - _t1) * 1000
    total_ms = poll_ms + serialize_ms
    if total_ms > 50:
        _trace.emit(
            "poll_status.slow", level="warn",
            poll_ms=round(poll_ms, 1),
            serialize_ms=round(serialize_ms, 1),
            total_ms=round(total_ms, 1),
        )
    return st, out


# Probe result widget names from DEBUG EVAL messages → friendly keys.
# Used by _status_poller below; declared here so the reference at use site
# isn't a forward-reference into the file.
_PROBE_WIDGET_MAP = {
    "x_probed_width":          "x_width",
    "x_center_probed":         "x_center",
    "y_probed_width":          "y_width",
    "y_center_probed":         "y_center",
    "x_minus_probed_position": "x_minus",
    "x_plus_probed_position":  "x_plus",
    "y_minus_probed_position": "y_minus",
    "y_plus_probed_position":  "y_plus",
    "z_minus_probed_position": "z_minus",
    "averaged_diam":           "diameter",
    "edge_delta":              "edge_delta",
    "edge_angle":              "edge_angle",
    "calibration_offset_3032": "cal_offset",
}

_PROBE_EVAL_RE = re.compile(
    r'EVAL\[vcp\.getWidget\{"(\w+)"\}\.setValue\{([^}]+)\}\]',
    re.IGNORECASE,
)


async def _status_poller():
    """Single global poller — polls LinuxCNC once per cycle for all clients.

    All LinuxCNC C extension calls (STAT.poll(), hal.get_value(), NML reads)
    hold the GIL and never release it — run_in_executor provides zero
    parallelism, only thread dispatch overhead (1-5ms per call). Every
    successful LinuxCNC UI (AXIS 50Hz, GMOCCAPY 10Hz, QtVCP 10Hz) polls
    synchronously. We do the same, yielding to the event loop between
    blocking sections so heartbeats and sends can proceed.
    """
    global _shared_status, _shared_status_dict, _shared_errors, _shared_probe_updates
    global _status_gen, lcnc_connected, STAT, CMD, ERR, _reconnect_fails, _shared_timing
    global _surface_points_pending, _surface_points_version, _surface_initialized
    global _comp_grid_pending, _comp_grid_version, _comp_grid_initialized, _last_comp_hal_ver
    global _caches_ini
    global _status_event, _shared_clients_list
    global _gcode_preview_pending, _gcode_preview_version
    global _gcode_last_file, _gcode_last_mtime, _gcode_refresh_running
    global _gcode_preview_bytes, _gcode_preview_bytes_gz
    global _surface_points_bytes, _comp_grid_bytes
    loop = asyncio.get_event_loop()
    _poll_fails = 0
    _last_pid_check = 0.0
    _cycle_start = time.monotonic()
    _was_active = True  # Experiment 4: track active/idle edge for instant wake-up
    # === TEMP IDLE-GATEWAY PROBE === log when the gateway is up but no
    # clients are connected. Helps tell "all tabs reconnected fast" from
    # "tabs are stuck somewhere and never reaching us" in the log alone.
    _idle_last_log = 0.0
    while True:
        try:
            _cycle_start = time.monotonic()

            if not _clients:
                _set_phase("status_poller.idle_wait")
                # Periodic idle marker (every ~5s) so the operator can see
                # at a glance that the gateway is alive but unconnected.
                if _cycle_start - _idle_last_log >= 5.0:
                    _uptime_ms = (_cycle_start - _T0) * 1000
                    print(
                        f"[IDLE] +{_uptime_ms:.0f}ms gateway up, "
                        f"0 clients connected (waiting)",
                        flush=True,
                    )
                    _idle_last_log = _cycle_start
                await asyncio.sleep(0.5)
                continue

            # ---- Reconnection logic (moved from per-client status_loop) ----
            # _get_lcnc_pid + try_connect_lcnc spawn subprocesses (pgrep,
            # 1 s timeout; _nml_connectable, 5 s timeout). Running them on
            # the event loop stalls poll_status long enough to flap the
            # HAL heartbeat and trip oneshot.0.out. Offload to a worker.
            if not lcnc_connected:
                pid = await asyncio.to_thread(_get_lcnc_pid)
                if pid is not None and await asyncio.to_thread(try_connect_lcnc):
                    _reconnect_fails = 0
                    _hal_connect()
                    _poll_fails = 0
                else:
                    if pid is not None and _ever_connected:
                        _reconnect_fails += 1
                        if _reconnect_fails >= _NML_POISON_THRESHOLD:
                            print(f"NML reconnect failed {_reconnect_fails} times with linuxcncsvr alive — restarting gateway")
                            _self_restart()
                    else:
                        _reconnect_fails = 0
                    await asyncio.sleep(2.0)
                    continue

            # ---- Process-level detection: rate-limited PID check ----
            now = time.monotonic()
            if now - _last_pid_check >= _PID_CHECK_INTERVAL:
                _last_pid_check = now
                if await asyncio.to_thread(check_lcnc_instance):
                    if _lcnc_pid is not None:
                        if await asyncio.to_thread(try_connect_lcnc):
                            _reconnect_fails = 0
                            _hal_connect()
                    else:
                        STAT = CMD = ERR = None
                        lcnc_connected = False
                        continue

            # poll_status() blocks ~40ms (GIL held by C extensions).
            # run_in_executor lets the event loop serve HTTP (STL files)
            # between GIL switches during that time. asdict() runs in the
            # same hop so StatusPayload serialisation stays off the event
            # loop too.
            t0 = time.monotonic()
            _set_phase("status_poller.poll_and_serialize")
            st, status_dict = await loop.run_in_executor(None, _poll_and_serialize)
            t1 = time.monotonic()
            _set_phase("status_poller.read_errors")
            # ERR.poll() is a C-extension call that holds the GIL for its
            # NML read; running it inline on the main thread blocks the
            # event loop in the same way STAT.poll does. Move it to the
            # default executor so heartbeat / WS sends can interleave.
            raw_errs = await loop.run_in_executor(None, read_errors_nonblocking)
            t2 = time.monotonic()
            _set_phase("status_poller.post_poll")
            _poll_fails = 0

            # Parse probe results from DEBUG EVAL messages; detect surface scan completion
            errs = []
            probe_updates = {}
            surface_scan_done = False
            OPERATOR_DISPLAY = 13
            for kind, text in raw_errs:
                if kind == OPERATOR_DISPLAY:
                    m = _PROBE_EVAL_RE.search(text)
                    if m:
                        key = _PROBE_WIDGET_MAP.get(m.group(1))
                        if key:
                            try:
                                probe_updates[key] = float(m.group(2))
                            except ValueError as e:
                                _trace.emit_exc(
                                    "probe.widget_value_parse_failed", e,
                                    widget=m.group(1), raw=m.group(2)[:80],
                                )
                            continue
                    elif "LCNC_SURFACE_SCAN_DONE" in text:
                        surface_scan_done = True
                        continue  # consume — don't forward to frontend as an error
                errs.append((kind, text))

            # INI-change invalidation (issue #29): if the active INI changed
            # under a persistent gateway, the surface/comp caches hold the
            # previous config's data. Reset the init flags + clear pending/bytes
            # and bump versions so clients refetch — the init blocks below then
            # reload from the new config's result files (or stay empty).
            _cur_ini = getattr(STAT, "ini_filename", None)
            if _cur_ini and _caches_ini is not None and _caches_ini != _cur_ini:
                _surface_initialized = False
                _comp_grid_initialized = False
                _surface_points_pending = None
                _surface_points_bytes = None
                _comp_grid_pending = None
                _comp_grid_bytes = None
                _surface_points_version += 1
                _comp_grid_version += 1
                _trace.emit("cache.ini_changed_invalidated", old=_caches_ini, new=_cur_ini)
            if _cur_ini:
                _caches_ini = _cur_ini

            # Startup init: push existing probe-results.txt to new clients on first connect
            if not _surface_initialized and getattr(STAT, "ini_filename", None):
                pts = await asyncio.to_thread(_read_probe_results_file)
                if pts:
                    _surface_points_pending = pts
                    _surface_points_bytes = await asyncio.to_thread(
                        _msgspec.msgpack.encode, pts
                    )
                    _surface_points_version += 1
                _surface_initialized = True

            # Scan completion: re-read file and push updated data to all clients
            if surface_scan_done:
                pts = await asyncio.to_thread(_read_probe_results_file)
                if pts:
                    _surface_points_pending = pts
                    _surface_points_bytes = await asyncio.to_thread(
                        _msgspec.msgpack.encode, pts
                    )
                    _surface_points_version += 1
                # Tell compensation.py the file is complete and ready to load.
                # Wrapped: compensation.py may not be loaded in all configs.
                try:
                    await _reader_request("set_p", pin="compensation.reload-req", value=str(int(time.time())))
                except Exception as e:
                    _trace.emit("comp.reload_req_failed", level="warn",
                                exc=type(e).__name__, msg=str(e),
                                hint="compensation.py not loaded?")

            # Comp grid startup init: push existing probe-results-grid.json on first connect
            if not _comp_grid_initialized and getattr(STAT, "ini_filename", None):
                t_read = time.monotonic()
                grid = await asyncio.to_thread(_read_comp_grid_file)
                t_read_done = time.monotonic()
                if grid:
                    _comp_grid_pending = grid
                    _comp_grid_bytes = await asyncio.to_thread(
                        _msgspec.msgpack.encode, grid
                    )
                    t_enc_done = time.monotonic()
                    _comp_grid_version += 1
                    print(
                        f"[COMP] publish v={_comp_grid_version} trigger=init "
                        f"file_ms={(t_read_done - t_read)*1000:.0f} "
                        f"encode_ms={(t_enc_done - t_read_done)*1000:.0f} "
                        f"bytes={len(_comp_grid_bytes)}B "
                        f"total_ms={(t_enc_done - t_read)*1000:.0f}",
                        flush=True,
                    )
                _last_comp_hal_ver = st.comp_grid_version  # sync — prevents re-fire below
                _comp_grid_initialized = True

            # Comp grid update: detect via compensation.grid-version HAL pin
            if _comp_grid_initialized and st.comp_grid_version is not None \
                    and st.comp_grid_version != _last_comp_hal_ver:
                t_read = time.monotonic()
                grid = await asyncio.to_thread(_read_comp_grid_file)
                t_read_done = time.monotonic()
                if grid:
                    _comp_grid_pending = grid
                    _comp_grid_bytes = await asyncio.to_thread(
                        _msgspec.msgpack.encode, grid
                    )
                    t_enc_done = time.monotonic()
                    _comp_grid_version += 1
                    print(
                        f"[COMP] publish v={_comp_grid_version} trigger=hal "
                        f"hal_ver={st.comp_grid_version} "
                        f"file_ms={(t_read_done - t_read)*1000:.0f} "
                        f"encode_ms={(t_enc_done - t_read_done)*1000:.0f} "
                        f"bytes={len(_comp_grid_bytes)}B "
                        f"total_ms={(t_enc_done - t_read)*1000:.0f}",
                        flush=True,
                    )
                _last_comp_hal_ver = st.comp_grid_version

            # Gcode preview: parse once (in subprocess) on file change, share
            # to all clients via version counter. Single-flight via
            # _gcode_refresh_running so rapid-fire loads don't stack
            # subprocesses. Parse output is WCS-invariant (worker un-rotates
            # and un-offsets), so rotation edits and WCS switches never
            # trigger a re-parse — frontend re-applies LIVE origin+rotation
            # as scene-graph updates.
            # Edge on either the path OR the file's mtime: re-loading the same
            # path after an in-place edit (web editor Save, or an external
            # edit) keeps active_file constant, so without the mtime check the
            # preview would never re-parse and the UI would show stale content.
            if st.active_file:
                try:
                    _cur_mtime = os.path.getmtime(st.active_file)
                except OSError:
                    # File momentarily absent (e.g. mid atomic-write swap).
                    # Leave _gcode_last_mtime as-is; next tick re-checks.
                    _cur_mtime = _gcode_last_mtime
            else:
                _cur_mtime = None
            file_changed = bool(st.active_file) and (
                st.active_file != _gcode_last_file or _cur_mtime != _gcode_last_mtime
            )
            if file_changed and not _gcode_refresh_running:
                _gcode_refresh_running = True
                register_bg_task(asyncio.create_task(_refresh_gcode_preview(st.active_file)))
            elif not st.active_file and _gcode_last_file is not None:
                _gcode_preview_pending = None
                _gcode_preview_bytes = None
                _gcode_preview_bytes_gz = None
                _gcode_preview_version += 1
                _gcode_last_file = None
                _gcode_last_mtime = None

            # Safety-trip detection via the servo-thread HAL latch level
            # (webui-hb-latch.fault-out, issue #34). The latch is sticky and
            # owned by HAL — independent of both this poller and the watchdog
            # process — so reading it here survives gateway stalls AND watchdog
            # restarts: the level still reads TRUE on resume. evaluate_trip_latch
            # is a pure state machine (unit-tested in gateway_util) that edge-
            # detects a clean FALSE→TRUE only after a known-good baseline, so a
            # boot-faulted latch doesn't spuriously banner.
            global _last_fault_latched, _trip_baseline_seen, _unacked_trip
            _tl = evaluate_trip_latch(
                _reader_get("trip_latched"), _last_fault_latched, _trip_baseline_seen
            )
            _last_fault_latched = _tl["last_latched"]
            _trip_baseline_seen = _tl["baseline_seen"]
            if _tl["faulted_on_connect"]:
                # First-sight TRUE: boot-in-ESTOP or a trip while the gateway was
                # absent — ambiguous, so audit it but don't raise the banner (the
                # real ESTOP state is already shown via STAT).
                _trace.emit("safety.latch_faulted_on_connect", level="warn")
            if _tl["tripped"] and _unacked_trip is None:
                # No timestamp: the level is only read inside the status loop,
                # which doesn't run when no clients are connected, so "when the
                # gateway noticed" != "when the trip happened" and would mislead
                # operators. Drop it; the trace event below carries its own ts.
                _unacked_trip = {"reason": "hal_heartbeat_timeout"}
                _trace.emit("safety.tripped", level="error")
                # Auto-snapshot the trace bus into a forensic bundle so the trip
                # is reconstructable end-to-end without operator action. Run in
                # to_thread so the bundler subprocess can't block the asyncio
                # loop or the safety-trip status update.
                _trip_ts_ns = int(time.time_ns())
                asyncio.create_task(
                    asyncio.to_thread(_snapshot_trip, _trip_ts_ns)
                )

            # Cache results for per-client loops
            _shared_status = st
            _shared_status_dict = status_dict
            # Pre-encode the shared `data` dict into msgpack bytes once per tick so
            # each client's envelope encode can splice via msgspec.Raw instead of
            # re-encoding the identical payload N times. Only the `_use_shared`
            # path consumes it, and that path requires `not _STATUS_DELTA_ENABLED`
            # — so when delta mode is on (each client diffs+encodes its own frame)
            # this full-status encode every tick was pure wasted loop work. JSON
            # wire format has no Raw-splice primitive, so we skip there too.
            if _WIRE_FORMAT == "msgpack" and not _STATUS_DELTA_ENABLED:
                _set_phase("status_poller.shared_msgpack_encode")
                _t_enc = time.monotonic()
                _shared_status_data_msgpack = _msgpack_encoder.encode(status_dict)
                shared_encode_ms = round((time.monotonic() - _t_enc) * 1000, 2)
            else:
                _shared_status_data_msgpack = None
                shared_encode_ms = 0.0
            _set_phase("status_poller.build_clients_list")
            _shared_clients_list = [
                {"ip": c.ip, "armed": c.armed} for c in _clients.values()
            ]
            t3 = time.monotonic()
            _shared_errors = errs
            _shared_probe_updates = probe_updates
            poll_ms = round((t1 - t0) * 1000, 2)
            errors_ms = round((t2 - t1) * 1000, 2)
            parse_ms = round((t3 - t2) * 1000, 2)
            cycle_ms = round((t3 - _cycle_start) * 1000, 2)
            # overhead = cycle - measured work (exact by construction).
            overhead_ms = round(cycle_ms - poll_ms - errors_ms - parse_ms, 2)
            _shared_timing = {
                "cycle_ms": cycle_ms,
                "overhead_ms": overhead_ms,
                "poll_ms": poll_ms,
                "errors_ms": errors_ms,
                "parse_ms": parse_ms,
                "shared_encode_ms": shared_encode_ms,
                "poller_ts": t3,
            }
            # Snapshot prior tick's aggregate before rolling gen. Silent under
            # normal load. wall_ms naturally ≈ heartbeat period (~33 ms at
            # 30 Hz active, ~200 ms at 5 Hz idle), so we only log on genuine
            # outliers: an unfinished client (done<expected → real stall),
            # a slow per-client send (send_max>20 ms), or wall_ms well above
            # the idle floor (>400 ms but still under the 500 ms HAL trip).
            _s = _status_tick_stats
            if _s["expected"] > 0:
                _wall_ms = (time.monotonic() - _s["tick_start"]) * 1000
                if (
                    _s["done"] < _s["expected"]
                    or _s["send_max"] > 20
                    or _wall_ms > 400
                ):
                    # Include this tick's poller-side breakdown so outliers
                    # show *where* the time went (poll vs encode vs send vs
                    # untracked overhead). Without these, a 510 ms wall_ms
                    # with 66 ms of encode+send leaves ~444 ms unexplained.
                    print(
                        f"[STATUS] tick gen={_s['gen']} "
                        f"clients={_s['done']}/{_s['expected']} "
                        f"poll_ms={poll_ms:.0f} "
                        f"errors_ms={errors_ms:.0f} "
                        f"parse_ms={parse_ms:.0f} "
                        f"overhead_ms={overhead_ms:.0f} "
                        f"shared_encode_ms={shared_encode_ms:.0f} "
                        f"encode_sum_ms={_s['encode_sum']:.0f} "
                        f"send_sum_ms={_s['send_sum']:.0f} "
                        f"send_max_ms={_s['send_max']:.0f} "
                        f"bytes_sum={_s['bytes_sum']} "
                        f"bytes_max={_s['bytes_max']} "
                        f"tool_meta_n={_s['tool_meta_count']} "
                        f"wall_ms={_wall_ms:.0f}",
                        flush=True,
                    )

            # Broadcast: swap in a fresh unset event for future waiters, then
            # set the old one to wake all current waiters. This avoids the
            # clear/set race where a client checking _status_gen between set()
            # and clear() could miss the wake-up.
            _status_gen += 1
            old_evt = _status_event
            _status_event = asyncio.Event()
            # Exclude hidden tabs from expected — they intentionally skip the
            # send loop, so counting them as "expected to finish" would flag
            # every tick as an outlier and spam the log. send_pending peers
            # stay in the count: if a send actually wedges across ticks, the
            # outlier log should still fire.
            _status_tick_stats.update({
                "gen": _status_gen,
                "tick_start": time.monotonic(),
                "expected": sum(1 for c in _clients.values() if not c.hidden),
                "done": 0, "encode_sum": 0.0, "send_sum": 0.0, "send_max": 0.0,
                "bytes_sum": 0, "bytes_max": 0, "tool_meta_count": 0,
            })
            if old_evt is not None:
                old_evt.set()

        except Exception as e:
            _poll_fails += 1
            if _poll_fails >= 5:
                lcnc_connected = False
                STAT = CMD = ERR = None
                _poll_fails = 0
                _trace.emit("poller.persistent_failure", level="warn",
                            exc=type(e).__name__, msg=str(e))

        # Adaptive sleep: subtract time already spent polling this cycle
        elapsed = time.monotonic() - _cycle_start

        # Experiment 4: adaptive poll rate — poll at IDLE_POLL_HZ when the
        # machine is idle (interp idle, not moving, no motion mode); full
        # POLL_HZ otherwise. Instant wake-up on idle→active transition keeps
        # first-motion latency at most one poll cycle, not one idle cycle.
        if _ADAPTIVE_POLL_ENABLED and _shared_status is not None:
            st = _shared_status
            _is_active = (
                (st.interp_state is not None and st.interp_state != linuxcnc.INTERP_IDLE)
                or (st.task_mode in (linuxcnc.MODE_AUTO, linuxcnc.MODE_MDI))
                or (st.current_vel is not None and abs(st.current_vel) > 0.001)
                or (st.inpos is False)
                or (st.tool_change_requested is True)
            )
            if _is_active and not _was_active:
                # idle → active: skip the sleep, tick immediately
                _was_active = True
                continue
            _was_active = _is_active
            target_hz = POLL_HZ if _is_active else _IDLE_POLL_HZ
        else:
            target_hz = POLL_HZ

        await asyncio.sleep(max(0, (1.0 / target_hz) - elapsed))


def _start_status_poller():
    global _status_poller_task
    if _status_poller_task is None or _status_poller_task.done():
        _status_poller_task = register_bg_task(asyncio.get_event_loop().create_task(_status_poller()))


_reader_task: Optional[asyncio.Task] = None


def _start_reader_recv_loop():
    """Start the long-running webui-reader IPC client task once."""
    global _reader_task
    if _reader_task is None or _reader_task.done():
        _reader_task = register_bg_task(asyncio.get_event_loop().create_task(_reader_recv_loop()))


def _get_lcnc_pid() -> Optional[int]:
    """Return PID of linuxcncsvr if running, else None.
    Fast path: check /proc/<pid>/comm for known PID (<0.1ms).
    Slow path: pgrep subprocess only for initial discovery (~42ms).
    """
    # Fast path: verify known PID is still alive and correct process
    if _lcnc_pid is not None:
        try:
            with open(f"/proc/{_lcnc_pid}/comm") as f:
                if f.read().strip() == "linuxcncsvr":
                    return _lcnc_pid
        except FileNotFoundError:
            pass  # PID is stale — process exited, fall through to pgrep
        except OSError as e:
            # ENOENT is "process gone" (legit). Anything else (EACCES, EIO)
            # is unexpected on /proc and worth surfacing.
            _trace.emit("pid.proc_comm_read_failed", level="warn",
                        pid=_lcnc_pid, exc=type(e).__name__, msg=str(e))
    # Slow path: discover PID via pgrep (only when PID unknown or stale)
    try:
        result = subprocess.run(
            ['pgrep', '-x', 'linuxcncsvr'],
            capture_output=True, text=True, timeout=1,
        )
        if result.returncode == 0:
            return int(result.stdout.strip().split('\n')[0])
    except Exception as e:
        # rc != 0 from pgrep = "no match" (legit, returns None below). This
        # except catches subprocess raising (timeout, OSError, ValueError on
        # parse) — all worth surfacing.
        _trace.emit("pid.pgrep_failed", level="warn",
                    exc=type(e).__name__, msg=str(e))
    return None


def _nml_connectable() -> bool:
    """Test NML connectivity in a disposable subprocess.
    Prevents the main process from touching stale/unready NML."""
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import linuxcnc; s=linuxcnc.stat(); s.poll(); print('OK')"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and "OK" in result.stdout
    except Exception:
        return False


# ---- NML poisoning detection ----
_reconnect_fails = 0
_ever_connected = False     # set True on first successful connection
_NML_POISON_THRESHOLD = 60  # consecutive probe-pass + main-fail = poisoned NML (single global poller)


def _self_restart():
    """Replace this process with a fresh gateway. Last resort for NML poisoning.

    os.execv atomically swaps the process image — no parent/child handoff
    window, no stale PID, file descriptors that aren't FD_CLOEXEC are
    inherited but the gateway re-opens its own HAL/socket state. Falls back
    to os._exit so the launcher can respawn if execv fails.
    """
    print("NML POISONED: self-restarting gateway process", flush=True)
    _hal_disconnect()
    try:
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn"] + sys.argv[1:])
    except Exception as e:
        _trace.emit("restart.execv_failed", level="error",
                    exc=type(e).__name__, msg=str(e),
                    hint="exiting; launcher will respawn")
        os._exit(1)


def try_connect_lcnc() -> bool:
    """Attempt to connect to LinuxCNC. Returns True on success."""
    global STAT, CMD, ERR, lcnc_connected, _lcnc_pid, _nc_files_dir, _ini_config, _ever_connected
    global _machine_pos_warned, _spindle_warned, _err_poll_warned
    global _var_file_path_cache_key, _var_file_path_cache_val
    _nc_files_dir = None        # re-resolve on reconnect
    _ini_config = None          # re-read INI config on reconnect
    _var_file_path_cache_key = None  # re-resolve var-file path on reconnect (P2.1)
    _var_file_path_cache_val = None
    if not _nml_connectable():
        return False
    try:
        STAT = linuxcnc.stat()
        CMD = linuxcnc.command()
        ERR = linuxcnc.error_channel()
        STAT.poll()  # verify it actually works
        lcnc_connected = True
        _ever_connected = True
        _lcnc_pid = _get_lcnc_pid()
        # Re-arm warn-once flags so a STAT field that disappears across a
        # reconnect produces a fresh log line instead of being suppressed.
        _machine_pos_warned = False
        _spindle_warned = False
        _err_poll_warned = False
        print(f"[VINIT] try_connect_lcnc OK, pid={_lcnc_pid}", flush=True)
        return True
    except Exception as e:
        print(f"[VINIT] try_connect_lcnc FAILED: {e}", flush=True)
        STAT = CMD = ERR = None
        lcnc_connected = False
        _lcnc_pid = None
        return False


def check_lcnc_instance() -> bool:
    """Check if linuxcncsvr PID changed. Returns True if reconnect needed."""
    global _lcnc_pid, lcnc_connected, _tool_tbl_path, _tool_tbl_ini
    pid = _get_lcnc_pid()
    if pid == _lcnc_pid:
        if pid is None and lcnc_connected:
            print(f"[VINIT] check_lcnc_instance: PID=None but was connected, resetting", flush=True)
            lcnc_connected = False
            _tool_tbl_path = None  # re-resolve on next connection
            _tool_tbl_ini = None
            return True
        return False
    # PID changed (appeared, disappeared, or different instance)
    global _wcs_var_file_mtime
    old_pid = _lcnc_pid
    _lcnc_pid = pid
    _tool_tbl_path = None  # config may have changed, re-resolve from INI
    _tool_tbl_ini = None
    _wcs_var_file_mtime = None  # force re-seed of WCS cache from var file on next poll
    if pid is None:
        print(f"[VINIT] check_lcnc_instance: PID gone (was {old_pid}), disconnecting", flush=True)
        lcnc_connected = False
        _hal_disconnect()
    else:
        print(f"[VINIT] check_lcnc_instance: PID changed {old_pid} -> {pid}", flush=True)
    return True


# Best-effort connection at startup (gateway still runs if LinuxCNC isn't up yet)
_trace.emit("boot.lcnc_connect_start")
if _get_lcnc_pid() is not None:
    _bt = time.monotonic()
    try_connect_lcnc()
    _trace.emit(
        "boot.lcnc_connect",
        dt_ms=round((time.monotonic() - _bt) * 1000, 1),
        connected=lcnc_connected,
    )
    if lcnc_connected:
        _bt = time.monotonic()
        _hal_connect()
        _trace.emit(
            "boot.hal_connect",
            dt_ms=round((time.monotonic() - _bt) * 1000, 1),
            sock_ok=_hal_sock is not None,
        )
else:
    _trace.emit("boot.lcnc_skip", reason="no linuxcncsvr pid")


# ---- NC files directory ----
# ALLOWED_EXTENSIONS now lives in gateway_util (imported at top).
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB (G-code can legitimately be this large)
# Fusion tool-library import (P1.2): json.loads holds the GIL in the worker thread
# (~6 ms/MB measured), so it can't be isolated from the heartbeat. Realistic
# libraries are <2 MB (<15 ms); cap at 16 MB (~100 ms worst case, sub-trip, machine
# idle during import) so a bogus 50 MB upload can't approach the watchdog budget.
MAX_TOOL_LIBRARY_SIZE = 16 * 1024 * 1024  # 16 MB
_nc_files_dir: Optional[str] = None
_nc_files_ini: Optional[str] = None   # INI identity the _nc_files_dir cache is keyed to
_ini_config: Optional[dict] = None


def get_max_jog_velocity() -> Optional[float]:
    """Return max jog velocity from INI (units/sec), cached."""
    return get_ini_config().get("max_jog_velocity")


def get_ini_config() -> dict:
    """Read static INI settings for the UI, cached until reconnect."""
    global _ini_config
    if _ini_config is not None:
        return _ini_config

    config: dict = {}
    if STAT is None:
        return config

    try:
        STAT.poll()
        ini_path = getattr(STAT, "ini_filename", None)
        if not ini_path:
            return config

        ini = linuxcnc.ini(ini_path)

        # Machine linear unit for increment conversion
        linear_unit = (ini.find("TRAJ", "LINEAR_UNITS") or "mm").strip().lower()

        # Ground truth velocity from trajectory planner (always u/s)
        traj_max = _ini_float(ini, "TRAJ", "MAX_LINEAR_VELOCITY")

        # Display velocity values (should be u/s, but some configs use u/min)
        disp_max = _ini_float(ini, "DISPLAY", "MAX_LINEAR_VELOCITY")
        disp_default = _ini_float(ini, "DISPLAY", "DEFAULT_LINEAR_VELOCITY")
        disp_min = _ini_float(ini, "DISPLAY", "MIN_LINEAR_VELOCITY")

        # Heuristic: if DISPLAY MAX is >10x TRAJ MAX, assume u/min → convert
        vel_divisor = 1.0
        if traj_max and disp_max and disp_max > traj_max * 10:
            vel_divisor = 60.0

        config["max_jog_velocity"] = (disp_max / vel_divisor) if disp_max else traj_max
        config["default_jog_velocity"] = (disp_default / vel_divisor) if disp_default else None
        config["min_jog_velocity"] = (disp_min / vel_divisor) if disp_min else None

        # Angular (rotary) jog velocity — always deg/s, no unit conversion
        ang_max = _ini_float(ini, "DISPLAY", "MAX_ANGULAR_VELOCITY")
        ang_default = _ini_float(ini, "DISPLAY", "DEFAULT_ANGULAR_VELOCITY")
        ang_min = _ini_float(ini, "DISPLAY", "MIN_ANGULAR_VELOCITY")
        config["max_angular_jog_velocity"] = ang_max
        config["default_angular_jog_velocity"] = ang_default
        config["min_angular_jog_velocity"] = ang_min

        # Machine native unit (for DRO labels, jog increments, etc.)
        config["linear_units"] = "in" if linear_unit in ("inch", "in", "imperial") else "mm"

        # Jog increments [DISPLAY]
        raw_incr = ini.find("DISPLAY", "INCREMENTS")
        config["increments"] = _parse_increments(raw_incr, linear_unit) if raw_incr else None

        # Spindle defaults [DISPLAY]
        config["default_spindle_speed"] = _ini_float(ini, "DISPLAY", "DEFAULT_SPINDLE_SPEED")
        config["min_spindle_override"] = _ini_float(ini, "DISPLAY", "MIN_SPINDLE_OVERRIDE")
        config["max_spindle_override"] = _ini_float(ini, "DISPLAY", "MAX_SPINDLE_OVERRIDE")

        # Feed override [DISPLAY]
        config["max_feed_override"] = _ini_float(ini, "DISPLAY", "MAX_FEED_OVERRIDE")

        # Debug mode [DISPLAY] — shows sim trip and other debug features in UI
        debug_raw = ini.find("DISPLAY", "DEBUG")
        config["debug"] = bool(int(debug_raw)) if debug_raw else False

        # Spindle speed limits — try SPINDLE_0 through SPINDLE_7
        # (LinuxCNC's EMCMOT_MAX_SPINDLES default; higher requires a rebuild)
        for i in range(8):
            section = f"SPINDLE_{i}"
            v = _ini_float(ini, section, "MAX_FORWARD_VELOCITY")
            if v is not None:
                config["max_spindle_speed"] = v
                config["min_spindle_speed"] = _ini_float(ini, section, "MIN_FORWARD_VELOCITY")
                config["spindle_increment"] = _ini_float(ini, section, "INCREMENT")
                break

        # Subroutine paths for probe macros [RS274NGC]SUBROUTINE_PATH
        sub_raw = ini.find("RS274NGC", "SUBROUTINE_PATH")
        if sub_raw:
            ini_dir = os.path.dirname(ini_path)
            sub_dirs = []
            for p in sub_raw.split(":"):
                p = p.strip()
                if not p:
                    continue
                if not os.path.isabs(p):
                    p = os.path.join(ini_dir, p)
                p = os.path.realpath(p)
                if os.path.isdir(p):
                    sub_dirs.append(p)
            config["subroutine_paths"] = sub_dirs

        _ini_config = config
    except Exception as e:
        _trace.emit("ini.parse_failed", level="warn",
                    exc=type(e).__name__, msg=str(e))

    return config


def get_probe_macros() -> list:
    """List probe macro names from subroutine paths."""
    cfg = get_ini_config()
    paths = cfg.get("subroutine_paths", [])
    macros = []
    seen = set()
    for d in paths:
        try:
            for f in sorted(os.listdir(d)):
                if f.startswith("probe_") and f.endswith(".ngc") and f not in seen:
                    seen.add(f)
                    macros.append(f[:-4])  # strip .ngc
        except OSError:
            continue
    return macros


def get_nc_files_dir() -> str:
    """Return NC files directory from the LinuxCNC INI, fallback ~/linuxcnc/nc_files.

    Reads PROGRAM_PREFIX from LCNC_INI_FILE (exported by the launcher) — the same
    boot-independent source get_tool_tbl_path() uses — instead of STAT.ini_filename,
    so it never polls the NML status channel on the event loop and works before
    try_connect_lcnc succeeds (B4). Cached and keyed to the active INI so an INI
    change re-resolves; only a tiny INI parse on first use / INI change.
    """
    global _nc_files_dir, _nc_files_ini
    ini_path = os.environ.get("LCNC_INI_FILE")
    if _nc_files_dir is not None and ini_path == _nc_files_ini:
        return _nc_files_dir

    resolved = None
    if ini_path:
        try:
            ini = linuxcnc.ini(ini_path)
            prefix = ini.find("DISPLAY", "PROGRAM_PREFIX")
            if prefix:
                if not os.path.isabs(prefix):
                    prefix = os.path.join(os.path.dirname(ini_path), prefix)
                prefix = os.path.realpath(prefix)
                if os.path.isdir(prefix):
                    resolved = prefix
        except Exception as e:
            _trace.emit_exc("ini.nc_files_parse_failed", e, ini_path=ini_path)

    if resolved is None:
        resolved = os.path.expanduser("~/linuxcnc/nc_files")
        os.makedirs(resolved, exist_ok=True)

    _nc_files_dir = resolved
    _nc_files_ini = ini_path
    return _nc_files_dir


# ---- Tool Table ----
TOOL_LIBRARY_PATH = BASE_DIR / "tool_library.json"
_tool_tbl_path: Optional[str] = None
_tool_tbl_ini: Optional[str] = None
_tool_meta_dirty = False


def get_tool_tbl_path() -> Optional[str]:
    """Resolve the tool table file path from the LinuxCNC INI.

    Uses LCNC_INI_FILE (exported by the launcher) as the single source of
    truth — independent of the gateway↔LinuxCNC status connection, so this
    works during the boot window before `try_connect_lcnc` succeeds.
    """
    global _tool_tbl_path, _tool_tbl_ini
    ini_path = os.environ.get("LCNC_INI_FILE")
    if not ini_path:
        return None
    if _tool_tbl_path is not None:
        if ini_path != _tool_tbl_ini:
            _tool_tbl_path = None
            _tool_tbl_ini = None
        else:
            return _tool_tbl_path
    try:
        ini = linuxcnc.ini(ini_path)
        tbl = ini.find("EMCIO", "TOOL_TABLE")
        if not tbl:
            return None
        if not os.path.isabs(tbl):
            tbl = os.path.join(os.path.dirname(ini_path), tbl)
        tbl = os.path.realpath(tbl)
        if os.path.isfile(tbl):
            _tool_tbl_path = tbl
            _tool_tbl_ini = ini_path
            return _tool_tbl_path
    except Exception as e:
        _trace.emit_exc("tool_tbl.path_resolve_failed", e, ini_path=ini_path)
    return None


# parse_tool_table / write_tool_table / _merge_tool_data now live in
# tool_table.py, and atomic_write_bytes in gateway_util — both imported at top
# (gateway modularization, issue #33).


async def _reload_tool_table_and_bump():
    """Reload tool.tbl into LinuxCNC and bump the broadcast version.

    Status_loop sends `tool_table_changed` to every client whose
    `_last_tool_table_version` lags this counter, so a remote edit
    propagates within one tick instead of waiting for manual refresh.
    """
    global _tool_table_version
    if CMD:
        await _cmd_blocking(CMD.load_tool_table, wait=None)
    _tool_table_version += 1


async def _persist_imported_tools(tbl_path: str, tbl_tools: list, library: dict) -> None:
    """Persist a full tool-table replacement (the REST Fusion import) atomically
    w.r.t. every other tool / NML operation (issue #24).

    Serialized under `_cmd_lock` — not a separate lock — for two reasons: the WS
    tool handlers (save/add/delete/renumber) already serialize on it via
    `handle_command`, and the reload touches the NML command channel, whose
    thread-safety contract requires that lock. The REST import previously
    bypassed `_cmd_lock`, so it could both race those handlers on tool.tbl /
    tool_library.json (lost update) AND call `_cmd_blocking` without the lock.
    The blocking file writes run off the event loop via `run_in_executor`."""
    loop = asyncio.get_event_loop()
    async with _get_cmd_lock():
        await loop.run_in_executor(None, write_tool_table, tbl_path, tbl_tools)
        await _reload_tool_table_and_bump()
        await loop.run_in_executor(None, save_tool_library, library)


def _current_ini_path() -> str:
    """Return the current INI file path, or 'default' if unavailable."""
    if STAT and getattr(STAT, "ini_filename", None):
        return STAT.ini_filename
    return "default"


def _tool_lib_error(event: str, level: str, e: Exception) -> None:
    _trace.emit(event, level=level, path=str(TOOL_LIBRARY_PATH),
                exc=type(e).__name__, msg=str(e))


# tool_library.json persistence lives in tool_store.py (issue #33). The store
# owns the mtime cache + the strict refuse-to-clobber write; the current-INI key
# (_current_ini_path → STAT) and trace are injected. Thin wrappers below keep the
# existing call sites stable.
_tool_lib_store = ToolLibraryStore(TOOL_LIBRARY_PATH, _current_ini_path,
                                   on_error=_tool_lib_error)


def load_tool_library() -> dict:
    return _tool_lib_store.load()


def save_tool_library(library: dict):
    _tool_lib_store.save(library)


# _TOOL_META_FIELDS now lives in tool_table.py (imported at top, #33).


# ---- Server-Side Settings ----
SETTINGS_PATH = BASE_DIR / "settings.json"
_fb_scale = 60  # spindle feedback scale: 60 (RPS→RPM) or 1 (already RPM)
_spindle_load_pin = ""  # HAL pin for spindle load %, empty = disabled
_tc_info_cache: dict = {}  # {(tool_num, tbl_mtime): merged_list} — one entry max
_HAL_PIN_RE = re.compile(r'^[a-zA-Z0-9_][a-zA-Z0-9_.:-]*$')


def _settings_load_error(e: Exception) -> None:
    _trace.emit("settings.load_failed", level="warn",
                path=str(SETTINGS_PATH), exc=type(e).__name__, msg=str(e))


# settings.json persistence lives in settings_store.py (issue #33). The store
# owns the cache, RMW lock, refuse-to-clobber guard and version counter; the
# current-INI key (_current_ini_path → STAT) is injected. Thin wrappers below
# keep the existing call sites stable.
_settings_store = SettingsStore(SETTINGS_PATH, _current_ini_path,
                                on_load_error=_settings_load_error)


def load_settings() -> dict:
    return _settings_store.load()


def save_settings_section(section: str, data):
    _settings_store.save_section(section, data)


def reset_settings():
    _settings_store.reset()


# _merge_tool_data now lives in tool_table.py (imported at top, #33).




# sanitize_filename / validate_extension / validate_path_within now live in
# gateway_util (imported at top) so they're unit-testable without linuxcnc.


@dataclass
class StatusPayload:
    ts: float

    # safety / state
    estop: bool
    enabled: bool
    # HAL safety-chain truth. STAT.estop and STAT.enabled are derived from
    # task_state, which iocontrol drives via *edge* detection on this pin.
    # A chain that was already LOW at the time of an estop_reset / machine_on
    # command is silently missed (issue #14). None ⇒ reader snapshot stale
    # or pin unavailable; the existing reader_stale banner surfaces that.
    emc_enable_in: Optional[bool]
    homed: Optional[bool]  # LinuxCNC stat truth (normalized)
    homed_joints: Optional[list]  # per-joint homed mask (configured joints only)

    # task/motion
    task_mode: Optional[int]
    interp_state: Optional[int]
    paused: Optional[bool]
    state: Optional[int]
    motion_mode: Optional[int]  # TRAJ_MODE_FREE=1, TRAJ_MODE_COORD=2, TRAJ_MODE_TELEOP=3
    inpos: Optional[bool]       # machine is at commanded position
    axis_mask: Optional[int]    # bitmask of configured axes (bit0=X, bit1=Y, bit2=Z, …)
    program_units: Optional[int]  # 1=inch, 2=mm, 3=cm
    current_line: Optional[int]   # interpreter line (read-ahead, ahead of motion_line)
    read_line: Optional[int]      # line being parsed
    call_level: Optional[int]     # subroutine nesting depth

    # offsets and positions
    g5x_index: Optional[int]  # 0=G54, 1=G55, 2=G56, etc.
    g5x_offset: Optional[List[float]]
    g92_offset: Optional[List[float]]
    rotation_xy: Optional[float]
    wcs_table: Optional[List[Dict[str, Any]]]  # all 9 WCS slots (G54–G59.3) w/ per-axis + rotation
    joint_pos: Optional[List[float]]
    tool_offset: Optional[List[float]]
    machine_pos: Optional[List[float]]
    work_pos: Optional[List[float]]
    dtg: Optional[List[float]]

    # misc
    feed_override: Optional[float]
    spindle_override: Optional[float]
    rapid_override: Optional[float]
    feed_override_enabled: Optional[bool]
    spindle_override_enabled: Optional[bool]
    block_delete: Optional[bool]           # block delete (/) switch
    optional_stop: Optional[bool]          # optional stop (M1) switch
    feed_hold_enabled: Optional[bool]      # feed hold allowed
    adaptive_feed_enabled: Optional[bool]  # adaptive feed active
    current_vel: Optional[float]
    spindle_speed: Optional[float]       # commanded (S word)
    spindle_speed_actual: Optional[float] # after override
    spindle_load: Optional[float]        # load % from configurable HAL pin
    spindle_direction: Optional[int]
    active_file: Optional[str]
    motion_line: Optional[int]

    # program elapsed (server-authoritative, mid-program reconnects see true value)
    program_elapsed_ms: Optional[int]

    # active modal codes
    gcodes: Optional[List[int]]
    mcodes: Optional[List[int]]

    # tool (stat-only)
    tool_number: Optional[int]
    tool_diameter: Optional[float]
    tool_length: Optional[float]   # Z length offset (positive magnitude)

    # tool change (HAL iocontrol)
    tool_change_requested: Optional[bool]
    tool_change_tool: Optional[int]
    tool_change_info: Optional[dict]

    # probing
    probe_tripped: Optional[bool]
    probe_input: Optional[bool]
    probing: Optional[bool]
    probed_position: Optional[List[float]]

    # external offset (surface compensation)
    eoffset_z: Optional[float]
    eoffset_enabled: Optional[bool]
    comp_method: Optional[int]  # 0=nearest, 1=linear, 2=cubic
    comp_grid_version: Optional[int]

    # coolant
    flood: Optional[bool]
    mist: Optional[bool]

    # backend-authoritative permission classes (issue #19) — mirror of
    # permissions.ts evaluatePermissions(). The frontend CONSUMES this instead
    # of recomputing. Computed with armed=True (the status payload is a single
    # shared broadcast, so per-client `armed`/`busy` are overlaid client-side).
    # Trailing default so the (unreachable) bare constructor stays valid.
    permissions: Optional[Dict[str, bool]] = None
    # estop/enabled merged with the HAL safety chain (issue #14). Computed ONCE
    # in _policy_state_from_payload and broadcast here so the frontend banner/DRO
    # consume the same merged truth the command policy uses — no duplicated merge
    # (review #5). None until the first poll.
    is_estop: Optional[bool] = None
    is_enabled: Optional[bool] = None




def _policy_state_from_payload(p: "StatusPayload", armed: bool) -> _PolicyMachineState:
    """Build the command-policy MachineState from a status snapshot.

    The estop/enabled HAL-merge lives here (issues #14 + #19): STAT.estop/enabled
    merged with the safety chain (emc_enable_in). poll_status broadcasts the
    result as is_estop/is_enabled, which the frontend banner/DRO consume — so the
    merge rule exists in ONE place (review #5). `armed` is supplied by the caller
    — True for the shared broadcast, the real per-client value for enforcement.

    `busy` is intentionally absent (see command_policy module docstring): it is a
    per-tab client debounce the gateway can't observe, overlaid client-side."""
    emc = p.emc_enable_in
    interp = p.interp_state if p.interp_state is not None else linuxcnc.INTERP_IDLE
    # Treat INTERP_PAUSED as paused even if the STAT.paused flag lags — matching
    # _update_program_timer. Otherwise, in that transient both pause and resume
    # gates close and the operator can't resume a paused program (review #1).
    is_paused = bool(p.paused) or interp == linuxcnc.INTERP_PAUSED
    return _PolicyMachineState(
        armed=armed,
        is_estop=bool(p.estop) or (emc is False),
        is_enabled=bool(p.enabled) and (emc is not False),
        is_homed=bool(p.homed),
        is_idle=(interp == linuxcnc.INTERP_IDLE),
        is_running=(not is_paused)
        and interp in (linuxcnc.INTERP_READING, linuxcnc.INTERP_WAITING),
        is_paused=is_paused,
        eoffset_enabled=bool(p.eoffset_enabled),
    )


def safe_get(attr: str, default=None):
    if STAT is None:
        return default
    return getattr(STAT, attr, default)


def to_float_list(x) -> Optional[List[float]]:
    if x is None:
        return None
    try:
        return [float(v) for v in x]
    except Exception:
        return None


def normalize_homed(homed_val) -> Optional[bool]:
    """LinuxCNC homed confirmation. STAT.homed is a fixed-length tuple of int
    (one slot per possible joint, e.g. length 16); STAT.joints is the configured
    joint count. Slice to that count so unused slots don't drag homed False."""
    if not homed_val:
        return None
    nj = safe_get("joints", 0)
    if not nj:
        return None
    return all(bool(x) for x in homed_val[:nj])

def _ini_float(ini, section: str, key: str):
    v = ini.find(section, key)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_increments(raw: str, linear_unit: str = "mm") -> List[float]:
    """Parse LinuxCNC INCREMENTS string into sorted machine-unit floats.

    Handles comma-separated ('1 mm, .01 in, 10 mil') or space-separated
    ('.01in .001in') formats, fractions ('1/8000 in'), and unit suffixes.
    Converts to machine units based on LINEAR_UNITS from [TRAJ].
    """
    is_metric = linear_unit in ("mm", "metric")
    if is_metric:
        unit_factors = {
            "mm": 1.0, "cm": 10.0, "um": 0.001,
            "in": 25.4, "inch": 25.4, "mil": 0.0254,
        }
    else:
        unit_factors = {
            "in": 1.0, "inch": 1.0, "mil": 0.001,
            "mm": 1.0 / 25.4, "cm": 10.0 / 25.4, "um": 0.001 / 25.4,
        }

    _num_unit_re = re.compile(
        r'([0-9]*\.?[0-9]+(?:/[0-9]+)?)\s*(mm|cm|um|inch|in|mil)?',
        re.IGNORECASE,
    )

    # Split by comma if commas present, else by whitespace
    entries = [e.strip() for e in raw.split(",")] if "," in raw else raw.split()

    result = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        m = _num_unit_re.search(entry)
        if not m:
            continue
        num_str, unit = m.group(1), (m.group(2) or "").lower()
        try:
            if "/" in num_str:
                n, d = num_str.split("/", 1)
                val = float(n) / float(d)
            else:
                val = float(num_str)
        except (ValueError, ZeroDivisionError):
            continue
        if val <= 0:
            continue
        factor = unit_factors.get(unit, 1.0)  # no unit = machine units
        result.append(round(val * factor, 6))

    result.sort()
    return result


def read_machine_limits_from_ini(stat_obj):
    """
    Returns (origin_xyz, size_xyz) from the *active* LinuxCNC INI.

    origin = [xmin, ymin, zmin]
    size   = [xmax-xmin, ymax-ymin, zmax-zmin]
    """
    ini_path = getattr(stat_obj, "ini_filename", None)
    if not ini_path:
        return None

    ini = linuxcnc.ini(ini_path)

    def axis_limits(axis_letter: str, joint_idx: int):
        # Prefer AXIS_X/Y/Z
        sec_axis = f"AXIS_{axis_letter}"
        mn = _ini_float(ini, sec_axis, "MIN_LIMIT")
        mx = _ini_float(ini, sec_axis, "MAX_LIMIT")

        # Fallback to JOINT_*
        if mn is None or mx is None:
            sec_joint = f"JOINT_{joint_idx}"
            mn = _ini_float(ini, sec_joint, "MIN_LIMIT")
            mx = _ini_float(ini, sec_joint, "MAX_LIMIT")

        if mn is None or mx is None:
            return None
        return (mn, mx)

    xl = axis_limits("X", 0)
    yl = axis_limits("Y", 1)
    zl = axis_limits("Z", 2)
    if not xl or not yl or not zl:
        return None

    xmin, xmax = xl
    ymin, ymax = yl
    zmin, zmax = zl

    origin = [xmin, ymin, zmin]
    size = [xmax - xmin, ymax - ymin, zmax - zmin]
    return origin, size


def get_spindle_override() -> Optional[float]:
    val = safe_get("spindle_override", None)
    if val is not None:
        try:
            result = float(val)
            if result > 0:
                return result
        except (TypeError, ValueError):
            pass  # safe-silent: fallback chain handles below

    spindles = safe_get("spindle", None)
    if spindles is not None:
        try:
            s0 = spindles[0]
            if hasattr(s0, 'override'):
                return float(s0.override)
            if isinstance(s0, dict) and 'override' in s0:
                return float(s0['override'])
        except (IndexError, AttributeError, TypeError, ValueError, KeyError):
            pass  # safe-silent: last fallback, caller handles None

    return None


def _read_var_file(path: str, wanted: set) -> Dict[str, float]:
    """Read var file, return {var_number_str: float_value} for wanted keys."""
    result: Dict[str, float] = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[0] in wanted:
                result[parts[0]] = float(parts[1])
    return result


def _write_var_file_updates(var_file: str, str_vars: Dict[str, float]) -> None:
    """Read var_file, replace/insert each {var: value}, atomically write back.

    Sync helper — call via asyncio.to_thread from async handlers so the
    blocking I/O can't stall the event loop.
    """
    with open(var_file) as f:
        lines = f.readlines()
    found = set()
    for i, line in enumerate(lines):
        parts = line.split()
        if len(parts) >= 2 and parts[0] in str_vars:
            lines[i] = f"{parts[0]}\t{str_vars[parts[0]]:.6f}\n"
            found.add(parts[0])
    missing = {k: v for k, v in str_vars.items() if k not in found}
    if missing:
        for k, v in missing.items():
            lines.append(f"{k}\t{v:.6f}\n")
        def _var_key(line):
            try: return int(line.split()[0])
            except (ValueError, IndexError): return 999999
        lines.sort(key=_var_key)
    atomic_write_bytes(var_file, "".join(lines).encode("utf-8"))


# Memoized resolved var-file path, keyed by the active INI filename. Resolving it
# parses the INI (linuxcnc.ini + .find) — wasteful to repeat on every 30 Hz poll
# (P2.1), since PARAMETER_FILE is static for a given INI. Re-resolved only when
# STAT.ini_filename changes; also cleared on reconnect (try_connect_lcnc).
_var_file_path_cache_key: Optional[str] = None
_var_file_path_cache_val: Optional[str] = None


def _resolve_var_file_path() -> Optional[str]:
    """Resolve absolute path to the LinuxCNC var file from the active INI.

    Memoized by INI filename so the INI parse runs once per INI, not on every
    30 Hz poll. A successful resolve AND a configured-but-absent PARAMETER_FILE
    are both cached (both stable for the INI); only a transient `linuxcnc.ini`
    failure is left uncached so it retries next tick.
    """
    global _var_file_path_cache_key, _var_file_path_cache_val
    ini_path = getattr(STAT, "ini_filename", None)
    if not ini_path:
        return None
    if ini_path == _var_file_path_cache_key:
        return _var_file_path_cache_val
    try:
        ini = linuxcnc.ini(ini_path)
    except Exception:
        return None  # transient — don't poison the cache; retry next tick
    var_file = ini.find("RS274NGC", "PARAMETER_FILE")
    if var_file and not os.path.isabs(var_file):
        var_file = os.path.join(os.path.dirname(ini_path), var_file)
    _var_file_path_cache_key = ini_path
    _var_file_path_cache_val = var_file or None
    return _var_file_path_cache_val


def _seed_wcs_cache():
    """Re-read _wcs_cache from the var file. Safe to call repeatedly."""
    global _wcs_var_file_mtime
    try:
        var_file = _resolve_var_file_path()
        if not var_file:
            return
        var_map = {}
        for i, base in enumerate(_WCS_BASES):
            for j, key in enumerate(_WCS_AXIS_KEYS):
                var_map[str(base + 1 + j)] = (i, key)
            var_map[str(base + 10)] = (i, "r")
        raw = _read_var_file(var_file, set(var_map))
        for var_key, value in raw.items():
            idx, field = var_map[var_key]
            _wcs_cache[idx][field] = value
        try:
            _wcs_var_file_mtime = os.path.getmtime(var_file)
        except OSError:
            _wcs_var_file_mtime = None
    except Exception as e:
        _trace.emit("wcs.seed_cache_failed", level="warn",
                    exc=type(e).__name__, msg=str(e))


def _stat_poll_timed(caller: str = "?") -> None:
    """Drop-in replacement for STAT.poll() that times the call and emits
    `stat.poll_slow` on >30 ms. Use from main-thread call sites (handlers)
    so we can localize storm-time GIL contention. The shared poller has
    its own inline probe inside poll_status."""
    if STAT is None:
        return
    t0 = time.monotonic()
    STAT.poll()
    dt_ms = (time.monotonic() - t0) * 1000
    if dt_ms > 30:
        _trace.emit("stat.poll_slow", level="warn",
                    duration_ms=round(dt_ms, 1), caller=caller)


def _update_program_timer(interp_state: Optional[int], paused: bool) -> Optional[int]:
    """Advance the server-authoritative program-elapsed accumulator and
    return the current elapsed time in milliseconds (or None if no program
    has ever run since startup). Called once per status poll.

    Transitions handled:
      idle    → active   start new run (reset accumulator + start anchor)
      running → paused   open a pause segment
      paused  → running  commit pause segment into accumulator
      active  → idle     freeze the elapsed clock at "now"
    """
    global _program_start_mono, _program_paused_accum_ms, _program_pause_start_mono
    global _program_active_last, _program_paused_last

    active = interp_state is not None and interp_state != linuxcnc.INTERP_IDLE
    is_paused = active and (paused or interp_state == linuxcnc.INTERP_PAUSED)
    now_mono = time.monotonic()

    # idle → active: new run
    if active and not _program_active_last:
        _program_start_mono = now_mono
        _program_paused_accum_ms = 0
        _program_pause_start_mono = None

    # running → paused: start pause segment
    elif active and is_paused and not _program_paused_last:
        _program_pause_start_mono = now_mono

    # paused → running: commit pause segment
    elif active and not is_paused and _program_paused_last and _program_pause_start_mono is not None:
        _program_paused_accum_ms += int((now_mono - _program_pause_start_mono) * 1000)
        _program_pause_start_mono = None

    # active → idle while running: freeze the clock at "now" so the final
    # elapsed value stays put after the program ends. If we went idle from
    # PAUSED, _program_pause_start_mono is already set — leave it alone.
    elif not active and _program_active_last and _program_pause_start_mono is None and _program_start_mono is not None:
        _program_pause_start_mono = now_mono

    _program_active_last = active
    _program_paused_last = is_paused

    if _program_start_mono is None:
        return None
    anchor = _program_pause_start_mono if _program_pause_start_mono is not None else now_mono
    return max(0, int((anchor - _program_start_mono) * 1000) - _program_paused_accum_ms)


def poll_status() -> StatusPayload:
    if STAT is None:
        raise RuntimeError("LinuxCNC not connected")
    # Time STAT.poll() in isolation. Trace shows status_poller.poll_and_serialize
    # holding the loop for 700+ms; we don't know yet whether it's STAT.poll
    # itself (LinuxCNC NML read), the var-file mtime check, or Python work.
    # Per-call probe surfaces the actual culprit. Threshold tight enough to
    # catch storm-time elevations (typical poll is <2 ms).
    _stat_t0 = time.monotonic()
    STAT.poll()
    _stat_dt_ms = (time.monotonic() - _stat_t0) * 1000
    if _stat_dt_ms > 30:
        _trace.emit("stat.poll_slow", level="warn",
                    duration_ms=round(_stat_dt_ms, 1), caller="poll_status")

    # ---- safety/state ----
    estop = bool(safe_get("estop", True))
    enabled = bool(safe_get("enabled", False))

    # ---- homing (stat-only truth) ----
    homed_val = safe_get("homed", None)
    homed = normalize_homed(homed_val)

    nj = safe_get("joints", 0)
    homed_joints = [bool(x) for x in homed_val[:nj]] if homed_val and nj else None

    # ---- offsets ----
    g5x_index = safe_get("g5x_index", None)
    g5x = to_float_list(safe_get("g5x_offset", None))
    g92 = to_float_list(safe_get("g92_offset", None))
    rotation_xy = safe_get("rotation_xy", None)

    # Update WCS cache: re-seed from var file whenever its mtime changes.
    # LinuxCNC rewrites the var file on interpreter sync (program end, MDI
    # completion that wrote vars, probe macros). This catches writes to
    # inactive slots. Active slot is overwritten from STAT below — mid-motion
    # authoritative source.
    try:
        _vfp = _resolve_var_file_path()
        if _vfp:
            _vmt = os.path.getmtime(_vfp)
            if _wcs_var_file_mtime is None or _vmt != _wcs_var_file_mtime:
                _seed_wcs_cache()
    except OSError:
        pass  # var file may be momentarily absent during rename-atomic writes
    if g5x_index is not None and g5x is not None:
        ci = g5x_index - 1  # STAT.g5x_index is 1-based
        if 0 <= ci < 9:
            for j, key in enumerate(_WCS_AXIS_KEYS):
                _wcs_cache[ci][key] = g5x[j] if len(g5x) > j else 0.0
            _wcs_cache[ci]["r"] = rotation_xy if rotation_xy is not None else 0.0

    # ---- positions ----
    # Prefer joint_actual_position (live encoder feedback, updates even when
    # machine is off/ESTOP) over actual_position (motion controller output,
    # stops updating when servo loop is disabled).  For trivkins machines
    # joint positions are identical to Cartesian axis positions.
    machine_pos = to_float_list(safe_get("joint_actual_position", None))
    if machine_pos is None:
        machine_pos = to_float_list(safe_get("actual_position", None))
    if machine_pos is None:
        machine_pos = to_float_list(safe_get("position", None))
    if machine_pos is None:
        global _machine_pos_warned
        if not _machine_pos_warned:
            _trace.emit("poller.no_machine_pos", level="warn",
                        msg="STAT exposes no joint_actual_position / actual_position / position — DRO blank")
            _machine_pos_warned = True

    # Tool offset vector (active tool length comp)
    tool_offset = to_float_list(safe_get("tool_offset", None))

    # Work position (matches AXIS / GMOCCAPY / QtPyVCP convention):
    #   rel = machine_pos − g5x − tool_offset
    #   rotate (rel.x, rel.y) by −rotation_xy
    #   work_pos = rel − g92
    # G92 is applied AFTER rotation per LinuxCNC coordinate-system spec, so a
    # G92 offset typed in the rotated WCS frame stays aligned with that frame.
    work_pos = None
    if machine_pos is not None:
        work_pos = machine_pos.copy()

        if g5x is not None:
            for i in range(min(len(work_pos), len(g5x))):
                work_pos[i] -= g5x[i]

        if tool_offset is not None:
            for i in range(min(len(work_pos), len(tool_offset))):
                work_pos[i] -= tool_offset[i]

        if rotation_xy and len(work_pos) >= 2:
            t = -math.radians(rotation_xy)
            c, s = math.cos(t), math.sin(t)
            x, y = work_pos[0], work_pos[1]
            work_pos[0] = x * c - y * s
            work_pos[1] = x * s + y * c

        if g92 is not None:
            for i in range(min(len(work_pos), len(g92))):
                work_pos[i] -= g92[i]

    # RAW joint positions (for driving the machine model / spindle nose)
    jpos = safe_get("joint_actual_position", None)
    if jpos is None:
        jpos = safe_get("joint_position", None)
    joint_pos = to_float_list(jpos)

    dtg = to_float_list(safe_get("dtg", None))

    # ---- velocity & spindle ----
    current_vel = safe_get("current_vel", None)
    try:
        current_vel = float(current_vel) if current_vel is not None else None
    except Exception:
        current_vel = None

    # Spindle speed and direction. STAT.spindle is a tuple of dicts; entry [0]
    # carries 'speed' (float) and 'direction' (int) for the primary spindle.
    spindle_speed = None
    spindle_direction = None
    spindles = safe_get("spindle", None)
    if spindles:
        s0 = spindles[0]
        spindle_speed = float(s0['speed'])
        spindle_direction = int(s0['direction'])
    else:
        global _spindle_warned
        if not _spindle_warned:
            _trace.emit("poller.no_spindle_data", level="warn",
                        msg="STAT.spindle empty/missing — commanded spindle speed unavailable")
            _spindle_warned = True




    # ---- tool (stat-only) ----
    # STAT.tool_table is a tuple of tool_result named tuples (id, xoffset..woffset,
    # diameter, frontangle, backangle, orientation). STAT.tool_offset is a 9-tuple
    # of floats holding the active G43 offset (Z at index 2).
    tool_number = safe_get("tool_in_spindle", None)
    tool_diameter = None
    tool_length = None

    tt = safe_get("tool_table", None)
    if tool_number is not None and tt:
        for t in tt:
            if t.id == tool_number:
                tool_diameter = float(t.diameter)
                tool_length = abs(float(t.zoffset))
                break

    if tool_length is None:
        tofs = safe_get("tool_offset", None)
        if tofs:
            tool_length = abs(float(tofs[2]))


    # Tool change request from HAL iocontrol (via webui-reader snapshot).
    # None means reader has no snapshot yet — pass that through honestly.
    tool_change_requested = _reader_get("tool_change")  # Optional[bool]
    tool_change_tool = None
    tool_change_info = None
    if tool_change_requested is True:
        _tc_num = _reader_get("tool_prep_number")
        # T0 (spindle unload) is a valid tool number — don't treat 0 as "no
        # tool". Only an absent reader snapshot (None) means "unknown".
        tool_change_tool = int(_tc_num) if _tc_num is not None else None
        if tool_change_tool is not None:
            try:
                tbl_path = get_tool_tbl_path()
                tbl_mtime = os.path.getmtime(tbl_path) if tbl_path and os.path.exists(tbl_path) else 0
                cache_key = (tool_change_tool, tbl_mtime)
                if cache_key not in _tc_info_cache:
                    tbl_tools = parse_tool_table(tbl_path)
                    library = load_tool_library()
                    _tc_info_cache.clear()
                    _tc_info_cache[cache_key] = _merge_tool_data(tbl_tools, library)
                entry = next((t for t in _tc_info_cache[cache_key] if t["T"] == tool_change_tool), None)
                if entry:
                    tool_change_info = {"D": entry["D"], "Z": entry["Z"], "description": entry.get("description", "")}
            except (OSError, KeyError, ValueError, TypeError) as e:
                _trace.emit("toolchange.info_lookup_failed", level="warn",
                            tool=tool_change_tool, exc=type(e).__name__, msg=str(e))

    spindle_ovr = get_spindle_override()

    # Spindle speed: pass None through if reader has no snapshot yet (or the
    # pin failed to read this tick). UI consumers handle null with `?? null`.
    _sp_in = _reader_get("spindle_speed_in")
    spindle_speed_actual = _sp_in * _fb_scale if _sp_in is not None else None

    program_elapsed_ms = _update_program_timer(
        safe_get("interp_state", None),
        bool(safe_get("paused", False)),
    )

    payload = StatusPayload(
        ts=time.time(),
        estop=estop,
        enabled=enabled,
        emc_enable_in=_reader_get("emc_enable_in"),
        homed=homed,
        homed_joints=homed_joints,
        task_mode=safe_get("task_mode", None),
        interp_state=safe_get("interp_state", None),
        paused=bool(safe_get("paused", False)),
        state=safe_get("state", None),
        motion_mode=safe_get("motion_mode", None),
        inpos=bool(safe_get("inpos", 0)),
        axis_mask=safe_get("axis_mask", None),
        program_units=safe_get("program_units", None),
        current_line=safe_get("current_line", None),
        read_line=safe_get("read_line", None),
        call_level=safe_get("call_level", None),
        g5x_index=g5x_index,
        g5x_offset=g5x,
        g92_offset=g92,
        rotation_xy=rotation_xy,
        wcs_table=[row.copy() for row in _wcs_cache],
        joint_pos=joint_pos,
        tool_offset=tool_offset,
        machine_pos=machine_pos,
        work_pos=work_pos,       # <-- tool-tip work coords
        dtg=dtg,
        feed_override=safe_get("feedrate", None),
        spindle_override=spindle_ovr,
        rapid_override=safe_get("rapidrate", None),
        feed_override_enabled=bool(safe_get("feed_override_enabled", True)),
        spindle_override_enabled=bool(safe_get("spindle_override_enabled", True)),
        block_delete=bool(safe_get("block_delete", 0)),
        optional_stop=bool(safe_get("optional_stop", 0)),
        feed_hold_enabled=bool(safe_get("feed_hold_enabled", 0)),
        adaptive_feed_enabled=bool(safe_get("adaptive_feed_enabled", 0)),
        current_vel=current_vel,
        spindle_speed=spindle_speed,
        spindle_speed_actual=spindle_speed_actual,
        spindle_load=_reader_get("spindle_load"),
        spindle_direction=spindle_direction,
        active_file=safe_get("file", None),
        motion_line=safe_get("motion_line", None),
        program_elapsed_ms=program_elapsed_ms,
        gcodes=to_float_list(safe_get("gcodes", None)),
        mcodes=to_float_list(safe_get("mcodes", None)),
        tool_number=tool_number,
        tool_diameter=tool_diameter,
        tool_length=tool_length,
        tool_change_requested=tool_change_requested,
        tool_change_tool=tool_change_tool,
        tool_change_info=tool_change_info,
        probe_tripped=bool(safe_get("probe_tripped", 0)),
        probe_input=_reader_get("probe_input"),
        probing=bool(safe_get("probing", 0)),
        probed_position=to_float_list(safe_get("probed_position", None)),
        flood=bool(safe_get("flood", 0)),
        mist=bool(safe_get("mist", 0)),
        eoffset_z=_reader_get("z_eoffset"),
        eoffset_enabled=_reader_get("z_eoffset_enable"),
        comp_method=_reader_get("comp_method"),
        comp_grid_version=_reader_get("comp_grid_version"),
    )
    # Backend-authoritative permissions from this very snapshot (issue #19).
    # armed=True; the per-client armed/busy overlay happens client-side.
    # One safety-merge: build the policy state once, broadcast its merged
    # is_estop/is_enabled for the frontend banner, and reuse it for permissions
    # (review #5 — removes the duplicate merge that lived in App.vue).
    _pstate = _policy_state_from_payload(payload, armed=True)
    payload.is_estop = _pstate.is_estop
    payload.is_enabled = _pstate.is_enabled
    payload.permissions = evaluate_permissions(_pstate)
    return payload



def read_errors_nonblocking() -> list:
    global _err_poll_warned
    if ERR is None:
        return []
    out = []
    try:
        while len(out) < 50:  # cap: prevents executor stall on pathological error floods
            e = ERR.poll()
            if not e:
                break
            out.append(e)
    except Exception as e:
        # Error buffer may be briefly invalid after reconnect — log first
        # failure per reconnect window so a persistent issue surfaces; reset
        # in try_connect_lcnc() so each reconnect gets one log line max.
        if not _err_poll_warned:
            _trace.emit("err_chan.poll_failed", level="warn",
                        exc=type(e).__name__, msg=str(e))
            _err_poll_warned = True
    return out


def _ws_hidden_flag(ws: WebSocket) -> bool:
    """Look up the `hidden` state of the client owning `ws` by identity.
    Used by the slow-send probes so a single trace event tells us whether
    the slow consumer was a backgrounded tab (hidden-tab gating should
    have caught it) or a genuinely-visible peer (separate problem).

    Iterates over a snapshot (`list(...)`) to avoid the only realistic
    failure mode (`RuntimeError: dictionary changed size during iteration`)
    when another coroutine adds or removes a client mid-call. Any other
    exception is a real bug and is allowed to propagate — better to
    surface a crash than to silently report `hidden=False` on a corrupted
    state and mislead the trace consumer.
    """
    for c in list(_clients.values()):
        if c.ws is ws:
            return c.hidden
    return False


async def _safe_ws_close(ws: WebSocket, peer: str) -> None:
    """Fire-and-forget close used by the ws_send_measured timeout branch.

    Bounds its own work to 500 ms total. Runs in a background task created
    via asyncio.create_task — the caller (timeout branch in ws_send_measured)
    must NOT await this. ws.close() itself can await an internal drain that
    isn't reliably bounded under storm conditions; if our wait_for here
    fires, the connection is forcibly torn down by the underlying TCP
    timeout / the peer's reconnect logic.
    """
    try:
        await asyncio.wait_for(ws.close(code=1001), timeout=0.5)
    except Exception:
        pass  # safe-silent: WS close is best-effort during shutdown


async def ws_send_json(ws: WebSocket, obj: Dict[str, Any]):
    # Legacy-name shim: all sends go through ws_send_measured so the wire-format
    # flag applies uniformly and non-status messages don't diverge from status
    # frames. Callers that need encode timing / bytes use ws_send_measured directly.
    await ws_send_measured(ws, obj)


def _diff_status_data(last: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Return fields of `current` that differ from `last`.

    Single-level diff — StatusPayload is flat (no nested dataclasses per the
    definition around line 1144). For list-valued fields, Python == compares
    element-wise; mismatched lists are included whole. Reports added/changed
    keys only (no tombstone semantics) — `data` mirrors `linuxcnc.stat`, whose
    keys are stable, so removals don't occur in practice.
    """
    diff: Dict[str, Any] = {}
    for k, v in current.items():
        if k not in last or last[k] != v:
            diff[k] = v
    return diff


async def ws_send_measured(ws: WebSocket, obj: Dict[str, Any]) -> Tuple[float, int]:
    """Encode + send a WS payload. Returns (encode_ms, bytes_sent).

    Used by the status hot path to attribute encode cost and payload size for
    the Debug-tab timing surface. Other callers keep using ws_send_json when
    they don't care about the measurement.

    The wire format is chosen by _WIRE_FORMAT (see Experiment 1). encode_ms
    excludes the actual ws.send_* call; bytes_sent is the size of the encoded
    payload. Returns (encode_ms, 0) if the client disconnected mid-send.
    """
    t0 = time.monotonic()
    _set_phase("ws_send_measured.encode")
    # Inline encode (deliberate). A dedicated encode thread-pool was tried and
    # removed: the trace bus showed encode_qsize=0 across every storm-time
    # loop.tick while encode_ms still climbed to 50–200 ms — pool dispatch isn't
    # the cost, GIL contention is, and offloading only adds dispatch overhead
    # while the worker fights for the GIL alongside the main loop. msgspec's
    # C-level encoder is fast (sub-ms for the typical envelope) and inline
    # encoding eliminates a thread round-trip per send. The `ws.encode_slow`
    # event still fires if this assumption breaks under future load.
    if _WIRE_FORMAT == "msgpack":
        data = _msgpack_encoder.encode(obj)
    else:
        data = _json_encoder_encode(obj)
    encode_ms = round((time.monotonic() - t0) * 1000, 3)
    try:
        _send_peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
    except Exception:
        _send_peer = "?"
    # Encode-only slow path: catches the case where the encode pool was
    # contended / GIL-starved but the actual ws.send returns fast. Without
    # this, ws.fanout_send_slow only fires when SEND is also slow and we'd
    # miss encode-pool starvation entirely.
    if encode_ms > 50:
        _trace.emit(
            "ws.encode_slow",
            level="warn" if encode_ms > 200 else "info",
            peer=_send_peer, encode_ms=encode_ms, bytes=len(data),
            hidden=_ws_hidden_flag(ws),
        )
    _set_phase(f"ws_send.{_WIRE_FORMAT} peer={_send_peer} bytes={len(data)}")
    # Bound any single send to _WS_SEND_TIMEOUT_S. A backgrounded browser
    # tab stops reading → kernel TCP write buffer fills → `await
    # ws.send_bytes` holds the loop ~1+ s, breaking the 500 ms HAL
    # heartbeat budget. wait_for caps it; on timeout we close the slow
    # client (they're already not getting updates anyway). Other clients
    # are unaffected — each runs in its own status_loop task. The
    # heartbeat task remains tied to the same asyncio loop, so any other
    # cause of loop hang still trips the watchdog correctly.
    send_t0 = time.monotonic()
    try:
        if _WIRE_FORMAT == "msgpack":
            await asyncio.wait_for(ws.send_bytes(data), timeout=_WS_SEND_TIMEOUT_S)
        else:
            await asyncio.wait_for(
                ws.send_text(data if isinstance(data, str) else data.decode("utf-8")),
                timeout=_WS_SEND_TIMEOUT_S,
            )
    except asyncio.TimeoutError:
        _set_phase(f"ws_send_measured.timeout_caught peer={_send_peer}")
        # Slow consumer — log peer and close the WS. The per-client
        # status_loop catches the resulting WebSocketDisconnect and
        # exits cleanly (existing handler).
        try:
            peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
        except Exception:
            peer = "?"
        try:
            obj_type = str(obj.get("type", "?"))
        except Exception:
            obj_type = "?"
        send_ms = (time.monotonic() - send_t0) * 1000
        _trace.emit(
            "ws.slow_client_drop", level="warn",
            peer=peer, send_ms=round(send_ms, 1),
            timeout_ms=int(_WS_SEND_TIMEOUT_S * 1000),
            bytes=len(data), obj_type=obj_type,
            hidden=_ws_hidden_flag(ws),
        )
        # Schedule the close as fire-and-forget so the timeout branch
        # itself never blocks the asyncio loop. With N concurrent slow
        # peers the previous inline `await wait_for(ws.close, 0.1)` could
        # add N × 100 ms of loop-hold; create_task adds ~0 ms. The close
        # task is bounded internally (see _safe_ws_close). Combined with
        # the per-client send_pending flag, no further fan-out is queued
        # to this client until the close completes or the connection
        # errors out.
        asyncio.create_task(_safe_ws_close(ws, peer))
        _set_phase(f"ws_send_measured.timeout_returning peer={_send_peer}")
        return (encode_ms, 0)
    except RuntimeError:
        _set_phase(f"ws_send_measured.RuntimeError peer={_send_peer}")
        return (encode_ms, 0)
    _set_phase(f"ws_send_measured.send_done peer={_send_peer}")
    send_dt_ms = (time.monotonic() - send_t0) * 1000
    # Promote any slow but completed send (>50 ms) into the trace bus so
    # we see backpressure building before it timeouts.
    if send_dt_ms > 50:
        try:
            peer = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
        except Exception:
            peer = "?"
        try:
            obj_type = str(obj.get("type", "?"))
        except Exception:
            obj_type = "?"
        _trace.emit(
            "ws.fanout_send_slow",
            level="warn" if send_dt_ms > 200 else "info",
            peer=peer, send_ms=round(send_dt_ms, 1),
            encode_ms=encode_ms, bytes=len(data), obj_type=obj_type,
            hidden=_ws_hidden_flag(ws),
        )
    # msgspec returns bytes; stdlib json returns str — both have len() == bytes/chars
    return (encode_ms, len(data))


async def _cmd_blocking(cmd_fn, *args, wait=_CMD_WAIT_TIMEOUT) -> int:
    """Run a blocking CMD.* call + optional wait_complete() on a worker thread.

    Every `CMD.* + wait_complete()` pair must go through here. The LinuxCNC C
    extension holds the GIL during its blocking sections; calling it directly
    from the event-loop thread starves `_heartbeat_loop` and trips the HAL
    watchdog. `asyncio.to_thread` isolates the blocking section so heartbeats
    and status polls keep firing. Returns wait_complete()'s int result (0 ok,
    1 failed, -1 timeout) or 0 when wait=None.

    Caller must hold `_cmd_lock` — NML command channel is not thread-safe.
    """
    def _run():
        cmd_fn(*args)
        if wait is not None:
            return CMD.wait_complete(wait)
        return 0
    return await asyncio.to_thread(_run)


async def set_mode(mode: int):
    """Switch LinuxCNC task mode. Caller must hold `_cmd_lock`."""
    STAT.poll()
    if safe_get("task_mode", None) == mode:
        return
    await _cmd_blocking(CMD.mode, mode)

def reject_if_auto_running() -> Optional[Dict[str, Any]]:
    STAT.poll()
    mode = safe_get("task_mode", None)
    interp = safe_get("interp_state", None)

    # If we're in AUTO and interpreter isn't idle, don't allow mode switches / jog / mdi
    if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
        return {
            "ok": False,
            "error": "Busy in AUTO (interpreter not IDLE) — command rejected",
            "task_mode": mode,
            "interp_state": interp,
        }
    # MDI commands are fire-and-forget — guard against overlapping commands
    if mode == linuxcnc.MODE_MDI and interp != linuxcnc.INTERP_IDLE:
        return {
            "ok": False,
            "error": "MDI command in progress — command rejected",
            "task_mode": mode,
            "interp_state": interp,
        }
    return None



def _jog_joint_flag() -> int:
    """Return the joint_flag for CMD.jog() based on current trajectory mode.
    0 = Cartesian axis (TRAJ_MODE_TELEOP), 1 = joint (TRAJ_MODE_FREE, safe default)."""
    STAT.poll()
    if safe_get("motion_mode", None) == linuxcnc.TRAJ_MODE_TELEOP:
        return 0
    return 1


async def _jog_stop_for_client() -> None:
    """Jog-stop every joint as part of a client disarm. Caller must hold _cmd_lock.

    Called when a client transitions to disarmed via heartbeat stall or WS
    close. The point is to stop any in-flight jog this client may have
    started — without a follow-up jog_stop the machine continues moving
    until it hits a limit.

    This function does NOT abort the program. A running G-code program is
    owned by LinuxCNC's interpreter, not by any single client. Motion abort
    from a client-liveness failure is the HAL chain's job (oneshot timeout
    on all-clients-out, or gateway hang). Per the architecture review in
    docs/plans, keep the "armed = authorization" and "HAL = safety" layers
    separate — this function only handles the in-flight-jog case.

    Mode safety: if the machine is in MODE_AUTO with the interpreter
    running, a jog cannot be in flight (jogging requires MANUAL/TELEOP).
    Forcing a mode switch to MANUAL here would interrupt the program —
    exactly what this cleanup is trying to avoid. So skip in that case.

    Multi-armed-client note: today we stop every joint, which can interfere
    with a parallel jog from another armed client. Per-client active-jog
    tracking is a documented follow-up; for now, the bluntness is acceptable
    because simultaneous multi-client jogging is rare and stopping is the
    safe default.
    """
    if not bool(safe_get("enabled", False)):
        return
    mode = safe_get("task_mode", None)
    interp = safe_get("interp_state", None)
    if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
        return  # program running — no jog to stop, must not switch mode
    homed = normalize_homed(safe_get("homed", None))
    if not homed:
        return  # nothing to jog-stop if not homed
    await set_mode(linuxcnc.MODE_MANUAL)
    jf = _jog_joint_flag()
    _nj = getattr(STAT, "joints", 3) if STAT else 3
    for ax in range(_nj):
        await _cmd_blocking(CMD.jog, linuxcnc.JOG_STOP, jf, ax, wait=None)


def require_armed(armed: bool):
    if not armed:
        raise PermissionError("Not armed")


def require_no_eoffset():
    """Mirror the frontend probe/zero gate (issue #19): refuse a touch-off / work
    offset edit while surface-compensation Z eoffset is active, or LinuxCNC would
    bake the eoffset into the new offset (the current position the offset is
    derived from is contaminated). LinuxCNC itself doesn't enforce this web-UI
    gate, so a direct client could otherwise bypass it. Mirrors the frontend's
    `!eoffsetEnabled` condition: only blocks when the pin is definitely enabled
    (None/stale → allowed, matching the UI)."""
    if _reader_get("z_eoffset_enable"):
        raise PermissionError("Surface compensation active — clear the eoffset before editing work offsets")


async def handle_command(msg: Dict[str, Any], armed: bool):
    # Acquire the CMD lock before dispatching. Every path that touches CMD.*
    # must hold this lock; see _cmd_lock docstring.
    async with _get_cmd_lock():
        return await _handle_command_impl(msg, armed)


async def _handle_command_impl(msg: Dict[str, Any], armed: bool):
    global _estop_hold
    cmd = msg.get("cmd")
    if not cmd:
        return {"ok": False, "error": "Missing cmd"}

    # ---- Read-only commands (no LinuxCNC connection or arming needed) ----
    try:
        if cmd == "get_tool_table":
            tbl_path = get_tool_tbl_path()
            if not tbl_path:
                return {"ok": False, "error": "Tool table path not available"}
            tbl_tools = await asyncio.to_thread(parse_tool_table, tbl_path)  # file reads off the loop (B3)
            library = await asyncio.to_thread(load_tool_library)
            merged = _merge_tool_data(tbl_tools, library)
            current_tool = None
            try:
                if STAT:
                    STAT.poll()
                    raw = safe_get("tool_in_spindle", None)
                    current_tool = int(raw) if raw is not None else None
            except (AttributeError, ValueError, TypeError, linuxcnc.error) as e:
                _trace.emit("tools.current_tool_poll_failed", level="warn",
                            exc=type(e).__name__, msg=str(e))
            return {"ok": True, "tools": merged, "current_tool": current_tool}

        if cmd == "get_probe_results":
            pts = await asyncio.to_thread(_read_probe_results_file)
            return {"ok": True, "points": pts}

        if cmd == "get_comp_grid":
            ini_path = getattr(STAT, "ini_filename", None)
            config_dir = os.path.dirname(ini_path) if ini_path else ""
            path = os.path.join(config_dir, "probe-results-grid.json")
            if not os.path.isfile(path):
                return {"ok": False, "error": "No grid file"}

            def _load_grid():
                with open(path, "r") as f:
                    try:
                        return json.load(f), None
                    except (json.JSONDecodeError, ValueError):
                        return None, "Invalid grid file"

            grid, err = await asyncio.to_thread(_load_grid)
            if err:
                return {"ok": False, "error": err}
            return {"ok": True, "comp_grid": grid}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not lcnc_connected:
        return {"ok": False, "error": "LinuxCNC not connected"}

    # Backend command authorization (issue #19). Enforces the SAME unified policy
    # the frontend consumes (command_policy). Because the frontend's Gate/fire()
    # layer already gates on these exact classes, this never rejects a
    # conforming-UI command — it backstops direct / buggy / malicious clients.
    # 'always' commands (jog_stop, estop, abort, …) pass. The handler-level
    # require_armed()/reject_if_auto_running() guards remain as defense in depth.
    # Denials are bounded + traced — never silently dropped
    # (feedback_no_silent_fallbacks).
    if _shared_status is not None:
        _deny = check_command(cmd, _policy_state_from_payload(_shared_status, armed))
        if _deny is not None:
            _trace.emit("ws.command_denied", level="warn", cmd=cmd, reason=_deny)
            return {"ok": False, "error": _deny}
    else:
        # No status snapshot yet (pre-first-poll window). Can't evaluate state;
        # the handler-level guards still apply. Trace so the gap is auditable.
        _trace.emit("policy.check_skipped_no_state", level="warn", cmd=cmd)

    try:
        if cmd == "arm":
            return {"ok": True}

        if cmd == "estop":
            await _cmd_blocking(CMD.state, linuxcnc.STATE_ESTOP, wait=None)
            _estop_hold = True
            _hal_send({"connected": False})  # hold via _estop_hold
            return {"ok": True}

        if cmd == "estop_reset":
            # require_armed is safe here despite a trip auto-clearing nothing:
            # the frontend gates its Reset button on canResetEstop (armed &&
            # isEstop), and arm is rejected while _unacked_trip is set, so the
            # operator-reachable recovery order is Acknowledge -> Arm -> Reset.
            # By the time this command can be sent the client is armed.
            require_armed(armed)
            # Do NOT pre-check emc_enable_in here. Standard LinuxCNC safety
            # chains feed iocontrol.0.user-enable-out back into the AND that
            # drives emc-enable-in (the operator-acknowledgement latch).
            # user-enable-out is LOW by design while task is in STATE_ESTOP,
            # so emc-enable-in is *always* FALSE at the moment of reset —
            # checking it deadlocks the only command that can escape ESTOP.
            # machine_on is the correct gate: by then user-enable-out is
            # TRUE and emc-enable-in reflects only hardware conditions.
            # UI's safetyChainOpen still keeps the estop indicator visible
            # if the chain is hardware-open post-reset, so the operator is
            # not misled. Issues #14 (problem) and #15 (this regression).
            # Pulse the HAL latch reset first so the safety chain can come back
            # up when LinuxCNC transitions out of ESTOP. The gateway sends the
            # IPC; hal_watchdog drives webui-safety.trip-reset-out, and the
            # servo-thread estop_latch clears within a cycle. 20ms sleep ≈ 2×
            # hal_watchdog select slice, enough for the reset edge to propagate
            # and ok-out to land before STATE_ESTOP_RESET is evaluated.
            _hal_send({"trip_reset": True})
            await asyncio.sleep(0.02)
            await _cmd_blocking(CMD.state, linuxcnc.STATE_ESTOP_RESET, wait=None)
            _estop_hold = False
            _hal_send({"connected": True})
            return {"ok": True}

        if cmd == "machine_on":
            require_armed(armed)
            # Optional but nice: avoid guaranteed-fail calls
            STAT.poll()
            if safe_get("estop", True):
                return {"ok": False, "error": "Cannot Machine On while in E-stop"}
            # Same edge-detection defense as estop_reset (issue #14).
            if _reader_get("emc_enable_in") is False:
                return {"ok": False, "error": "Safety chain is open — cannot turn machine on"}
            await _cmd_blocking(CMD.state, linuxcnc.STATE_ON, wait=None)
            return {"ok": True}

        if cmd == "machine_off":
            require_armed(armed)
            await _cmd_blocking(CMD.state, linuxcnc.STATE_OFF, wait=None)
            return {"ok": True}

        if cmd == "set_mode":
            require_armed(armed)
            blocked = reject_if_auto_running()
            if blocked:
                return blocked
            mode = finite_int(msg.get("mode", 0))
            if mode not in (linuxcnc.MODE_MANUAL, linuxcnc.MODE_AUTO, linuxcnc.MODE_MDI):
                return {"ok": False, "error": f"Invalid mode: {mode}"}
            await set_mode(mode)
            return {"ok": True}

        if cmd == "shutdown":
            # No require_armed — confirmation dialog is the safety gate.
            # Signal the LAUNCHER (parent), not ourselves, so the UI
            # shutdown path goes through the same _term trap as Ctrl-C.
            # Self-signaling SIGTERM makes uvicorn run lifespan teardown
            # but the gateway then exits with status 143 (128 + SIGTERM);
            # the launcher's `wait` captures 143 → LinuxCNC sees DISPLAY
            # exit non-zero → "terminated with an error" even though
            # everything ran cleanly. Signaling the launcher routes
            # through its `_term` trap, which forwards SIGTERM to the
            # gateway (lifespan still runs end-to-end) and then `exit 0`s
            # explicitly — identical end state as Ctrl-C.
            print("Shutdown requested via web UI", flush=True)
            try:
                os.kill(os.getppid(), signal.SIGTERM)
            except (ProcessLookupError, PermissionError) as e:
                # Launcher already gone — fall back to self-signal so we
                # at least exit cleanly via uvicorn's lifespan path.
                print(f"Shutdown: launcher signal failed ({e}); self-signaling", flush=True)
                signal.raise_signal(signal.SIGTERM)
            return {"ok": True}

        if cmd == "abort":
            require_armed(armed)
            await _cmd_blocking(CMD.abort, wait=None)
            return {"ok": True}

        if cmd == "mdi":
            require_armed(armed)

            blocked = reject_if_auto_running()
            if blocked:
                return blocked

            text = msg.get("text", "")

            if not isinstance(text, str) or not text.strip():
                return {"ok": False, "error": "Missing text"}
            await set_mode(linuxcnc.MODE_MDI)
            await _cmd_blocking(CMD.mdi, text, wait=None)
            return {"ok": True}

        if cmd == "save_tool":
            require_armed(armed)
            tool_num = finite_int(msg["tool_number"], lo=0)
            tbl_path = get_tool_tbl_path()
            if not tbl_path:
                return {"ok": False, "error": "Tool table path not available"}

            # Update tool.tbl — all file I/O off the loop; _cmd_lock stays held
            # so the read-modify-write is still serialized (B3). ToolLibraryStore
            # is lock-guarded, so an offloaded status-loop read can't race it.
            tbl_tools = await asyncio.to_thread(parse_tool_table, tbl_path)
            found = False
            for t in tbl_tools:
                if t["T"] == tool_num:
                    if "pocket" in msg:
                        t["P"] = finite_int(msg["pocket"], lo=0)
                    if "z_offset" in msg:
                        t["Z"] = finite_float(msg["z_offset"])
                    if "diameter" in msg:
                        t["D"] = finite_float(msg["diameter"], lo=0)
                    if "remark" in msg:
                        t["remark"] = str(msg["remark"])
                    found = True
                    break
            if not found:
                return {"ok": False, "error": f"Tool T{tool_num} not found"}

            await asyncio.to_thread(write_tool_table, tbl_path, tbl_tools)
            await _reload_tool_table_and_bump()

            # Update metadata
            library = await asyncio.to_thread(load_tool_library)
            key = str(tool_num)
            if key not in library:
                library[key] = {}
            for field in _TOOL_META_FIELDS:
                if field in msg:
                    library[key][field] = msg[field]
            await asyncio.to_thread(save_tool_library, library)
            global _tool_meta_dirty
            _tool_meta_dirty = True
            return {"ok": True}

        if cmd == "add_tool":
            require_armed(armed)
            tool_num = finite_int(msg["tool_number"], lo=0)
            tbl_path = get_tool_tbl_path()
            if not tbl_path:
                return {"ok": False, "error": "Tool table path not available"}

            tbl_tools = await asyncio.to_thread(parse_tool_table, tbl_path)
            for t in tbl_tools:
                if t["T"] == tool_num:
                    return {"ok": False, "error": f"Tool T{tool_num} already exists"}

            tbl_tools.append({
                "T": tool_num,
                "P": finite_int(msg.get("pocket", tool_num), lo=0),
                "Z": finite_float(msg.get("z_offset", 0.0)),
                "D": finite_float(msg.get("diameter", 0.0), lo=0),
                "remark": str(msg.get("remark", "")),
            })
            await asyncio.to_thread(write_tool_table, tbl_path, tbl_tools)
            await _reload_tool_table_and_bump()

            # Save metadata if provided
            library = await asyncio.to_thread(load_tool_library)
            key = str(tool_num)
            library[key] = {}
            for field in _TOOL_META_FIELDS:
                if field in msg:
                    library[key][field] = msg[field]
            await asyncio.to_thread(save_tool_library, library)
            return {"ok": True}

        if cmd == "delete_tool":
            require_armed(armed)
            tool_num = finite_int(msg["tool_number"], lo=0)

            # Don't delete the currently loaded tool
            STAT.poll()
            current = safe_get("tool_in_spindle", None)
            try:
                current = int(current) if current is not None else None
            except (ValueError, TypeError):
                current = None
            if current == tool_num:
                return {"ok": False, "error": f"Cannot delete T{tool_num} — currently in spindle"}

            tbl_path = get_tool_tbl_path()
            if not tbl_path:
                return {"ok": False, "error": "Tool table path not available"}

            tbl_tools = await asyncio.to_thread(parse_tool_table, tbl_path)
            new_tools = [t for t in tbl_tools if t["T"] != tool_num]
            if len(new_tools) == len(tbl_tools):
                return {"ok": False, "error": f"Tool T{tool_num} not found"}

            await asyncio.to_thread(write_tool_table, tbl_path, new_tools)
            await _reload_tool_table_and_bump()

            library = await asyncio.to_thread(load_tool_library)
            library.pop(str(tool_num), None)
            await asyncio.to_thread(save_tool_library, library)

            return {"ok": True}

        if cmd == "renumber_tool":
            # One transactional renumber (issue #30): validate, then rewrite the
            # tool table AND move the metadata together under the already-held
            # _cmd_lock. Replaces the old add_tool+delete_tool client sequence,
            # which could leave a duplicate / lost tool if the second send failed.
            require_armed(armed)
            old_num = finite_int(msg["old_tool_number"], lo=0)
            new_num = finite_int(msg["tool_number"], lo=0)
            tbl_path = get_tool_tbl_path()
            if not tbl_path:
                return {"ok": False, "error": "Tool table path not available"}

            tbl_tools = await asyncio.to_thread(parse_tool_table, tbl_path)
            src = next((t for t in tbl_tools if t["T"] == old_num), None)
            if src is None:
                return {"ok": False, "error": f"Tool T{old_num} not found"}
            if new_num != old_num and any(t["T"] == new_num for t in tbl_tools):
                return {"ok": False, "error": f"Tool T{new_num} already exists"}

            # Don't renumber the tool in the spindle — its active offset would
            # shift under it.
            STAT.poll()
            current = safe_get("tool_in_spindle", None)
            try:
                current = int(current) if current is not None else None
            except (ValueError, TypeError):
                current = None
            if current == old_num:
                return {"ok": False, "error": f"Cannot renumber T{old_num} — currently in spindle"}

            src["T"] = new_num
            src["P"] = finite_int(msg.get("pocket", new_num), lo=0)
            src["Z"] = finite_float(msg.get("z_offset", src.get("Z", 0.0)))
            src["D"] = finite_float(msg.get("diameter", src.get("D", 0.0)), lo=0)
            src["remark"] = str(msg.get("remark", src.get("remark", "")))
            await asyncio.to_thread(write_tool_table, tbl_path, tbl_tools)
            await _reload_tool_table_and_bump()

            library = await asyncio.to_thread(load_tool_library)
            meta = library.pop(str(old_num), {}) or {}
            for field in _TOOL_META_FIELDS:
                if field in msg:
                    meta[field] = msg[field]
            library[str(new_num)] = meta
            await asyncio.to_thread(save_tool_library, library)
            return {"ok": True, "tool_number": new_num}

        if cmd == "tool_change":
            require_armed(armed)
            blocked = reject_if_auto_running()
            if blocked:
                return blocked
            tool_num = finite_int(msg["tool_number"], lo=0)
            await set_mode(linuxcnc.MODE_MDI)
            await _cmd_blocking(CMD.mdi, f"T{tool_num} M6 G43", wait=None)
            return {"ok": True}

        if cmd == "auto_step":
            require_armed(armed)
            STAT.poll()
            paused = bool(safe_get("paused", False))
            interp = safe_get("interp_state", None)
            mode = safe_get("task_mode", None)

            if paused:
                # Already paused → advance one block (no mode change)
                await _cmd_blocking(CMD.auto, linuxcnc.AUTO_STEP, wait=None)
            elif mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                # Running (not paused). The 'step' gate forbids stepping while
                # running, but it checks a status SNAPSHOT while this handler
                # re-polls fresh — so this guards the race where the program
                # started in between. Reject (consistent with the gate) rather
                # than fall through to the 'idle' branch and re-start a running
                # program (review #3).
                return {"ok": False, "error": "Cannot step while running — pause first"}
            else:
                # Idle → start program and step
                await set_mode(linuxcnc.MODE_AUTO)
                await _cmd_blocking(CMD.auto, linuxcnc.AUTO_STEP, wait=None)
            return {"ok": True}

        if cmd == "auto_run":
            require_armed(armed)
            spindle_dir = msg.get("spindle_dir")
            spindle_speed = finite_int(msg.get("spindle_speed", 0), lo=0)
            if spindle_dir and spindle_speed > 0:
                await set_mode(linuxcnc.MODE_MANUAL)
                if spindle_dir == "forward":
                    await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_FORWARD, spindle_speed, wait=None)
                elif spindle_dir == "reverse":
                    await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_REVERSE, spindle_speed, wait=None)
            await set_mode(linuxcnc.MODE_AUTO)
            start_line = finite_int(msg.get("line", 0), lo=0)
            await _cmd_blocking(CMD.auto, linuxcnc.AUTO_RUN, start_line, wait=None)
            return {"ok": True}

        # jog left intact (even if you're not using it right now)
        if cmd == "jog_cont":
            require_armed(armed)

            blocked = reject_if_auto_running()
            if blocked:
                return blocked

            axis = finite_int(msg.get("axis"), lo=0)
            vel = finite_float(msg.get("vel", 0.0))
            await set_mode(linuxcnc.MODE_MANUAL)
            jf = _jog_joint_flag()
            await _cmd_blocking(CMD.jog, linuxcnc.JOG_CONTINUOUS, jf, axis, vel, wait=None)
            return {"ok": True}

        if cmd == "jog_stop":
            # Stopping motion is always allowed — gateway must NOT silently
            # drop a stop request, even from a disarmed client. The previous
            # `if not armed: return ok` short-circuit caused a real safety
            # gap: operator holds jog, disarms, releases → client's jog_stop
            # was accepted as no-op and the machine kept moving. Audit Phase
            # 2 / Issue E1. In AUTO+running a jog cannot be in flight
            # (jogging requires MANUAL/TELEOP) and a forced mode switch
            # would interrupt the program — skip that case explicitly.
            mode = safe_get("task_mode", None)
            interp = safe_get("interp_state", None)
            if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                return {"ok": True}
            axis = finite_int(msg.get("axis"), lo=0)
            await set_mode(linuxcnc.MODE_MANUAL)
            jf = _jog_joint_flag()
            await _cmd_blocking(CMD.jog, linuxcnc.JOG_STOP, jf, axis, wait=None)
            return {"ok": True}

        if cmd == "jog_cont_multi":
            require_armed(armed)

            blocked = reject_if_auto_running()
            if blocked:
                return blocked

            axes = msg.get("axes", [])
            await set_mode(linuxcnc.MODE_MANUAL)
            jf = _jog_joint_flag()
            for entry in axes:
                await _cmd_blocking(CMD.jog, linuxcnc.JOG_CONTINUOUS, jf, finite_int(entry["axis"], lo=0), finite_float(entry["vel"]), wait=None)
            return {"ok": True}

        if cmd == "jog_stop_multi":
            # Stopping motion is always allowed — see jog_stop above for the
            # same audit rationale (Phase 2 / Issue E1). In AUTO+running we
            # have no jog in flight and must not switch modes.
            mode = safe_get("task_mode", None)
            interp = safe_get("interp_state", None)
            if mode == linuxcnc.MODE_AUTO and interp != linuxcnc.INTERP_IDLE:
                return {"ok": True}
            axes = msg.get("axes", [])
            await set_mode(linuxcnc.MODE_MANUAL)
            jf = _jog_joint_flag()
            for a in axes:
                await _cmd_blocking(CMD.jog, linuxcnc.JOG_STOP, jf, finite_int(a, lo=0), wait=None)
            return {"ok": True}

        if cmd == "jog_incr":
            require_armed(armed)

            blocked = reject_if_auto_running()
            if blocked:
                return blocked

            axis = finite_int(msg.get("axis"), lo=0)
            vel = abs(finite_float(msg.get("vel", 0.0)))  # speed only; distance carries direction
            dist = finite_float(msg.get("distance", 0.0))
            await set_mode(linuxcnc.MODE_MANUAL)
            jf = _jog_joint_flag()
            await _cmd_blocking(CMD.jog, linuxcnc.JOG_INCREMENT, jf, axis, vel, dist, wait=None)
            return {"ok": True}

        if cmd == "jog_incr_multi":
            require_armed(armed)

            blocked = reject_if_auto_running()
            if blocked:
                return blocked

            axes = msg.get("axes", [])
            await set_mode(linuxcnc.MODE_MANUAL)
            jf = _jog_joint_flag()
            for entry in axes:
                await _cmd_blocking(CMD.jog, linuxcnc.JOG_INCREMENT, jf, finite_int(entry["axis"], lo=0), abs(finite_float(entry["vel"])), finite_float(entry["distance"]), wait=None)
            return {"ok": True}

        if cmd == "home_all":
            require_armed(armed)
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.home, -1, wait=None)  # -1 homes all axes
            return {"ok": True}

        if cmd == "unhome_all":
            require_armed(armed)
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.teleop_enable, 0)  # unhome requires joint mode
            await _cmd_blocking(CMD.unhome, -1, wait=None)  # -1 unhomes all axes
            return {"ok": True}

        if cmd == "home":
            require_armed(armed)
            joint = finite_int(msg.get("joint", -1))
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.home, joint, wait=None)
            return {"ok": True}

        if cmd == "unhome":
            require_armed(armed)
            joint = finite_int(msg.get("joint", -1))
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.teleop_enable, 0)  # unhome requires joint mode
            await _cmd_blocking(CMD.unhome, joint, wait=None)
            return {"ok": True}

        if cmd == "cycle_start":
            require_armed(armed)
            await set_mode(linuxcnc.MODE_AUTO)
            await _cmd_blocking(CMD.auto, linuxcnc.AUTO_RUN, 0, wait=None)  # Start from beginning
            return {"ok": True}

        if cmd == "cycle_pause":
            require_armed(armed)
            await _cmd_blocking(CMD.auto, linuxcnc.AUTO_PAUSE, wait=None)
            return {"ok": True}

        if cmd == "cycle_resume":
            require_armed(armed)
            # Don't call set_mode - already in AUTO mode when paused
            await _cmd_blocking(CMD.auto, linuxcnc.AUTO_RESUME, wait=None)
            return {"ok": True}

        if cmd == "set_feed_override":
            require_armed(armed)
            scale = finite_float(msg.get("scale", 1.0), 1.0)
            # Clamp to reasonable range (0-200%)
            scale = max(0.0, min(2.0, scale))
            await _cmd_blocking(CMD.feedrate, scale, wait=None)
            return {"ok": True, "scale": scale}

        if cmd == "set_spindle_override":
            require_armed(armed)
            scale = finite_float(msg.get("scale", 1.0), 1.0)
            # Clamp to reasonable range (50-200%)
            scale = max(0.5, min(2.0, scale))
            await _cmd_blocking(CMD.spindleoverride, scale, wait=None)
            return {"ok": True, "scale": scale}

        if cmd == "spindle_forward":
            require_armed(armed)
            speed = finite_float(msg.get("speed", 0))
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_FORWARD, speed, wait=None)
            return {"ok": True}

        if cmd == "spindle_reverse":
            require_armed(armed)
            speed = finite_float(msg.get("speed", 0))
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_REVERSE, speed, wait=None)
            return {"ok": True}

        if cmd == "spindle_stop":
            require_armed(armed)
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_OFF, wait=None)
            return {"ok": True}

        if cmd == "spindle_increase":
            require_armed(armed)
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_INCREASE, wait=None)
            return {"ok": True}

        if cmd == "spindle_decrease":
            require_armed(armed)
            await set_mode(linuxcnc.MODE_MANUAL)
            await _cmd_blocking(CMD.spindle, linuxcnc.SPINDLE_DECREASE, wait=None)
            return {"ok": True}

        if cmd == "flood_on":
            require_armed(armed)
            await _cmd_blocking(CMD.flood, linuxcnc.FLOOD_ON, wait=None)
            return {"ok": True}

        if cmd == "flood_off":
            require_armed(armed)
            await _cmd_blocking(CMD.flood, linuxcnc.FLOOD_OFF, wait=None)
            return {"ok": True}

        if cmd == "mist_on":
            require_armed(armed)
            await _cmd_blocking(CMD.mist, linuxcnc.MIST_ON, wait=None)
            return {"ok": True}

        if cmd == "mist_off":
            require_armed(armed)
            await _cmd_blocking(CMD.mist, linuxcnc.MIST_OFF, wait=None)
            return {"ok": True}

        if cmd == "set_rapid_override":
            require_armed(armed)
            scale = finite_float(msg.get("scale", 1.0), 1.0)
            # Clamp to 0-100%
            scale = max(0.0, min(1.0, scale))
            await _cmd_blocking(CMD.rapidrate, scale, wait=None)
            return {"ok": True, "scale": scale}

        if cmd == "set_optional_stop":
            require_armed(armed)
            value = bool(msg.get("value", False))
            await _cmd_blocking(CMD.set_optional_stop, value, wait=None)
            return {"ok": True}

        if cmd == "set_block_delete":
            require_armed(armed)
            value = bool(msg.get("value", False))
            await _cmd_blocking(CMD.set_block_delete, value, wait=None)
            return {"ok": True}

        if cmd == "set_max_velocity":
            require_armed(armed)
            velocity = finite_float(msg.get("velocity", 0.0))
            # Clamp to positive values
            velocity = max(0.0, velocity)
            await _cmd_blocking(CMD.maxvel, velocity, wait=None)
            return {"ok": True, "velocity": velocity}

        if cmd == "load_file":
            require_armed(armed)
            path = msg.get("path", "")
            if not path or not isinstance(path, str):
                return {"ok": False, "error": "Missing path"}

            abs_path = os.path.abspath(path)
            if not os.path.isfile(abs_path):
                return {"ok": False, "error": "File not found"}

            nc_dir = get_nc_files_dir()
            if not validate_path_within(abs_path, nc_dir):
                return {"ok": False, "error": "File not in NC files directory"}

            if not validate_extension(abs_path):
                return {"ok": False, "error": "Invalid file extension"}

            blocked = reject_if_auto_running()
            if blocked:
                return blocked

            # Phase markers (B8) subdivide load_file so the lag monitor pins which
            # step holds the loop — the run named "handle_command cmd=load_file"
            # at ~78 ms but couldn't say which part. set_mode/program_open already
            # offload their NML work; if a marker dominates a future lag.window,
            # that's the synchronous piece to move off-loop next.
            _set_phase("load_file.set_mode")
            await set_mode(linuxcnc.MODE_AUTO)
            # Fire-and-forget (no wait_complete). milltask opens the file in its
            # OWN process; our `wait=5` just polled it via CMD.wait_complete — and
            # the LinuxCNC C binding HOLDS THE GIL across that poll, so the
            # `to_thread` offload could not free the loop (a 44 MB open stalled it
            # 62 ms — `load_file.program_open` dominated the lag.window; a larger
            # file or slower disk scales straight toward the 500 ms watchdog trip).
            # The program_open SEND is a microsecond NML write. Completion is
            # observed via the status stream (interp_state + file) and the preview
            # re-parses from disk independently — same contract as the tool_change
            # fire-and-forget path. Open failures surface on the error channel.
            _set_phase("load_file.program_open")
            await _cmd_blocking(CMD.program_open, abs_path, wait=None)
            return {"ok": True, "path": abs_path}

        if cmd == "unload_file":
            require_armed(armed)
            blocked = reject_if_auto_running()
            if blocked:
                return blocked
            await _cmd_blocking(CMD.abort)
            await _cmd_blocking(CMD.reset_interpreter)
            return {"ok": True}

        if cmd == "list_probe_macros":
            return {"ok": True, "macros": get_probe_macros()}

        if cmd == "set_probe_vars":
            require_armed(armed)
            vars_to_set = msg.get("vars", {})
            if not vars_to_set or not isinstance(vars_to_set, dict):
                return {"ok": False, "error": "Missing vars dict"}
            # 1) Always write to var file for persistence across restarts
            file_ok = False
            ini_path = getattr(STAT, "ini_filename", None)
            if ini_path:
                ini = linuxcnc.ini(ini_path)
                var_file = ini.find("RS274NGC", "PARAMETER_FILE")
                if var_file:
                    if not os.path.isabs(var_file):
                        var_file = os.path.join(os.path.dirname(ini_path), var_file)
                    str_vars = {str(k): float(v) for k, v in vars_to_set.items()}
                    _trace.emit("probe.set_vars", vars=str_vars)
                    await asyncio.to_thread(_write_var_file_updates, var_file, str_vars)
                    file_ok = True
            # 2) Best-effort: set in interpreter memory via MDI (requires armed + machine on + idle)
            # Split into chunks ≤250 chars to fit LinuxCNC's 256-char MDI buffer
            mdi_ok = False
            STAT.poll()
            if armed and bool(safe_get("enabled", False)) and not reject_if_auto_running():
                try:
                    items = [f"#{k}={float(v):.6f}" for k, v in vars_to_set.items()]
                    chunks, current = [], ""
                    for item in items:
                        if current and len(current) + 1 + len(item) > 250:
                            chunks.append(current)
                            current = item
                        else:
                            current = f"{current} {item}".strip() if current else item
                    if current:
                        chunks.append(current)
                    await set_mode(linuxcnc.MODE_MDI)
                    mdi_ok = True
                    for chunk in chunks:
                        ret = await _cmd_blocking(CMD.mdi, chunk, wait=5)
                        if ret != 0:
                            mdi_ok = False
                except Exception as e:
                    _trace.emit("probe.mdi_set_failed", level="warn",
                                exc=type(e).__name__, msg=str(e))
            _trace.emit("probe.set_vars_result",
                        file_saved=file_ok, mdi_set=mdi_ok)
            return {"ok": True, "file_saved": file_ok, "mdi_set": mdi_ok}

        if cmd == "get_probe_vars":
            var_nums = msg.get("vars", [])
            if not var_nums or not isinstance(var_nums, list):
                return {"ok": False, "error": "Missing vars list"}
            ini_path = getattr(STAT, "ini_filename", None)
            if not ini_path:
                return {"ok": False, "error": "No INI file"}
            ini = linuxcnc.ini(ini_path)
            var_file = ini.find("RS274NGC", "PARAMETER_FILE")
            if not var_file:
                return {"ok": False, "error": "No PARAMETER_FILE in INI"}
            if not os.path.isabs(var_file):
                var_file = os.path.join(os.path.dirname(ini_path), var_file)
            result = await asyncio.to_thread(_read_var_file, var_file, {str(v) for v in var_nums})
            _trace.emit("probe.get_vars", vars=result)
            return {"ok": True, "vars": result}

        if cmd == "get_wcs_table":
            return {"ok": True, "table": [row.copy() for row in _wcs_cache]}

        if cmd == "clear_wcs":
            require_armed(armed)
            blocked = reject_if_auto_running()
            if blocked:
                return blocked
            target = msg.get("target", "active")
            if target == "active":
                STAT.poll()
                indices = [STAT.g5x_index]  # 1-based
            elif target == "all":
                indices = list(range(1, 10))
            elif target in _G5X_MAP:
                indices = [_G5X_MAP[target]]
            else:
                return {"ok": False, "error": f"Invalid target: {target}"}
            await set_mode(linuxcnc.MODE_MDI)
            STAT.poll()
            machine_axes = [a.lower() for a in _axes_from_mask(STAT.axis_mask)]
            zero_parts = " ".join(f"{k.upper()}0" for k in machine_axes) + " R0"
            for p in indices:
                await _cmd_blocking(CMD.mdi, f"G10 L2 P{p} {zero_parts}", wait=5)
            # Update cache immediately
            for p in indices:
                ci = p - 1
                if 0 <= ci < 9:
                    _wcs_cache[ci] = {"name": _WCS_NAMES[ci], **{k: 0.0 for k in _WCS_AXIS_KEYS}, "r": 0.0}
            return {"ok": True, "table": [row.copy() for row in _wcs_cache]}

        if cmd == "set_wcs":
            require_armed(armed)
            require_no_eoffset()  # touch-off contamination guard (issue #19)
            blocked = reject_if_auto_running()
            if blocked:
                return blocked
            target = msg.get("target")
            if target not in _G5X_MAP:
                return {"ok": False, "error": f"Invalid WCS: {target}"}
            p = _G5X_MAP[target]
            parts = []
            all_keys = list(_WCS_AXIS_KEYS) + ["r"]
            for axis in all_keys:
                val = msg.get(axis)
                if val is not None:
                    parts.append(f"{axis.upper()}{float(val):.6f}")
            if not parts:
                return {"ok": False, "error": "No axis values provided"}
            await set_mode(linuxcnc.MODE_MDI)
            await _cmd_blocking(CMD.mdi, f"G10 L2 P{p} {' '.join(parts)}", wait=5)
            ci = p - 1
            for axis in all_keys:
                val = msg.get(axis)
                if val is not None:
                    _wcs_cache[ci][axis] = float(val)
            return {"ok": True, "table": [row.copy() for row in _wcs_cache]}

        return {"ok": False, "error": f"Unknown cmd: {cmd}"}

    except PermissionError as pe:
        return {"ok": False, "error": str(pe)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}



# -----------------------------
# Viewer support (Web 3D)
# -----------------------------

# ── Config-fallback degraded state (issue #21) ──
# A unit-system or machine-geometry fallback is dangerous for a CNC, so it is
# surfaced to the operator (status_msg["config_warning"]) instead of silently
# becoming a normal default. Flags latch until a subsequent successful read.
_units_fallback_active = False
_units_fallback_reason = ""
_config_warning_active = False
_config_warning_reason = ""


def _set_units_fallback(active: bool, reason: str = "") -> None:
    global _units_fallback_active, _units_fallback_reason
    if active and not _units_fallback_active:
        _trace.emit("config.units_fallback", level="warn", reason=reason)
    _units_fallback_active = active
    _units_fallback_reason = reason if active else ""


def get_machine_units() -> str:
    """Return 'in' or 'mm' based on INI [TRAJ]LINEAR_UNITS.

    Falling back to 'mm' is unit-ambiguous and unsafe for visualization/motion,
    so each fallback is surfaced via _set_units_fallback (issue #21) rather than
    silently returning a default.
    """
    if not STAT or not getattr(STAT, "ini_filename", None):
        _set_units_fallback(True, "no INI loaded")
        return "mm"
    try:
        lu = linuxcnc.ini(STAT.ini_filename).find("TRAJ", "LINEAR_UNITS")
        if not lu:
            _set_units_fallback(True, "[TRAJ]LINEAR_UNITS missing")
            return "mm"
        _set_units_fallback(False)
        return "in" if lu.strip().lower() in ("inch", "in", "imperial") else "mm"
    except Exception as e:
        _set_units_fallback(True, f"INI parse error: {type(e).__name__}")
        return "mm"


def _load_machine_config() -> dict:
    """Load machine model config from machine.json, or return hardcoded defaults."""
    global _config_warning_active, _config_warning_reason
    cfg_path = MACHINE_DIR / "machine.json"
    if cfg_path.exists():
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            print(f"[VINIT] Loaded machine config: {cfg.get('name', '?')}", flush=True)
            return cfg
        except Exception as e:
            # Wrong geometry is an operator-visible degraded state, not a silent
            # default (issue #21).
            _trace.emit_exc("config.machine_json_load_failed", e, level="warn")
            _config_warning_active = True
            _config_warning_reason = f"machine.json load failed ({type(e).__name__}) — using default geometry"
    else:
        _trace.emit("config.machine_json_missing", level="warn", path=str(cfg_path))
        _config_warning_active = True
        _config_warning_reason = "machine.json missing — using default geometry"
    # Fallback: hardcoded defaults (original PM-25MV setup)
    return {
        "name": "Default",
        "groups": [
            {"id": "x", "parent": "root"},
            {"id": "y", "parent": "root"},
            {"id": "z", "parent": "y"},
            {"id": "tool", "parent": "z"},
        ],
        "parts": [
            {"id": "frame",  "file": "frame.stl",  "group": None, "translate": [-760, -122, -294]},
            {"id": "x_axis", "file": "x_axis.stl", "group": "x",  "translate": [319, 398, -244]},
            {"id": "y_axis", "file": "y_axis.stl", "group": "y",  "translate": [-140, 0, 21]},
            {"id": "z_axis", "file": "z_axis.stl", "group": "z",  "translate": [0, 0, 0]},
        ],
        "kinematics": [
            {"group": "x", "joint": 0, "direction": "x", "sign": -1},
            {"group": "y", "joint": 1, "direction": "y", "sign":  1},
            {"group": "z", "joint": 2, "direction": "z", "sign":  1},
        ],
        "workGroup": "x",
        "toolGroup": "tool",
    }


_bt = time.monotonic()
MACHINE_CFG = _load_machine_config()
_trace.emit(
    "boot.machine_config",
    dt_ms=round((time.monotonic() - _bt) * 1000, 1),
    name=MACHINE_CFG.get("name", "?"),
)


def _stl_versioned(filename: str) -> str:
    """Append ?v=<mtime> for immutable browser caching with automatic invalidation."""
    p = MACHINE_DIR / filename
    mtime = int(p.stat().st_mtime) if p.exists() else 0
    return f"{filename}?v={mtime}"


_AXIS_LETTERS = "XYZABCUVW"


def _axes_from_mask(mask: int) -> List[str]:
    """Derive axis letter list from LinuxCNC axis_mask bitmask."""
    return [_AXIS_LETTERS[i] for i in range(9) if mask & (1 << i)]


# Cache for build_viewer_init() output. Keyed on every input that can
# change at runtime: stl_base_url (per-client host header), INI path +
# mtime, STAT.axis_mask, STAT.max_velocity (frontend uses it as a jog-
# velocity fallback), and per-STL mtimes. With ~10 clients reconnecting
# in a storm, the second client onward gets a sub-µs cache hit instead
# of paying the file-I/O cost of re-parsing the INI per connect — the
# cost we measured at ~21 ms in [VINIT-T] worst-case. Bounded at 16
# entries so distinct host headers can't grow it without limit.
_viewer_init_cache: Dict[Tuple, Dict[str, Any]] = {}


def _viewer_init_cache_key(stl_base_url: str) -> Tuple:
    ini_filename = getattr(STAT, "ini_filename", None) if STAT else None
    if ini_filename and os.path.exists(ini_filename):
        try:
            ini_mtime = int(os.path.getmtime(ini_filename))
        except OSError:
            ini_mtime = 0
    else:
        ini_mtime = 0
    stl_mtimes: Tuple[int, ...] = tuple(
        int((MACHINE_DIR / p["file"]).stat().st_mtime)
        if (MACHINE_DIR / p["file"]).exists() else 0
        for p in MACHINE_CFG.get("parts", [])
    )
    axis_mask = getattr(STAT, "axis_mask", 0) if STAT else 0
    max_v = safe_get("max_velocity", 0.0) or 0.0
    return (
        stl_base_url,
        ini_filename or "",
        ini_mtime,
        int(axis_mask),
        round(float(max_v), 3),
        stl_mtimes,
    )


def build_viewer_init(stl_base_url: str) -> Dict[str, Any]:
    """Build viewer init payload from machine.json config + INI-derived bounds.

    Result is cached on (stl_base_url, ini mtime, axis_mask, max_velocity,
    stl_mtimes). Storm of 10+ reconnecting clients pays the build cost
    once and then hits cache; cache invalidates automatically if the
    operator edits the INI, swaps a machine STL, or changes max_velocity
    via the UI."""
    # Cache lookup. Misses fall through to the existing build below.
    _cache_key = _viewer_init_cache_key(stl_base_url)
    _cached = _viewer_init_cache.get(_cache_key)
    if _cached is not None:
        _trace.emit("viewer_init.cache_hit", dt_ms=0)
        return _cached

    # Per-step timing collected for [CONN] probe; survives normal startup with
    # negligible cost (a few time.monotonic() calls).
    _bvi_t0 = time.monotonic()
    _bvi_steps: Dict[str, float] = {}

    def _mark(step: str, t0: float) -> None:
        _bvi_steps[step] = (time.monotonic() - t0) * 1000

    _t = time.monotonic()
    limits = read_machine_limits_from_ini(STAT) if STAT else None
    _mark("read_machine_limits", _t)
    if limits:
        bounds_origin, bounds_size = limits
    else:
        bounds_origin, bounds_size = [0, 0, 0], [0, 0, 0]

    _t = time.monotonic()
    units = get_machine_units()
    _mark("get_machine_units", _t)

    # Axis letters from axis_mask (e.g. XYZ=7, XYZAC=39). If LinuxCNC hasn't
    # connected yet, we ship no axes — the client waits for viewer_init before
    # rendering axis-dependent UI.
    _t = time.monotonic()
    if STAT:
        STAT.poll()
        axes = _axes_from_mask(STAT.axis_mask)
    else:
        axes = []
    _mark("stat_poll_axes", _t)

    # Build parts with cache-busted filenames
    parts = []
    for p in MACHINE_CFG.get("parts", []):
        parts.append({
            "id": p["id"],
            "file": _stl_versioned(p["file"]),
            "group": p.get("group"),
            "translate": p.get("translate"),
            "rotate": p.get("rotate"),
        })

    # INI/static fields — delivered once per connect so the per-tick status
    # payload doesn't re-ship them to every client every cycle.
    _t = time.monotonic()
    ini_cfg = get_ini_config()
    _mark("get_ini_config", _t)
    ini_filename = getattr(STAT, "ini_filename", None) if STAT else None
    ini_config = {
        "ini_filename": ini_filename,
        "linear_units": ini_cfg.get("linear_units"),
        "max_velocity": safe_get("max_velocity", None),
        "max_jog_velocity": get_max_jog_velocity(),
        "default_jog_velocity": ini_cfg.get("default_jog_velocity"),
        "min_jog_velocity": ini_cfg.get("min_jog_velocity"),
        "max_angular_jog_velocity": ini_cfg.get("max_angular_jog_velocity"),
        "default_angular_jog_velocity": ini_cfg.get("default_angular_jog_velocity"),
        "min_angular_jog_velocity": ini_cfg.get("min_angular_jog_velocity"),
        "increments": ini_cfg.get("increments"),
        "default_spindle_speed": ini_cfg.get("default_spindle_speed"),
        "min_spindle_speed": ini_cfg.get("min_spindle_speed"),
        "max_spindle_speed": ini_cfg.get("max_spindle_speed"),
        "min_spindle_override": ini_cfg.get("min_spindle_override"),
        "max_spindle_override": ini_cfg.get("max_spindle_override"),
        "max_feed_override": ini_cfg.get("max_feed_override"),
        "debug": ini_cfg.get("debug", False),
    }

    out = {
        "units": units,
        "stl_base_url": stl_base_url,
        "axes": axes,
        "machine_bounds": {
            "origin": bounds_origin,
            "size": bounds_size,
        },
        "groups": MACHINE_CFG.get("groups", []),
        "parts": parts,
        "kinematics": MACHINE_CFG.get("kinematics", []),
        "workGroup": MACHINE_CFG.get("workGroup"),
        "toolGroup": MACHINE_CFG.get("toolGroup"),
        "ini_config": ini_config,
    }
    _bvi_total = (time.monotonic() - _bvi_t0) * 1000
    _trace.emit(
        "viewer_init.timing",
        total_ms=round(_bvi_total, 1),
        steps={k: round(v, 1) for k, v in _bvi_steps.items()},
    )
    # Store under the same key we looked up with at the top.
    _viewer_init_cache[_cache_key] = out
    # Bounded cache — if a fleet of clients with distinct host headers
    # connects, evict oldest (insertion-order) so we don't grow forever.
    if len(_viewer_init_cache) > 16:
        _viewer_init_cache.pop(next(iter(_viewer_init_cache)))
    return out


def _build_wcs_rotation_patches() -> Dict[str, str]:
    """Build {param_number: str(value)} rotation patches for the parse worker.

    LinuxCNC only writes the var file on shutdown, so after G10 L2 R changes
    the disk copy has stale rotation. Only rotation (base + 10) is patched —
    axis offsets on disk are already correct in interpreter-internal units.
    (_wcs_cache mixes internal units from seed with machine units from STAT,
    so patching axis offsets would corrupt them.)
    """
    patches: Dict[str, str] = {}
    if not _wcs_cache:
        return patches
    for i, base in enumerate(_WCS_BASES):
        row = _wcs_cache[i]
        if row and "r" in row:
            patches[str(base + 10)] = f"{row['r']:.6f}"
    return patches


_gcode_parse_proc: Optional[subprocess.Popen] = None  # tracked so lifespan can terminate it


def _run_gcode_worker_blocking(ctx_bytes: bytes, timeout: float):
    """Spawn the parse worker and run it to completion. Runs in a worker thread
    (via asyncio.to_thread) so the fork happens OFF the event loop — B7.

    asyncio.create_subprocess_exec forks the (large) gateway process
    synchronously on the loop; under load that copy-on-write fork + pipe
    registration stalled the loop ~60 ms (#35 attribution: a 62 ms
    _SelectorTransport._add_reader). stdlib subprocess.Popen here forks inside
    the thread (the fork syscall releases the GIL, so the loop keeps running) and
    uses posix_spawn where the platform allows, which is cheaper still. The Popen
    handle is published to _gcode_parse_proc so lifespan shutdown can terminate an
    in-flight parse. Returns (returncode, stdout, stderr); raises
    subprocess.TimeoutExpired on timeout (child already killed + reaped)."""
    global _gcode_parse_proc
    proc = subprocess.Popen(
        [sys.executable, _GCODE_WORKER_PATH],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _gcode_parse_proc = proc
    try:
        stdout, stderr = proc.communicate(input=ctx_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # reap the killed child so it doesn't zombie
        raise
    return proc.returncode, stdout, stderr


async def _refresh_gcode_preview(filepath: str):
    """Parse filepath in an isolated subprocess and publish the result.

    Called from _status_poller on file change. Single-flight via
    _gcode_refresh_running — the caller sets the flag before scheduling, this
    coroutine clears it on exit. The subprocess has its own Python interpreter
    and its own GIL, so _heartbeat_loop keeps ticking through the parse even
    for multi-second programs.
    """
    global _gcode_preview_pending, _gcode_preview_version
    global _gcode_last_file, _gcode_last_mtime, _gcode_refresh_running
    global _gcode_preview_bytes, _gcode_preview_bytes_gz, _gcode_parse_proc
    t_start = time.monotonic()
    # Snapshot mtime BEFORE the parse: if an edit lands while the subprocess is
    # running, we record the pre-parse mtime, so the poller's next tick still
    # sees a mismatch and re-parses the newest content rather than missing it.
    try:
        _mtime_at_parse: Optional[float] = os.path.getmtime(filepath)
    except OSError:
        _mtime_at_parse = None
    try:
        ini_path = getattr(STAT, "ini_filename", None) if STAT is not None else None
        if not ini_path:
            return
        active_idx = getattr(STAT, "g5x_index", None) if STAT is not None else None
        patches = _build_wcs_rotation_patches()
        ctx = {
            "file": filepath,
            "ini_path": ini_path,
            "units": get_machine_units(),
            "var_patches": patches,
            "g5x_index": active_idx if isinstance(active_idx, int) else 1,
        }
        ctx_bytes = _msgpack_encoder.encode(ctx)
        _trace.emit("gcode.spawn_start",
                    file=os.path.basename(filepath), active_idx=active_idx)

        t_spawn = time.monotonic()
        # Spawn + run the worker entirely off the event loop (B7): the fork no
        # longer stalls the loop. communicate() (write ctx, read stdout/stderr,
        # wait) and the 60 s timeout all run in the thread.
        try:
            returncode, stdout, stderr = await asyncio.to_thread(
                _run_gcode_worker_blocking, ctx_bytes, 60.0)
        except subprocess.TimeoutExpired:
            _trace.emit("gcode.parse_timeout", level="warn", file=filepath)
            return
        t_communicated = time.monotonic()
        if returncode != 0:
            err_tail = stderr.decode(errors="replace")[:500] if stderr else ""
            _trace.emit("gcode.parse_worker_failed", level="warn",
                        rc=returncode, stderr_tail=err_tail)
            return
        # Surface worker-side timing + lift the partial-parse marker into a
        # structured event WITHOUT decoding the (multi-MB) stdout payload.
        if stderr:
            for ln in stderr.decode(errors="replace").splitlines():
                if not ln.strip():
                    continue
                if ln.startswith("__PARTIAL__"):
                    _p = ln.split("\t", 2)
                    _trace.emit("gcode.parse_partial", level="warn", file=filepath,
                                error=_p[2] if len(_p) > 2 else "",
                                error_line=_p[1] if len(_p) > 1 else "")
                else:
                    _trace.emit("gcode.worker_log", line=ln)
        _trace.emit("gcode.worker_done",
                    parse_ms=round((t_communicated - t_spawn) * 1000, 1),
                    stdout_bytes=len(stdout))
        if not stdout:
            _trace.emit("gcode.preview_refresh_failed", level="warn",
                        file=filepath, exc="EmptyOutput", msg="worker emitted no bytes")
            return

        # PASSTHROUGH (mmw#4 / GC): the worker already emits the EXACT GET /preview
        # wire shape (incl. "file"), so we publish its bytes verbatim — no decode +
        # re-encode. Decoding inflated the payload into hundreds of thousands of
        # tiny [x,y,z] list objects purely to re-serialize them, and that fresh
        # live-object population is what drove gen-0/gen-1 GC scans to 50-120 ms
        # (the HB-WAKEs). Nothing in the gateway reads the polylines as Python
        # objects — both consumers only need `file` — so we keep just that. gzip
        # runs on the opaque bytes off-thread (GIL-releasing C, allocates no
        # tracked objects). Clients fetch over HTTP (GET /preview), off the WS writer.
        t_gz0 = time.monotonic()
        preview_bytes_gz: Optional[bytes] = None
        if len(stdout) >= 4096:
            preview_bytes_gz = await asyncio.to_thread(gzip.compress, stdout, 6)
        t_gz_done = time.monotonic()
        # Publish metadata + bytes together before bumping the version so
        # GET /preview readers never see stale bytes under a new version.
        _gcode_preview_pending = {"file": filepath}
        _gcode_preview_bytes = stdout
        _gcode_preview_bytes_gz = preview_bytes_gz
        _gcode_preview_version += 1
        _gcode_last_file = filepath
        _gcode_last_mtime = _mtime_at_parse
        _trace.emit("gcode.publish",
                    version=_gcode_preview_version,
                    gzip_ms=round((t_gz_done - t_gz0) * 1000, 1),
                    bytes=len(stdout),
                    bytes_gz=len(preview_bytes_gz) if preview_bytes_gz else 0,
                    total_ms=round((t_gz_done - t_start) * 1000, 1))
    except Exception as e:
        _trace.emit("gcode.preview_refresh_failed", level="warn",
                    file=filepath, exc=type(e).__name__, msg=str(e))
    finally:
        _gcode_refresh_running = False
        _gcode_parse_proc = None


# ---- Lifespan shutdown registry ----
# Long-lived tasks register themselves here so the FastAPI lifespan can cancel
# them on shutdown. uvicorn replaces our module-level signal handlers, so the
# only deterministic shutdown path is FastAPI's lifespan exit.
_shutting_down: bool = False
_bg_tasks: set[asyncio.Task] = set()

def register_bg_task(t: asyncio.Task) -> asyncio.Task:
    """Register a long-lived task so lifespan can cancel it. Returns the task."""
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


async def _terminate_parse_proc(proc) -> None:
    """Terminate an in-flight G-code parse subprocess, bounded so a stuck child can't
    hang the deterministic shutdown: SIGTERM, wait ≤1 s, then SIGKILL. wait() is a
    blocking stdlib Popen call (B7), so it's offloaded via to_thread. No-op when the
    proc is None or already exited. Callers pass a LOCAL handle (captured before any
    concurrent _refresh_gcode_preview finally can clear the global), so the child is
    always either live (terminate works) or already reaped (poll short-circuits)."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=1.0)
    except asyncio.TimeoutError:
        proc.kill()
        await asyncio.to_thread(proc.wait)


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    _trace.emit("boot.lifespan_ready")
    # Asyncio loop exists only after lifespan startup — wire the
    # unhandled-task hook here so uvicorn's own handler is preserved.
    _trace.install_asyncio_handler("gateway")
    # === TEMP IDLE-GATEWAY PROBE === start the status poller from lifespan
    # rather than waiting for the first WS connect, so the [IDLE] log fires
    # during the pre-first-client window. The poller's `if not _clients`
    # branch sleeps cheaply, so running from boot is harmless.
    _start_status_poller()
    # Warm the machine-path caches now (both resolve from LCNC_INI_FILE, which the
    # launcher exports before uvicorn, and neither touches NML) so the first
    # /upload or tool op doesn't pay an INI parse / makedirs on the event loop (B4).
    try:
        get_nc_files_dir()
        get_tool_tbl_path()
    except Exception as e:
        _trace.emit_exc("boot.path_warmup_failed", e)
    # Opt-in event-loop attribution (issue #35): with WEBUI_ASYNCIO_DEBUG=1 asyncio
    # logs "Executing <coro …> took N seconds" for any callback holding the loop
    # >50 ms (half the [HB-WAKE] threshold), naming the exact culprit behind a
    # stall. Off by default — asyncio debug mode adds per-callback overhead, an
    # observer effect on the hot path, so it's a diagnostic toggle, not always-on.
    if os.environ.get("WEBUI_ASYNCIO_DEBUG") == "1":
        try:
            _dbg_loop = asyncio.get_running_loop()
            _dbg_loop.slow_callback_duration = 0.05
            _dbg_loop.set_debug(True)
            _trace.emit("boot.asyncio_debug_enabled", level="warn", slow_callback_ms=50)
            print("[ASYNCIO-DEBUG] slow-callback logging enabled (>50ms)", flush=True)
        except Exception as e:
            _trace.emit_exc("boot.asyncio_debug_failed", e)
    # Optional (mmw#4): move the long-lived startup heap out of GC's reach. gen-2
    # (full-heap) collections scan every tracked object and freeze the loop while
    # they run; the startup set (modules, app, caches, viewer init) is never
    # garbage, so scanning it every gen-2 is pure waste (mmw#4 saw 3 of 4
    # collections free 0 objects). collect() first so startup cyclic garbage is
    # reclaimed (not frozen forever), then freeze() promotes the survivors to the
    # permanent generation — later gen-2 collections scan only post-startup allocs.
    # freeze() does NOT disable GC: refcounting is untouched and new objects still
    # flow gen0→1→2, so runtime memory (incl. post-startup cycles) is still fully
    # collected — the frozen set is a one-time snapshot, so this can't pile up over
    # time. Default OFF: it's a narrow optimization with a small risk (a startup
    # cycle that later dies is never reclaimed), so best practice is to A/B it on
    # the target (WEBUI_GC_FREEZE=1) — confirm gen-2 durations drop AND RSS stays
    # flat via the [GC] logs — then flip this default ON once proven.
    if os.environ.get("WEBUI_GC_FREEZE") == "1":
        try:
            _gc.collect()
            _gc.freeze()
            _trace.emit("boot.gc_frozen")
            print("[GC] froze startup heap — gen-2 now scans only new allocations", flush=True)
        except Exception as e:
            _trace.emit_exc("boot.gc_freeze_failed", e)
    yield
    # ---- Shutdown ----
    # Order matters. Each step is bounded so a stuck client/socket can't block
    # the rest. Total worst case ~5s — sized to fit uvicorn's
    # --timeout-graceful-shutdown 5 in the launcher.
    global _shutting_down
    _shutting_down = True
    # Re-anchored to module-level _T0 so [SHUTDOWN] deltas line up with
    # [BOOT]/[CONN]/[SHUTDOWN-PROBE] on a single timeline.
    def _elapsed() -> str:
        return f"+{(time.monotonic() - _T0) * 1000:.0f}ms"
    print(f"[SHUTDOWN] {_elapsed()} lifespan teardown begin", flush=True)

    # 1. Notify all clients before tearing the connection.
    snapshot = list(_clients.values())
    if snapshot:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(ws_send_json(c.ws, {"type": "server_shutdown"}) for c in snapshot),
                    return_exceptions=True,
                ),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            print(f"[SHUTDOWN] {_elapsed()} server_shutdown broadcast timed out", flush=True)
        print(f"[SHUTDOWN] {_elapsed()} broadcast server_shutdown to {len(snapshot)} clients", flush=True)

    # 2. Close WS connections (1001 = going away).
    if snapshot:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(c.ws.close(code=1001) for c in snapshot),
                    return_exceptions=True,
                ),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            print(f"[SHUTDOWN] {_elapsed()} WS close timed out", flush=True)
        print(f"[SHUTDOWN] {_elapsed()} closed {len(snapshot)} WS connection(s)", flush=True)

    # 3. Cancel background tasks. Must precede step 4: _disconnect_grace and
    # _heartbeat_loop continuously toggle the heartbeat field via _hal_send;
    # if still running when step 4 writes the deterministic LOW, the watchdog
    # could see a flipped value on the wire.
    if _bg_tasks:
        tasks = list(_bg_tasks)
        for t in tasks:
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            print(f"[SHUTDOWN] {_elapsed()} {sum(1 for t in tasks if not t.done())} bg task(s) did not finish in 2s", flush=True)
        print(f"[SHUTDOWN] {_elapsed()} cancelled {len(tasks)} bg tasks", flush=True)

    # 4. Final HAL state — deterministic LOW transition. hal_watchdog also
    # forces pins LOW on socket close (recv empty data), but explicit is
    # better here so the trip-latch / safety chain settles before halrun
    # unloads the watchdog component.
    try:
        _hal_send({"heartbeat": False, "connected": False})
        print(f"[SHUTDOWN] {_elapsed()} sent final HAL state heartbeat=False connected=False", flush=True)
    except Exception as e:
        print(f"[SHUTDOWN] {_elapsed()} final _hal_send failed: {e}", flush=True)

    # 5. Disconnect HAL sockets (must follow step 4 — _hal_send needs the socket).
    _hal_disconnect()
    if _reader_writer is not None:
        try:
            _reader_writer.close()
            await asyncio.wait_for(_reader_writer.wait_closed(), timeout=0.5)
        except (asyncio.TimeoutError, Exception) as e:
            print(f"[SHUTDOWN] {_elapsed()} reader writer close: {type(e).__name__}: {e}", flush=True)
    print(f"[SHUTDOWN] {_elapsed()} HAL sockets disconnected", flush=True)

    # 6. Terminate gcode parse subprocess if alive. Capture the handle LOCALLY first
    # so a concurrent _refresh_gcode_preview finally clearing the global can't drop it
    # mid-terminate (review #1). _terminate_parse_proc bounds the wait (to_thread).
    proc = _gcode_parse_proc
    if proc is not None:
        try:
            await _terminate_parse_proc(proc)
            print(f"[SHUTDOWN] {_elapsed()} gcode parse subprocess handled rc={proc.returncode}", flush=True)
        except Exception as e:
            print(f"[SHUTDOWN] {_elapsed()} gcode proc terminate failed: {e}", flush=True)

    # 7. Camera.
    try:
        _camera_release()
        print(f"[SHUTDOWN] {_elapsed()} camera released", flush=True)
    except Exception as e:
        print(f"[SHUTDOWN] {_elapsed()} camera release failed: {e}", flush=True)

    print(f"[SHUTDOWN] {_elapsed()} lifespan teardown complete", flush=True)


app = FastAPI(lifespan=lifespan)
_trace.emit("boot.app_instantiated")

# CORS is defense-in-depth only — the WS Origin gate and the REST token
# dependency are the real boundary (issue #17). When an explicit allow-list is
# configured, honour it (plus dev origins); otherwise fall back to "*", since
# the gateway can't enumerate its own same-host origins at config time and the
# token (a custom header, never served cross-origin) is what actually gates.
_cors_origins = sorted(_ALLOWED_ORIGINS | _DEV_ORIGINS) if _ALLOWED_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CacheStaticAssets:
    """Pure ASGI middleware — adds Cache-Control to /assets/ and /static/ responses."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        p = scope.get("path", "")
        if scope["type"] != "http" or not (p.startswith("/assets/") or p.startswith("/static/")):
            await self.app(scope, receive, send)
            return

        async def send_with_cache(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache)

app.add_middleware(CacheStaticAssets)


@app.middleware("http")
async def trace_http(request: Request, call_next):
    """Phase-track + emit start/end for every HTTP request. Single hook
    covers all routes so we never miss one (the audit found 15+ routes
    that were uninstrumented under the old per-handler approach)."""
    t0 = time.monotonic()
    path = request.url.path
    method = request.method
    peer = "?"
    try:
        if request.client is not None:
            peer = f"{request.client.host}:{request.client.port}"
    except Exception:
        pass  # safe-silent: peer label is cosmetic, "?" is a fine fallback
    # Skip /assets and /static (served by StaticFiles, no instrumentation
    # value, and they fan out a lot during cold-load).
    if path.startswith("/assets/") or path.startswith("/static/"):
        return await call_next(request)
    _set_phase(f"http.{method}.{path}")
    # /telemetry is high-frequency, low-value diagnostics (best-effort browser
    # event batches). Emitting http.start + http.end for each one floods the
    # trace bus and was itself measurable event-loop load (P0.3). Skip the
    # routine pair for it; still surface slow (>50 ms) or error completions.
    is_telemetry = path == "/telemetry"
    if not is_telemetry:
        _trace.emit("http.start", path=path, method=method, peer=peer)
    status = 0
    try:
        resp = await call_next(request)
        try:
            status = int(getattr(resp, "status_code", 0) or 0)
        except Exception:
            status = 0
        return resp
    except Exception as e:
        _trace.emit("http.error", level="error", path=path, method=method,
                    peer=peer, exc=type(e).__name__, msg=str(e))
        raise
    finally:
        dur = (time.monotonic() - t0) * 1000
        if not is_telemetry or dur > 50 or status >= 400 or status == 0:
            level = "warn" if dur > 50 else "info"
            _trace.emit("http.end", level=level, path=path, method=method,
                        peer=peer, duration_ms=round(dur, 1), status=status)


# Serve static machine assets (STLs etc.)
# Always resolve relative to THIS FILE, not cwd

app.mount("/assets", StaticFiles(directory=str(MACHINE_DIR), html=False), name="assets")



@app.get("/health")
def health():
    return {"ok": True}


@app.post("/telemetry")
async def telemetry(request: Request):
    """Browser → server telemetry sink.

    Body is NDJSON: one JSON object per line. Each event is forwarded to
    the trace bus tagged `browser.<event_kind>`. Designed to be cheap and
    survive partial / malformed batches: a bad line is dropped, not 500'd.
    Used by lcncWs.ts to report tab visibility, WS connect/disconnect,
    send-buffer pressure, and JS errors.

    Sized for ~1 KB / s / tab steady state with sendBeacon flushes on
    pagehide. Treat as untrusted: every field becomes a string in the
    trace, never executed or rendered raw.
    """
    raw = b""
    try:
        raw = await request.body()
    except Exception:
        return {"ok": False, "error": "body_read_failed"}
    if not raw:
        return {"ok": True, "events": 0}
    peer = "?"
    try:
        if request.client is not None:
            peer = f"{request.client.host}:{request.client.port}"
    except Exception:
        pass  # safe-silent: peer label is cosmetic, "?" is a fine fallback
    accepted = 0
    rejected = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            rejected += 1
            continue
        if not isinstance(evt, dict):
            rejected += 1
            continue
        kind = str(evt.get("kind") or evt.get("tag") or "event")
        # Forward all fields as-is (strings/numbers/bools only after JSON
        # decode anyway). Prefix tag with `browser.` so the merged trace
        # makes the source obvious.
        fields = {k: v for k, v in evt.items() if k not in ("kind", "tag")}
        fields["peer"] = peer
        _trace.emit(f"browser.{kind}", **fields)
        accepted += 1
    return {"ok": True, "events": accepted, "rejected": rejected}


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass  # safe-silent: best-effort temp cleanup, already-gone is fine


async def _atomic_stream_upload(file: "UploadFile", dest_path: str,
                                max_bytes: int, chunk_size: int = 1 << 20) -> int:
    """Stream an upload to ``dest_path`` atomically and bounded, keeping the
    event loop free.

    Reads are async (Starlette's threadpool — yields to the loop between chunks);
    every blocking disk op (write/flush/fsync/replace/unlink) is offloaded via
    run_in_executor, so a slow disk, an fsync, or a large file can't stall the
    HAL heartbeat (#35). The body is never materialized whole — that also avoids
    the multi-MB allocation burst that feeds gen-2 GC (mmw#4).

    Bounded: rejects with 413 the instant the running byte count exceeds
    ``max_bytes`` (no oversized buffering). Atomic + durable: writes to a
    ``.part`` temp in the destination dir, fsyncs, then ``os.replace`` — and
    removes the temp on ANY failure, so LinuxCNC never sees a partial file.
    Returns the number of bytes written.
    """
    loop = asyncio.get_event_loop()
    dest_dir = os.path.dirname(dest_path) or "."
    fd, tmp = await loop.run_in_executor(
        None, lambda: tempfile.mkstemp(dir=dest_dir, suffix=".part")
    )
    written = 0
    try:
        f = os.fdopen(fd, "wb")
        try:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
                await loop.run_in_executor(None, f.write, chunk)
            await loop.run_in_executor(None, lambda: (f.flush(), os.fsync(f.fileno())))
        finally:
            await loop.run_in_executor(None, f.close)
        await loop.run_in_executor(None, os.replace, tmp, dest_path)
        tmp = None  # published — don't unlink in finally
        return written
    finally:
        if tmp is not None:
            await loop.run_in_executor(None, _safe_unlink, tmp)


@app.post("/upload", dependencies=[Depends(require_token)])
async def upload_gcode(file: UploadFile = File(...)):
    """Upload a G-code file to the NC files directory."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    safe_name = sanitize_filename(file.filename)
    if not validate_extension(safe_name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    nc_dir = get_nc_files_dir()
    dest_path = os.path.join(nc_dir, safe_name)

    if not validate_path_within(dest_path, nc_dir):
        raise HTTPException(status_code=400, detail="Invalid filename")

    try:
        size = await _atomic_stream_upload(file, dest_path, MAX_UPLOAD_SIZE)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    return {"ok": True, "path": dest_path, "filename": safe_name, "size": size}


@app.put("/save", dependencies=[Depends(require_token)])
async def save_gcode(path: str = Body(...), content: str = Body(...)):
    """Save edited G-code content back to an existing file."""
    nc_dir = get_nc_files_dir()
    abs_path = os.path.abspath(path)

    if not validate_path_within(abs_path, nc_dir):
        raise HTTPException(status_code=400, detail="Path outside NC files directory")

    if not validate_extension(abs_path):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    # Encode + size-check + atomic write+fsync ALL off the event loop (#35, P1.1):
    # the UTF-8 encode of a multi-MB editor save holds the GIL, so running it inline
    # (as before) stalled the HAL heartbeat just like the disk write did. fsync for
    # durable atomic publication — LinuxCNC must never read a half-written file.
    def _encode_check_write() -> Tuple[bool, int]:
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_UPLOAD_SIZE:
            return (False, len(encoded))
        atomic_write_bytes(abs_path, encoded, fsync=True)
        return (True, len(encoded))

    try:
        loop = asyncio.get_event_loop()
        ok, size = await loop.run_in_executor(None, _encode_check_write)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")
    if not ok:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")

    return {"ok": True, "path": abs_path, "size": size}


# ---- Fusion 360 Tool Library Import ----

_FUSION_TYPE_MAP = {
    "flat end mill": "endmill",
    "ball end mill": "ball",
    "bull nose end mill": "bullnose",
    "chamfer mill": "chamfer",
    "drill": "drill",
    "spot drill": "drill",
    "counter bore": "endmill",
    "reamer": "endmill",
    "boring bar": "endmill",
    "center drill": "centerdrill",
    "counter sink": "countersink",
    "dovetail mill": "dovetail",
    "face mill": "facemill",
    "lollipop mill": "lollipop",
    "slot mill": "slotmill",
    "thread mill": "threadmill",
    "form mill": "formmill",
    "radius mill": "radiusmill",
    "tapered mill": "tapered",
    "probe": "probe",
    "tap right hand": "tap",
    "tap left hand": "tap",
    "engraving cutter": "engraver",
}


def _fusion_unit_scale(src: Optional[str], machine_unit: str) -> float:
    """Multiplier to convert a Fusion `unit` field value to machine native units.
    Fusion writes "millimeters" or "inches"; default to mm if missing/unknown.
    """
    src_mm = not (src or "millimeters").lower().startswith("in")
    machine_mm = machine_unit == "mm"
    if src_mm == machine_mm:
        return 1.0
    return 25.4 if (not src_mm and machine_mm) else (1.0 / 25.4)


def _opt_scale(v, scale: float):
    return None if v is None else v * scale


def _parse_fusion_library(data: dict, machine_unit: str) -> tuple[list, list]:
    """Parse a Fusion 360 Library.json → (tools, skipped_duplicates).

    Tools with duplicate numbers are excluded from the main list and returned
    separately so the caller can warn about them.  The *first* occurrence of
    each number is kept; later duplicates are skipped.

    Linear dimensions are converted from each entry's `unit` (and each
    holder's `unit`) into the machine's native linear unit. ``machine_unit`` is
    passed in (resolved on the event loop, since get_ini_config() may STAT.poll)
    so this stays NML-free and safe to run in an executor thread (B2).
    """
    tools: list[dict] = []
    skipped: list[dict] = []
    seen_nums: dict[int, int] = {}          # tool_num → index in tools[]
    for entry in data.get("data", []):
        pp = entry.get("post-process", {})
        geom = entry.get("geometry", {})
        presets = entry.get("start-values", {}).get("presets", [])
        holder = entry.get("holder", {})

        tool_num = pp.get("number")
        if tool_num is None:
            continue

        fusion_type = entry.get("type", "")
        our_type = _FUSION_TYPE_MAP.get(fusion_type, "other")

        tool_scale = _fusion_unit_scale(entry.get("unit"), machine_unit)

        tool = {
            "T": int(tool_num),
            "D": float(geom.get("DC", 0)) * tool_scale,
            "description": entry.get("description", "").strip(),
            "type": our_type,
            "flutes": geom.get("NOF"),
            "oal": _opt_scale(geom.get("OAL"), tool_scale),
            "flute_length": _opt_scale(geom.get("LCF"), tool_scale),
            "corner_radius": _opt_scale(geom.get("RE"), tool_scale),
            "body_length": _opt_scale(geom.get("LB"), tool_scale),
            "shaft_diameter": _opt_scale(geom.get("SFDM"), tool_scale),
            "taper_angle": geom.get("TA"),
            "point_angle": geom.get("SIG"),
            "tip_diameter": _opt_scale(geom.get("tip-diameter"), tool_scale),
            "shoulder_length": _opt_scale(geom.get("shoulder-length"), tool_scale),
            "shoulder_diameter": _opt_scale(geom.get("shoulder-diameter"), tool_scale),
            "assembly_gauge_length": _opt_scale(geom.get("assemblyGaugeLength"), tool_scale),
            "material": entry.get("BMC"),
            "holder": holder.get("description") if holder else None,
            "fusion_type": fusion_type,
        }
        # ---- Per-type angle normalization (Fusion stores half-angles for some types) ----
        # Source: FreeCAD Better Tool Library reverse-engineering of Fusion 360 geometry keys
        if our_type in ("chamfer", "countersink", "centerdrill"):
            # Fusion TA is half-angle for chamfer/countersink — double to get included angle
            if tool.get("taper_angle"):
                tool["taper_angle"] *= 2
        if our_type in ("countersink", "centerdrill"):
            # Fusion SIG is half-angle for countersink/centerdrill — double to get included angle
            if tool.get("point_angle"):
                tool["point_angle"] *= 2
        # (drill/spot drill SIG is already the full included angle — no adjustment needed)

        # Holders carry their own `unit` independent of the tool body.
        holder_segs = holder.get("segments", []) if holder else []
        if holder_segs:
            holder_scale = _fusion_unit_scale(holder.get("unit"), machine_unit)
            tool["holder_segments"] = [
                {"height": s["height"] * holder_scale,
                 "lower_diameter": s["lower-diameter"] * holder_scale,
                 "upper_diameter": s["upper-diameter"] * holder_scale}
                for s in holder_segs if "height" in s
            ]
        # Form-mill profile coords share the tool's unit; arcs add a `center` pair.
        if our_type == "formmill":
            raw_profile = geom.get("profile")
            if raw_profile and isinstance(raw_profile, list):
                scaled_profile = []
                for seg in raw_profile:
                    new_seg = dict(seg)
                    if "end" in seg:
                        new_seg["end"] = [seg["end"][0] * tool_scale,
                                          seg["end"][1] * tool_scale]
                    if "center" in seg:
                        new_seg["center"] = [seg["center"][0] * tool_scale,
                                             seg["center"][1] * tool_scale]
                    scaled_profile.append(new_seg)
                tool["profile"] = scaled_profile
        # Preserve raw presets (speeds/feeds per material) for sidecar
        if presets:
            tool["presets"] = presets

        t_int = int(tool_num)
        if t_int in seen_nums:
            skipped.append(tool)
        else:
            seen_nums[t_int] = len(tools)
            tools.append(tool)
    return tools, skipped


def _decode_fusion_blob(raw: bytes, machine_unit: str) -> tuple[list, list]:
    """Decode + parse a Fusion library blob → (parsed, skipped). CPU/GIL-bound,
    so callers run it via an executor (B2): json.loads is one C call that holds
    the GIL for its duration (fine for realistic KB–MB libraries — the 50 MB cap
    is only a DoS bound), while the per-tool transform is a Python loop that
    releases the GIL every few ms so the heartbeat keeps running. Raises
    ValueError on malformed input (the caller maps it to HTTP 400)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Invalid JSON: {e}")
    if not isinstance(data, dict) or "data" not in data or not isinstance(data["data"], list):
        raise ValueError("Not a Fusion 360 tool library (missing 'data' array)")
    return _parse_fusion_library(data, machine_unit)


@app.post("/import-tool-library", dependencies=[Depends(require_token)])
async def import_tool_library(file: UploadFile = File(...)):
    """Preview or apply a Fusion 360 tool library import.

    Query params:
      ?apply=true  — actually write to tool table + library (default: preview only)
      ?overwrite=true — overwrite existing tools (default: skip)
    """
    raw = await file.read(MAX_TOOL_LIBRARY_SIZE + 1)
    if len(raw) > MAX_TOOL_LIBRARY_SIZE:
        raise HTTPException(status_code=413, detail="Tool library too large (max 16 MB)")

    loop = asyncio.get_event_loop()
    machine_unit = get_ini_config().get("linear_units", "mm")  # on loop (cached; may STAT.poll once)
    try:
        parsed, skipped = await loop.run_in_executor(
            None, _decode_fusion_blob, raw, machine_unit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed and not skipped:
        raise HTTPException(status_code=400, detail="No tools found in library")

    # Count existing tools for warning (file read off the loop)
    tbl_path = get_tool_tbl_path()
    existing_count = 0
    if tbl_path:
        try:
            existing_count = len(await loop.run_in_executor(None, parse_tool_table, tbl_path))
        except Exception as e:
            _trace.emit_exc("tool_tbl.recount_failed", e, tbl_path=tbl_path)

    preview = [{**t} for t in parsed]
    skipped_preview = [{"T": t["T"], "description": t.get("description", ""),
                        "type": t.get("type", ""), "fusion_type": t.get("fusion_type", "")}
                       for t in skipped]

    return {"ok": True, "tools": preview, "total": len(parsed),
            "existing_count": existing_count,
            "skipped_duplicates": skipped_preview}


@app.post("/import-tool-library/apply", dependencies=[Depends(require_token)])
async def apply_tool_library_import(
    file: UploadFile = File(...),
):
    """Apply a Fusion 360 tool library import — replaces tool.tbl and tool_library.json."""
    raw = await file.read(MAX_TOOL_LIBRARY_SIZE + 1)
    if len(raw) > MAX_TOOL_LIBRARY_SIZE:
        raise HTTPException(status_code=413, detail="Tool library too large (max 16 MB)")

    loop = asyncio.get_event_loop()
    machine_unit = get_ini_config().get("linear_units", "mm")  # on loop (cached; may STAT.poll once)
    try:
        parsed, _skipped = await loop.run_in_executor(
            None, _decode_fusion_blob, raw, machine_unit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed:
        raise HTTPException(status_code=400, detail="No tools found")

    tbl_path = get_tool_tbl_path()
    if not tbl_path:
        raise HTTPException(status_code=500, detail="Tool table path not available")

    # Replace entire tool table and sidecar
    tbl_tools = []
    library: dict = {}

    for tool in parsed:
        t_num = tool["T"]
        z_init = tool.get("body_length") or tool.get("oal") or 0.0
        tbl_tools.append({
            "T": t_num,
            "P": t_num,
            "Z": float(z_init),
            "D": tool["D"],
            "remark": tool.get("description", ""),
        })

        key = str(t_num)
        library[key] = {}
        for field in _TOOL_META_FIELDS:
            val = tool.get(field)
            if val is not None:
                library[key][field] = val
        if tool.get("presets"):
            library[key]["presets"] = tool["presets"]

    # Serialize the whole replacement under _cmd_lock + off the event loop, so
    # it can't race the WS tool handlers or call into NML unlocked (issue #24).
    await _persist_imported_tools(tbl_path, tbl_tools, library)

    return {"ok": True, "added": len(parsed), "skipped": len(_skipped)}


@app.get("/files")
def list_files(subdir: str = ""):
    """List G-code files in the NC files directory."""
    nc_dir = get_nc_files_dir()
    browse_dir = os.path.join(nc_dir, subdir) if subdir else nc_dir

    if not validate_path_within(browse_dir, nc_dir):
        raise HTTPException(status_code=400, detail="Invalid directory")

    if not os.path.isdir(browse_dir):
        raise HTTPException(status_code=404, detail="Directory not found")

    entries = []
    try:
        for entry in sorted(os.scandir(browse_dir), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                entries.append({
                    "name": entry.name,
                    "type": "directory",
                    "path": os.path.relpath(entry.path, nc_dir),
                })
            elif entry.is_file():
                _, ext = os.path.splitext(entry.name)
                if ext.lower() in ALLOWED_EXTENSIONS:
                    stat = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "type": "file",
                        "path": entry.path,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"ok": True, "nc_dir": nc_dir, "subdir": subdir, "entries": entries}


# ---- Fan-out instrumentation ----
# Counts how many bulk-payload HTTP requests are in flight at any moment so
# [FANOUT] log lines correlate with [HB-STALL] / [LAG] trips. Uses a simple
# lock because sync def endpoints run in starlette's threadpool.
_fanout_lock = threading.Lock()
_fanout_inflight: Dict[str, int] = {"comp_grid": 0, "preview": 0, "gcode": 0, "surface_points": 0}


def _fanout_enter(kind: str) -> int:
    with _fanout_lock:
        _fanout_inflight[kind] += 1
        return _fanout_inflight[kind]


def _fanout_exit(kind: str) -> int:
    with _fanout_lock:
        _fanout_inflight[kind] -= 1
        return _fanout_inflight[kind]


@app.get("/gcode")
def get_gcode(path: str):
    """Stream a G-code file as plain text so the frontend doesn't need the
    bytes inline in the viewer_gcode WS frame. Path is validated against the
    NC dir allow-list (same as program_open). Uvicorn's file-sendfile path
    runs separately from the WS writer, so bulk reads don't stall heartbeats.
    """
    nc_dir = get_nc_files_dir()
    abs_path = os.path.abspath(path)
    if not validate_path_within(abs_path, nc_dir):
        raise HTTPException(status_code=403, detail="Path outside NC dir")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    _, ext = os.path.splitext(abs_path)
    if ext.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Invalid extension")
    t_start = time.monotonic()
    peak = _fanout_enter("gcode")
    try:
        return FileResponse(abs_path, media_type="text/plain")
    finally:
        _fanout_exit("gcode")
        handler_ms = (time.monotonic() - t_start) * 1000
        if peak > 1 or handler_ms > 50:
            print(
                f"[FANOUT] gcode peak={peak} handler_ms={handler_ms:.0f} "
                f"bytes={os.path.getsize(abs_path)}B file={os.path.basename(abs_path)}",
                flush=True,
            )


@app.get("/preview")
def get_preview(request: Request, v: Optional[int] = None):
    """Return the cached msgpack-encoded parsed G-code preview.

    The `v` query parameter is advisory — purely a cache-buster so the
    browser treats each version as a distinct URL. The server always returns
    the CURRENT cached preview (even if `v` is stale), because that's what
    the client wants anyway. Returns 404 if no file is loaded. If the client
    accepts gzip and a pre-compressed copy exists, serve that instead — both
    variants are produced once per parse, so per-fetch CPU stays at zero.
    """
    if _gcode_preview_bytes is None:
        raise HTTPException(status_code=404, detail="No preview cached")
    t_start = time.monotonic()
    peak = _fanout_enter("preview")
    try:
        accept_enc = request.headers.get("accept-encoding", "")
        use_gzip = "gzip" in accept_enc and _gcode_preview_bytes_gz is not None
        body = _gcode_preview_bytes_gz if use_gzip else _gcode_preview_bytes
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Preview-Version": str(_gcode_preview_version),
            "Vary": "Accept-Encoding",
        }
        if use_gzip:
            headers["Content-Encoding"] = "gzip"
        return Response(
            content=body,
            media_type="application/x-msgpack",
            headers=headers,
        )
    finally:
        _fanout_exit("preview")
        handler_ms = (time.monotonic() - t_start) * 1000
        if peak > 1 or handler_ms > 50:
            print(
                f"[FANOUT] preview peak={peak} handler_ms={handler_ms:.0f} "
                f"bytes={len(_gcode_preview_bytes)}B v={_gcode_preview_version}",
                flush=True,
            )


@app.get("/surface_points")
def get_surface_points(v: Optional[int] = None):
    """Return the cached msgpack-encoded surface-scan points.

    Served off the WS writer so the 10-80 KB payload × N clients doesn't
    stall the event loop past the HAL heartbeat window. `v` is advisory —
    a cache-buster so each version is a distinct URL.
    """
    if _surface_points_bytes is None:
        raise HTTPException(status_code=404, detail="No surface data")
    t_start = time.monotonic()
    peak = _fanout_enter("surface_points")
    try:
        return Response(
            content=_surface_points_bytes,
            media_type="application/x-msgpack",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Surface-Version": str(_surface_points_version),
            },
        )
    finally:
        _fanout_exit("surface_points")
        handler_ms = (time.monotonic() - t_start) * 1000
        if peak > 1 or handler_ms > 50:
            print(
                f"[FANOUT] surface_points peak={peak} handler_ms={handler_ms:.0f} "
                f"bytes={len(_surface_points_bytes)}B v={_surface_points_version}",
                flush=True,
            )


@app.get("/comp_grid")
def get_comp_grid(v: Optional[int] = None):
    """Return the cached msgpack-encoded compensation grid.

    Same pattern as /surface_points — keeps the up-to-~80 KB grid off the
    single-threaded WS writer.
    """
    if _comp_grid_bytes is None:
        raise HTTPException(status_code=404, detail="No comp grid")
    t_start = time.monotonic()
    peak = _fanout_enter("comp_grid")
    try:
        return Response(
            content=_comp_grid_bytes,
            media_type="application/x-msgpack",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Comp-Grid-Version": str(_comp_grid_version),
            },
        )
    finally:
        _fanout_exit("comp_grid")
        handler_ms = (time.monotonic() - t_start) * 1000
        if peak > 1 or handler_ms > 50:
            print(
                f"[FANOUT] comp_grid peak={peak} handler_ms={handler_ms:.0f} "
                f"bytes={len(_comp_grid_bytes)}B v={_comp_grid_version}",
                flush=True,
            )


# ---------- HAL viewer ----------

def _parse_hal_pins() -> list:
    """Parse `halcmd -s show pin` into list of dicts."""
    try:
        result = subprocess.run(
            ["halcmd", "-s", "show", "pin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            _trace.emit("halcmd.show_pin_failed", level="warn",
                        rc=result.returncode, stderr=result.stderr.strip())
            return []
    except Exception as e:
        _trace.emit("halcmd.show_pin_raised", level="warn",
                    exc=type(e).__name__, msg=str(e))
        return []
    pins = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        entry = {
            "comp": parts[0],
            "type": parts[1],
            "dir": parts[2],
            "value": parts[3],
            "name": parts[4],
        }
        if len(parts) >= 7 and parts[5] in ("<==", "==>"):
            entry["signal"] = parts[6]
            entry["arrow"] = parts[5]
        pins.append(entry)
    return pins


def _parse_hal_signals() -> list:
    """Parse `halcmd -s show sig` into list of dicts."""
    try:
        result = subprocess.run(
            ["halcmd", "-s", "show", "sig"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            _trace.emit("halcmd.show_sig_failed", level="warn",
                        rc=result.returncode, stderr=result.stderr.strip())
            return []
    except Exception as e:
        _trace.emit("halcmd.show_sig_raised", level="warn",
                    exc=type(e).__name__, msg=str(e))
        return []
    signals = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sig = {
            "type": parts[0],
            "value": parts[1],
            "name": parts[2],
            "pins": [],
        }
        i = 3
        while i < len(parts) - 1:
            if parts[i] in ("<==", "==>"):
                sig["pins"].append({"arrow": parts[i], "pin": parts[i + 1]})
                i += 2
            else:
                i += 1
        signals.append(sig)
    return signals


def _parse_hal_params() -> list:
    """Parse `halcmd -s show param` into list of dicts."""
    try:
        result = subprocess.run(
            ["halcmd", "-s", "show", "param"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            _trace.emit("halcmd.show_param_failed", level="warn",
                        rc=result.returncode, stderr=result.stderr.strip())
            return []
    except Exception as e:
        _trace.emit("halcmd.show_param_raised", level="warn",
                    exc=type(e).__name__, msg=str(e))
        return []
    params = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        params.append({
            "comp": parts[0],
            "type": parts[1],
            "dir": parts[2],
            "value": parts[3],
            "name": parts[4],
        })
    return params


async def _halshow_topology() -> dict:
    """Full HAL topology (pin/sig/param structure with links) via halcmd subprocess.

    Cached for the gateway lifetime (P5): the HAL graph is fixed at config load, so
    we build it ONCE (3 halcmd subprocesses) and reuse it for every subscriber —
    previously each new subscriber re-ran all three subprocesses. `_halshow_loop`
    processes subscribers sequentially, so the first cache-miss build completes
    before the next subscriber is served (no double-build). Force a rebuild via
    `_invalidate_halshow_topology()` (the `halshow_refresh` command), e.g. after an
    external HAL reload."""
    global _halshow_topology_cache
    if _halshow_topology_cache is not None:
        return _halshow_topology_cache
    loop = asyncio.get_event_loop()
    pins, signals, params = await asyncio.gather(
        loop.run_in_executor(None, _parse_hal_pins),
        loop.run_in_executor(None, _parse_hal_signals),
        loop.run_in_executor(None, _parse_hal_params),
    )
    _halshow_topology_cache = {"pins": pins, "signals": signals, "params": params}
    return _halshow_topology_cache


def _invalidate_halshow_topology() -> None:
    """Drop the cached HAL topology so the next subscriber rebuilds it (P5)."""
    global _halshow_topology_cache
    _halshow_topology_cache = None


async def _halshow_value_snapshot() -> dict:
    """Flat {'section/name': value-string} map via webui-reader's halshow_dump RPC.
    Reader runs hal.get_info_* in its own process (~1 ms typical, see hal-bench memory).
    Values arrive as halcmd-formatted strings so they merge cleanly with the topology snapshot."""
    out: Dict[str, str] = {}
    try:
        result = await _reader_request("halshow_dump", timeout=3.0)
        for name, value in result.get("pins", {}).items():
            out[f"pins/{name}"] = value
        for name, value in result.get("signals", {}).items():
            out[f"signals/{name}"] = value
        for name, value in result.get("params", {}).items():
            out[f"params/{name}"] = value
    except Exception as e:
        _trace.emit("halshow.value_snapshot_failed", level="warn",
                    exc=type(e).__name__, msg=str(e))
    return out


def _ensure_halshow_loop() -> None:
    """Start the live-update task if it isn't already running."""
    global _halshow_loop_task
    if _halshow_loop_task is None or _halshow_loop_task.done():
        _halshow_loop_task = register_bg_task(asyncio.create_task(_halshow_loop()))


async def _halshow_loop() -> None:
    """Push topology snapshot to new subscribers, then 5 Hz value deltas to all subscribers."""
    global _halshow_last_values
    try:
        while any(c.halshow_live for c in _clients.values()):
            # New subscribers: send full topology once
            for cid, c in list(_clients.items()):
                if not c.halshow_live:
                    continue
                if _halshow_topology_sent.get(cid):
                    continue
                ws = c.ws
                if ws is None:
                    continue
                topology = await _halshow_topology()
                try:
                    await ws_send_json(ws, {"type": "halshow_snapshot", **topology})
                    _halshow_topology_sent[cid] = True
                except Exception as e:
                    _trace.emit("halshow.snapshot_send_failed", level="warn",
                                client_id=cid, exc=type(e).__name__, msg=str(e))

            # Value diff for everyone with topology already in hand
            new_values = await _halshow_value_snapshot()
            if _halshow_last_values:
                delta = {k: v for k, v in new_values.items() if _halshow_last_values.get(k) != v}
                if delta:
                    msg: Dict[str, Any] = {"type": "halshow_update", "pins": {}, "signals": {}, "params": {}}
                    for k, v in delta.items():
                        section, name = k.split("/", 1)
                        msg[section][name] = v
                    for cid, c in list(_clients.items()):
                        if not c.halshow_live:
                            continue
                        if not _halshow_topology_sent.get(cid):
                            continue
                        ws = c.ws
                        if ws is None:
                            continue
                        try:
                            await ws_send_json(ws, msg)
                        except Exception as e:
                            _trace.emit_exc("ws.halshow_send_failed", e,
                                            client_id=cid)
            _halshow_last_values = new_values

            await asyncio.sleep(0.2)  # 5 Hz
    finally:
        # No subscribers (or task crashed) — drop baseline so the next subscribe starts clean
        _halshow_last_values = {}
        _halshow_topology_sent.clear()


def _read_g30_vars():
    """Read G30 tool change position (#5181-#5183) from the var file."""
    ini_path = getattr(STAT, "ini_filename", None)
    if not ini_path:
        return {"ok": False, "error": "No INI file"}
    ini = linuxcnc.ini(ini_path)
    var_file = ini.find("RS274NGC", "PARAMETER_FILE")
    if not var_file:
        return {"ok": False, "error": "No PARAMETER_FILE in INI"}
    if not os.path.isabs(var_file):
        var_file = os.path.join(os.path.dirname(ini_path), var_file)
    try:
        result = _read_var_file(var_file, {"5181", "5182", "5183"})
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "x": result.get("5181", 0.0), "y": result.get("5182", 0.0), "z": result.get("5183", 0.0)}


@app.get("/g30")
async def get_g30():
    """Return G30 tool change position (#5181-#5183)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_g30_vars)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    _conn_t0 = time.monotonic()  # === TEMP LIFECYCLE PROBE anchor for [CONN] deltas ===
    # Per-client init under the WS-init semaphore. Released at block
    # exit (before the recv loop) so live connections aren't capped by
    # WEBUI_WS_INIT_CONCURRENCY. With cached build_viewer_init() this
    # is almost always a no-op; the limit only matters during a real
    # overload (e.g. >20 tabs reconnecting in lockstep).
    async with _ws_init_sem:
        await ws.accept()
        # ── Auth + origin gate (issue #17) ── reject BEFORE registering the
        # client or starting any background loop. Browsers always send Origin
        # on a WS handshake, so a foreign Origin that fails the check is a
        # cross-origin hijack attempt; the token gates non-browser clients.
        if not _ws_origin_ok(ws.headers.get("origin"), ws.headers.get("host")):
            _trace.emit("ws.auth_rejected", level="warn", peer=_peer_of(ws), reason="origin")
            await ws.close(code=1008)
            return
        if not token_ok(ws.query_params.get("token"), WEBUI_TOKEN):
            _trace.emit("ws.auth_rejected", level="warn", peer=_peer_of(ws), reason="token")
            await ws.close(code=1008)
            return
        armed = False  # connection-local arming

        global _next_client_id, _estop_hold, _unacked_trip
        global _last_fault_latched, _trip_baseline_seen
        global _gcode_preview_pending, _gcode_preview_version
        global _gcode_last_file, _gcode_last_mtime
        global _gcode_preview_bytes, _gcode_preview_bytes_gz, _gcode_refresh_running
        client_id = _next_client_id
        _next_client_id += 1
        client_ip = ws.client.host if ws.client else "unknown"
        _clients[client_id] = ClientState(
            ip=client_ip,
            ws=ws,
            last_hb=time.time(),
        )
        await _cancel_disconnect_grace()
        _start_heartbeat()
        _start_status_poller()
        _start_reader_recv_loop()
        _trace.emit(
            "ws.connect.accept",
            client_id=client_id, peer=client_ip,
            accept_ms=round((time.monotonic() - _conn_t0) * 1000, 1),
        )

        # Restore lcnc_connected if LinuxCNC is still running but the flag was
        # cleared by a previous connection's WebSocket error
        global lcnc_connected
        _t = time.monotonic()
        _stat_path = "noop"
        if not lcnc_connected:
            if STAT is not None:
                try:
                    await asyncio.to_thread(STAT.poll)
                    lcnc_connected = True
                    _stat_path = "stat-poll"
                except Exception:
                    await asyncio.to_thread(try_connect_lcnc)
                    _stat_path = "stat-fail-reconnect"
            else:
                await asyncio.to_thread(try_connect_lcnc)
                _stat_path = "reconnect"
        _trace.emit(
            "ws.conn.lcnc_restored",
            client_id=client_id,
            dt_ms=round((time.monotonic() - _t) * 1000, 1),
            path=_stat_path,
            connected=lcnc_connected,
        )

        # Viewer: send static model/init once per connection
        host = ws.headers.get("host", "127.0.0.1:8000")  # includes port
        # Use the gateway's own port (8000) for STL assets rather than the
        # client-facing port.  In dev the client connects via Vite:5173 whose
        # HTTP/1.1 proxy pool is shared with JS-chunk downloads — STL fetches
        # stall behind them.  Gateway has CORS allow_origins=["*"] so a
        # cross-origin fetch from :5173 → :8000 works fine.
        host_only = host.split(":")[0]
        stl_base_url = f"http://{host_only}:8000/assets/"

        print(f"[VINIT] client#{client_id} connect-time viewer_init: lcnc_connected={lcnc_connected}, STAT={'OK' if STAT else 'None'}", flush=True)
        _t = time.monotonic()
        try:
            _set_phase(f"build_viewer_init client#{client_id}")
            await ws_send_json(ws, {"type": "viewer_init", "data": build_viewer_init(stl_base_url)})
            viewer_init_sent = True  # prevents status_loop re-send; reset on LinuxCNC reconnect
            _trace.emit(
                "ws.connect.viewer_init",
                client_id=client_id,
                viewer_init_ms=round((time.monotonic() - _t) * 1000, 1),
                since_accept_ms=round((time.monotonic() - _conn_t0) * 1000, 1),
            )
        except Exception as e:
            _trace.emit(
                "ws.connect.viewer_init", level="error",
                client_id=client_id, exc=type(e).__name__, msg=str(e),
            )

        # Send initial settings snapshot (part of WS handshake)
        _t = time.monotonic()
        try:
            _set_phase(f"load_settings client#{client_id}")
            _init_settings = await asyncio.get_event_loop().run_in_executor(None, load_settings)
            _set_phase(f"send_settings_init client#{client_id}")
            await ws_send_json(ws, {"type": "settings_init", "settings": _init_settings, "armed": armed})
            _trace.emit(
                "ws.connect.settings",
                client_id=client_id,
                settings_ms=round((time.monotonic() - _t) * 1000, 1),
            )
        except Exception as e:
            _trace.emit(
                "ws.connect.settings", level="error",
                client_id=client_id, exc=type(e).__name__, msg=str(e),
            )

        # Send initial G-code ping if a file is already loaded. The full preview
        # payload (feed/rapid polylines, stats) is served over HTTP via GET
        # /preview — we only broadcast a tiny "ready" notification over the WS
        # so each client can fetch the cached msgpack bytes out of band.
        # Cold path (file loaded but not yet parsed): schedule a refresh; the
        # client's status_loop picks up the ping on the next version bump.
        # File *content* is fetched separately via GET /gcode.
        _init_preview_ver = _gcode_preview_version
        _t = time.monotonic()
        _gcode_path = "no-file"
        try:
            if STAT is not None:
                STAT.poll()
            initial_file = safe_get("file", None)
            if initial_file:
                cache_hit = (
                    _gcode_preview_pending is not None
                    and _gcode_preview_pending.get("file") == initial_file
                    and _gcode_preview_bytes is not None
                )
                if cache_hit:
                    await ws_send_json(ws, {
                        "type": "viewer_gcode_ready",
                        "version": _gcode_preview_version,
                        "file": initial_file,
                    })
                    _gcode_path = "cache-hit-sent"
                elif not _gcode_refresh_running:
                    _gcode_refresh_running = True
                    register_bg_task(asyncio.create_task(_refresh_gcode_preview(initial_file)))
                    _gcode_path = "refresh-scheduled"
                else:
                    _gcode_path = "refresh-already-running"
        except Exception as e:
            print(f"Error loading initial G-code: {e}")
        _trace.emit(
            "ws.conn.gcode_ping_sent",
            client_id=client_id,
            dt_ms=round((time.monotonic() - _t) * 1000, 1),
            path=_gcode_path,
        )
        _trace.emit(
            "ws.conn.ready",
            client_id=client_id,
            total_ms=round((time.monotonic() - _conn_t0) * 1000, 1),
        )

        viewer_init_sent = False
        _probe_results: dict = {}  # populated from shared poller probe updates
        _prev_tc_req = False  # previous tool-change-requested state for edge detection
        _prev_tool_num = None  # previous tool_number for metadata edge detection


        async def status_loop():
            _set_phase(f"status_loop.entry client#{client_id}")
            nonlocal armed, viewer_init_sent, _probe_results, _prev_tc_req, _prev_tool_num
            global _tool_meta_dirty, _fb_scale, _spindle_load_pin
            loop = asyncio.get_event_loop()
            _last_settings_ver = _settings_store.version
            _last_gen = 0  # tracks which _status_gen we last processed
            _consec_fails = 0  # consecutive status_loop exceptions — bail after 10
            # Spindle feedback scale: 60 if pin outputs RPS (default), 1 if RPM
            _ss_init = await asyncio.to_thread(load_settings)  # file read off the loop (B3)
            _machine_s = _ss_init.get("machine", {})
            _fb_scale = 1 if _machine_s.get("spindleFeedbackUnit") == "rpm" else 60
            _slp = _machine_s.get("spindleLoadPin", "")
            _spindle_load_pin = _slp if isinstance(_slp, str) and _HAL_PIN_RE.match(_slp) else ""
            _prev_send_ms = 0.0  # send_ms from previous cycle (sent in next message)
            _prev_encode_ms = 0.0  # encode_ms from previous cycle (wire-format serialise time)
            _last_surface_version = 0  # tracks which _surface_points_version was last sent to this client
            _last_comp_grid_version = 0  # tracks which _comp_grid_version was last sent to this client
            _last_tool_table_version = 0  # tracks which _tool_table_version was last sent to this client
            _last_gcode_preview_version = _init_preview_ver  # initialized from connect-time snapshot
            # Experiment 2: status-delta per-client state
            _last_status_data: Optional[Dict[str, Any]] = None
            _cycles_since_full = 0
            while True:
                try:
                    # Not connected — disarm and send error to this client
                    if not lcnc_connected:
                        if viewer_init_sent:
                            viewer_init_sent = False
                        if armed:
                            armed = False
                            if client_id in _clients:
                                _clients[client_id].armed = False
                        try:
                            await ws_send_json(ws, {
                                "type": "status_error",
                                "error": "LinuxCNC not connected",
                                "clients": [{"ip": c.ip, "armed": c.armed} for c in _clients.values()],
                                "armed": armed,
                            })
                        except Exception:
                            break
                        await asyncio.sleep(2.0)
                        continue

                    # Wait for new data from shared poller. Awaiting the broadcast
                    # event replaces a 500Hz tight-poll — wakeups now match the
                    # actual poll rate (~30Hz). The 1s timeout is a safety net:
                    # if the poller stalls, we re-check and fall through to the
                    # "poller not running" branch above on the next iteration.
                    if _status_gen == _last_gen:
                        evt = _status_event
                        if evt is None:
                            # Poller hasn't emitted its first event yet.
                            await asyncio.sleep(0.05)
                            continue
                        try:
                            await asyncio.wait_for(evt.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass  # re-check on next iteration
                        continue
                    _last_gen = _status_gen
                    pickup_ts = time.monotonic()
                    # Phase-mark the per-client build (#35 fanout): with delta on,
                    # _use_shared is False so each client copies the shared dict
                    # (6005) + runs _diff_status_data (6066) + encodes its own
                    # frame — the suspected steady-state stall. If this phase
                    # dominates a lag.window, the fix is delta-off (shared-encode)
                    # or a cheaper diff. ws_send_measured re-phases for encode/send.
                    _set_phase("status_loop.build")

                    # Slow-consumer / hidden-tab skip: if a previous tick's
                    # send to this client is still in flight, OR the client's
                    # tab is backgrounded, skip building and sending status
                    # this tick. Bounds per-client outstanding work to one
                    # send and removes hidden tabs (the storm trigger) from
                    # the fan-out entirely. Loop continues to the next iter
                    # and re-waits on _status_event for the next tick.
                    _client_state = _clients.get(client_id)
                    if _client_state is not None and (
                        _client_state.send_pending or _client_state.hidden
                    ):
                        continue

                    st = _shared_status
                    if st is None:
                        await asyncio.sleep(0.5)
                        continue

                    # Send viewer_init on first successful poll for this client
                    if not viewer_init_sent:
                        print(f"[VINIT] client#{client_id} sending viewer_init (post-poll), STAT={'OK' if STAT else 'None'}", flush=True)
                        _t = time.monotonic()
                        try:
                            await ws_send_json(ws, {"type": "viewer_init", "data": build_viewer_init(stl_base_url)})
                            viewer_init_sent = True
                            _trace.emit(
                                "ws.conn.viewer_init_late",
                                client_id=client_id,
                                dt_ms=round((time.monotonic() - _t) * 1000, 1),
                                since_accept_ms=round((time.monotonic() - _conn_t0) * 1000, 1),
                            )
                        except Exception as e:
                            _trace.emit(
                                "ws.conn.viewer_init_late", level="error",
                                client_id=client_id, exc=type(e).__name__, msg=str(e),
                            )

                    # Merge shared probe updates into per-client results
                    if _shared_probe_updates:
                        _probe_results.update(_shared_probe_updates)

                    # Build status message. When the wire format is msgpack and no
                    # delta is active, splice the poller's pre-encoded bytes via
                    # msgspec.Raw instead of re-encoding an identical dict for each
                    # client. tool_meta now lives at top-level (not inside data),
                    # so tool-change ticks no longer mutate the shared dict and the
                    # shared-encode path stays engaged.
                    _tool_meta_tick = (st.tool_number != _prev_tool_num or _tool_meta_dirty)
                    _use_shared = (
                        _WIRE_FORMAT == "msgpack"
                        and not _STATUS_DELTA_ENABLED
                        and _shared_status_data_msgpack is not None
                    )
                    if _use_shared:
                        status_data: Any = _msgspec.Raw(_shared_status_data_msgpack)
                    else:
                        status_data = _shared_status_dict.copy() if _shared_status_dict else st.__dict__.copy()
                    status_msg: dict = {
                        "type": "status",
                        "data": status_data,
                        "errors": _shared_errors,
                        "clients": _shared_clients_list,
                        "armed": armed,
                    }
                    if _unacked_trip is not None:
                        status_msg["safety_trip"] = _unacked_trip
                    if _reader_is_stale():
                        status_msg["reader_stale"] = True
                    if _units_fallback_active or _config_warning_active:
                        status_msg["config_warning"] = {
                            "reason": _config_warning_reason or _units_fallback_reason,
                            "units": _units_fallback_active,
                        }
                    if _probe_results:
                        status_msg["probe_results"] = _probe_results

                    # Inject tool_meta on tool_number change or library edit (for
                    # 3D rendering). Lives at top level — sibling of `data` — so
                    # the shared-encode of `data` stays valid every tick.
                    if _tool_meta_tick:
                        _prev_tool_num = st.tool_number
                        _tool_meta_dirty = False
                        if st.tool_number is not None:
                            try:
                                # Hot path (per tool-number change × client): read
                                # off the loop. ToolLibraryStore is mtime-cached and
                                # now lock-guarded (B3), so this is safe against a
                                # concurrent WS tool-command write on another thread.
                                _lib = await asyncio.to_thread(load_tool_library)
                                _meta = _lib.get(str(st.tool_number), {})
                                if _meta:
                                    _tm = {k: _meta[k] for k in (
                                        "type", "oal", "flute_length", "shoulder_length",
                                        "shoulder_diameter", "body_length",
                                        "shaft_diameter", "taper_angle",
                                        "point_angle", "tip_diameter", "corner_radius",
                                        "holder_segments", "stl_file",
                                    ) if k in _meta}
                                    if _tm:
                                        status_msg["tool_meta"] = _tm
                            except (KeyError, TypeError, OSError) as e:
                                _trace.emit("status.tool_meta_failed", level="warn",
                                            tool=st.tool_number, exc=type(e).__name__, msg=str(e))

                    # Experiment 2: delta encoding of the `data` field.
                    # After all mutations to status_msg["data"] are done, compare
                    # against this client's last-sent baseline and swap the full
                    # dict for a diff when delta mode is on. On a forced-full
                    # cadence (every _DELTA_FULL_INTERVAL cycles) or first send,
                    # stay with the full payload. `errors`, `clients`, `timing`,
                    # `surface_points`, etc. always go through as-is — they're
                    # already sparse or cheap.
                    if _STATUS_DELTA_ENABLED:
                        if _last_status_data is None or _cycles_since_full >= _DELTA_FULL_INTERVAL:
                            _cycles_since_full = 0
                        else:
                            status_msg["type"] = "status_delta"
                            status_msg["data"] = _diff_status_data(_last_status_data, status_msg["data"])
                            _cycles_since_full += 1
                        _last_status_data = status_data

                    # Timing: only on first status after heartbeat so all
                    # components share the same ~1Hz sample rate.
                    # Two exact sums:
                    #   RT = Network + Server  (client-side, by construction)
                    #   Cycle = Poll + Errors + Parse + Overhead  (server-side)
                    hb_mono = _clients[client_id].hb_mono if client_id in _clients else 0.0
                    if hb_mono > 0:
                        status_msg["timing"] = {
                            "server_ms": round((time.monotonic() - hb_mono) * 1000, 2),
                            "cycle_ms": _shared_timing.get("cycle_ms", 0),
                            "poll_ms": _shared_timing.get("poll_ms", 0),
                            "errors_ms": _shared_timing.get("errors_ms", 0),
                            "parse_ms": _shared_timing.get("parse_ms", 0),
                            "overhead_ms": _shared_timing.get("overhead_ms", 0),
                            # shared_encode_ms: cost of the poller's one-per-tick
                            # msgpack pre-encode that each client's envelope
                            # splices via msgspec.Raw. Keeps the Debug tab honest
                            # — per-client encode_ms drops to envelope-only when
                            # shared-encode is active, masking the shared cost.
                            "shared_encode_ms": _shared_timing.get("shared_encode_ms", 0),
                            # Prior-cycle encode time (status_msg built before the
                            # encode happens → we attach the last known value).
                            # ws_bytes is measured client-side from the received frame.
                            "encode_ms": _prev_encode_ms,
                        }
                        if client_id in _clients:
                            _clients[client_id].hb_mono = 0.0

                    pre_send = time.monotonic()
                    # Mark the client as having an in-flight send. Cleared in
                    # finally so even an exception path doesn't leave the flag
                    # stuck (which would silently mute future fan-out for this
                    # client). The skip check at the top of the loop reads this
                    # flag to bound outstanding work to one send per peer.
                    if client_id in _clients:
                        _clients[client_id].send_pending = True
                    try:
                        _prev_encode_ms, _bytes_sent = await ws_send_measured(ws, status_msg)
                    finally:
                        if client_id in _clients:
                            _clients[client_id].send_pending = False
                    _prev_send_ms = round((time.monotonic() - pre_send) * 1000, 2)

                    # Contribute to per-tick aggregate stats (logged by poller on
                    # next cycle). Only record for the current generation to avoid
                    # double-counting if a client is slow and rolls into next tick.
                    if _status_tick_stats["gen"] == _last_gen:
                        _status_tick_stats["done"] += 1
                        _status_tick_stats["encode_sum"] += _prev_encode_ms
                        _status_tick_stats["send_sum"] += _prev_send_ms
                        if _prev_send_ms > _status_tick_stats["send_max"]:
                            _status_tick_stats["send_max"] = _prev_send_ms
                        # === TEMP STATUS-PAYLOAD PROBE === wire-size accounting.
                        _status_tick_stats["bytes_sum"] += _bytes_sent
                        if _bytes_sent > _status_tick_stats["bytes_max"]:
                            _status_tick_stats["bytes_max"] = _bytes_sent
                        if "tool_meta" in status_msg:
                            _status_tick_stats["tool_meta_count"] += 1

                    # Log timing to file if enabled
                    if _timing_log_enabled and "timing" in status_msg:
                        _log_timing({**status_msg["timing"], "send_ms": _prev_send_ms})

                    # Settings broadcast: send full settings when version changes
                    if _last_settings_ver != _settings_store.version:
                        _last_settings_ver = _settings_store.version
                        try:
                            _ss = await loop.run_in_executor(None, load_settings)
                            _machine_s = _ss.get("machine", {})
                            _fb_scale = 1 if _machine_s.get("spindleFeedbackUnit") == "rpm" else 60
                            _slp = _machine_s.get("spindleLoadPin", "")
                            _new_load_pin = _slp if isinstance(_slp, str) and _HAL_PIN_RE.match(_slp) else ""
                            if _new_load_pin != _spindle_load_pin:
                                _spindle_load_pin = _new_load_pin
                                asyncio.create_task(_reader_configure_extra_pins())
                            await ws_send_json(ws, {
                                "type": "settings_changed",
                                "settings": _ss,
                                "armed": armed,
                            })
                        except (OSError, json.JSONDecodeError, ValueError) as e:
                            _trace.emit("status.settings_changed_broadcast_failed", level="warn",
                                        exc=type(e).__name__, msg=str(e))

                    # Tool change: auto-deassert when request clears
                    if _prev_tc_req and not st.tool_change_requested:
                        _hal_send({"tool_changed": False})
                    _prev_tc_req = st.tool_change_requested

                    # Viewer: gcode preview — send a tiny "ready" ping so each
                    # client fetches the cached msgpack bytes via GET /preview.
                    # Broadcasting the full (multi-MB) frame to every client on
                    # the single-threaded WS writer stalled the event loop past
                    # the HAL heartbeat window. File content is fetched via
                    # GET /gcode; preview data is fetched via GET /preview.
                    if _gcode_preview_version != _last_gcode_preview_version:
                        _last_gcode_preview_version = _gcode_preview_version
                        pending = _gcode_preview_pending
                        if _gcode_preview_bytes is not None and pending is not None:
                            try:
                                await ws_send_json(ws, {
                                    "type": "viewer_gcode_ready",
                                    "version": _gcode_preview_version,
                                    "file": pending.get("file"),
                                })
                            except RuntimeError:
                                pass  # safe-silent: WS closed between iteration start and send
                        else:
                            await ws_send_json(ws, {
                                "type": "viewer_gcode",
                                "data": {"file": None, "feed": [], "feed_lines": [], "rapid": []},
                            })

                    # Surface points: cached msgpack lives on the server; send a
                    # tiny version ping so each client fetches via GET /surface_points
                    # off the WS writer.
                    if _surface_points_version != _last_surface_version:
                        _last_surface_version = _surface_points_version
                        if _surface_points_bytes is not None:
                            try:
                                await ws_send_json(ws, {
                                    "type": "surface_points_ready",
                                    "version": _surface_points_version,
                                })
                            except RuntimeError:
                                pass  # safe-silent: WS closed between iteration start and send

                    # Compensation grid: same pattern — fetch via GET /comp_grid.
                    if _comp_grid_version != _last_comp_grid_version:
                        _last_comp_grid_version = _comp_grid_version
                        if _comp_grid_bytes is not None:
                            try:
                                await ws_send_json(ws, {
                                    "type": "comp_grid_ready",
                                    "version": _comp_grid_version,
                                })
                            except RuntimeError:
                                pass  # safe-silent: WS closed between iteration start and send

                    # Tool table: ping clients to refetch via WS RPC `get_tool_table`.
                    # Bumped by _reload_tool_table_and_bump() after every save/add/
                    # delete/import. Initial connect resyncs because _last_*=0.
                    if _tool_table_version != _last_tool_table_version:
                        _last_tool_table_version = _tool_table_version
                        try:
                            await ws_send_json(ws, {
                                "type": "tool_table_changed",
                                "version": _tool_table_version,
                            })
                        except RuntimeError:
                            pass  # safe-silent: WS closed between iteration start and send

                    # Heartbeat timeout — per-client liveness signal.
                    #
                    # Per the architecture cleanup: a client-side heartbeat
                    # stall is NOT a safety event. It is a UI event. The HAL
                    # chain (oneshot.0 + trip-latch) is the real safety, and
                    # it independently trips on gateway hang or all-clients-out.
                    # Stalling on one armed client while others (or even just
                    # viewers) keep the HAL chain alive must NOT abort a
                    # running program. So: disarm the client + jog-stop any
                    # in-flight jog from this client. No program abort.
                    if client_id in _clients:
                        if time.time() - _clients[client_id].last_hb > 3.0:
                            if armed:
                                armed = False
                                _clients[client_id].armed = False
                                try:
                                    async with _get_cmd_lock():
                                        await _jog_stop_for_client()
                                except Exception as _e:
                                    _trace.emit(
                                        "safety.hb_stall_jog_stop_failed", level="error",
                                        client_id=client_id, exc=type(_e).__name__, err=str(_e),
                                    )
                                # Unhealthy client \u2014 drop any armed-resume hold so a
                                # reconnect can't silently restore armed; the
                                # operator must explicitly re-arm (safety clarity).
                                _consume_armed_resume_hold(_clients[client_id].session_id)
                                _trace.emit(
                                    "safety.hb_stall_disarmed",
                                    client_id=client_id,
                                )
                                try:
                                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "Heartbeat timeout \u2014 disarmed for safety", "armed": False})
                                except Exception as _e:
                                    # Server-side disarm already happened; the
                                    # notification just didn't reach the client.
                                    # UI may show stale armed state until the
                                    # next status frame. Logged so the missed
                                    # signal is auditable rather than silently
                                    # lost.
                                    _trace.emit(
                                        "safety.disarm_notify_failed", level="warn",
                                        client_id=client_id, exc=type(_e).__name__, err=str(_e),
                                    )
                            else:
                                # Non-armed client stalled — evict to keep _clients accurate
                                # Closing WS triggers finally block → removes from _clients → updates HAL pins
                                try:
                                    await ws.close(code=1000, reason="Heartbeat timeout")
                                except Exception as _e:
                                    # Close failure is recoverable (finally block
                                    # still runs cleanup) but worth surfacing —
                                    # repeated occurrences indicate the WS layer
                                    # is unhealthy.
                                    _trace.emit(
                                        "ws.close_failed", level="warn",
                                        client_id=client_id, reason="hb_stall_nonarmed",
                                        exc=type(_e).__name__, err=str(_e),
                                    )
                                return  # exit status_loop; finally block handles cleanup

                    # Full iteration completed without exception — reset failure counter.
                    # (Healthy early-`continue` paths above are neutral: they don't
                    # reset but also don't increment, so the counter only grows when
                    # exceptions truly occur.)
                    _consec_fails = 0

                except Exception as e:
                    _set_phase(f"status_loop.exception client#{client_id} type={type(e).__name__}")
                    _consec_fails += 1
                    _trace.emit("status.loop_exception", level="warn",
                                client_id=client_id, fails=_consec_fails, max_fails=10,
                                exc=type(e).__name__, msg=str(e))
                    if _consec_fails >= 10:
                        _trace.emit("status.loop_abort", level="error",
                                    client_id=client_id,
                                    msg="aborting status loop after 10 consecutive failures")
                        break
                    await asyncio.sleep(0.5)
            _set_phase(f"status_loop.exit client#{client_id}")

        status_task = register_bg_task(asyncio.create_task(status_loop()))

    _disc_reason = "unknown"
    # Captured on `cmd:"hello"`; the finally block uses this to register
    # the armed-resume hold so the next reconnect of the same tab can
    # silently inherit armed. Stays None for old clients that don't send
    # hello (they lose armed on reconnect, same as today).
    _disc_session_id: Optional[str] = None
    try:
        while True:
            _set_phase(f"ws.receive_text client#{client_id}")
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws_send_json(ws, {"type": "reply", "ok": False, "error": "Invalid JSON"})
                continue
            _set_phase(f"handle_msg cmd={msg.get('cmd', '?')} client#{client_id}")

            if msg.get("cmd") == "heartbeat":
                if client_id in _clients:
                    _clients[client_id].last_hb = time.time()
                    _clients[client_id].hb_mono = time.monotonic()
                await ws_send_json(ws, {"type": "pong"})
                continue

            if msg.get("cmd") == "hello":
                # Tab handshake: captures session_id for armed-resume across
                # brief reconnects (Ctrl-R, Wi-Fi blip, screen-lock close).
                # Decision tree, each branch traced — no silent fallbacks.
                #
                # Phase 2 / E4: peek first so denial events distinguish
                # "no hold ever existed" from "hold expired" from "hold
                # would have granted but a safety trip is pending". The
                # previous ordering (unacked-trip-before-peek) emitted
                # `resume_denied_unacked_trip` for fresh tabs that never
                # had a hold — misleading.
                _sid = msg.get("session")
                if isinstance(_sid, str) and _sid:
                    _disc_session_id = _sid
                    if client_id in _clients:
                        _clients[client_id].session_id = _sid
                want_resume = bool(msg.get("resume_armed", False))
                if want_resume:
                    if not _sid:
                        _trace.emit(
                            "session.hello_missing",
                            client_id=client_id, reason="resume_requested_without_session_id",
                        )
                    else:
                        _hold_state = _peek_armed_resume_hold(_sid)
                        if _hold_state == "no_match":
                            _trace.emit(
                                "session.resume_denied_no_match",
                                client_id=client_id, session_id=_sid,
                            )
                        elif _hold_state == "expired":
                            # Consume the stale hold so it doesn't linger.
                            _consume_armed_resume_hold(_sid)
                            _trace.emit(
                                "session.resume_denied_expired",
                                client_id=client_id, session_id=_sid,
                            )
                        elif _hold_state == "granted":
                            # Within grace. Trip check applies here, not earlier:
                            # an unacked trip blocks a real resume, but it should
                            # not poison the trace for a fresh tab with no hold.
                            if _unacked_trip is not None:
                                # Don't consume — once the operator acks the
                                # trip and reconnects again (still within
                                # original window), they should still resume.
                                # Note: in practice the grace window is short
                                # so this is unlikely to matter; the choice
                                # here favours operator-friendly behaviour.
                                _trace.emit(
                                    "session.resume_denied_unacked_trip",
                                    client_id=client_id, session_id=_sid,
                                )
                            else:
                                _consume_armed_resume_hold(_sid)
                                armed = True
                                if client_id in _clients:
                                    _clients[client_id].armed = True
                                    _clients[client_id].last_hb = time.time()
                                _trace.emit(
                                    "session.resume_granted",
                                    client_id=client_id, session_id=_sid,
                                )
                await ws_send_json(ws, {"type": "reply", "ok": True, "armed": armed})
                continue

            if msg.get("cmd") == "arm":
                want_armed = bool(msg.get("armed", False))
                # Re-arm gate: operator must acknowledge a sticky safety trip
                # before the machine can come back up. Disarming is always
                # allowed.
                if want_armed and _unacked_trip is not None:
                    await ws_send_json(ws, {
                        "type": "reply",
                        "ok": False,
                        "error": "Safety trip not acknowledged",
                    })
                    continue
                _was_armed = armed
                armed = want_armed
                if client_id in _clients:
                    _clients[client_id].armed = armed
                    _clients[client_id].last_hb = time.time()  # reset on arm change
                # Symmetry with auto-disarm paths (Phase 2 / E1.2 + E2):
                # explicit disarm must jog-stop any in-flight jog from this
                # client AND register an armed-resume hold (so a deliberate
                # disarm-then-Ctrl-R can still restore armed state). Closes
                # the released-jog-button hazard and matches the "all paths
                # to disarmed do the same thing" principle.
                if _was_armed and not armed:
                    if CMD is not None and not _shutting_down:
                        try:
                            async with _get_cmd_lock():
                                await _jog_stop_for_client()
                        except Exception as _e:
                            _trace.emit(
                                "safety.explicit_disarm_jog_stop_failed", level="error",
                                client_id=client_id, exc=type(_e).__name__, err=str(_e),
                            )
                    _register_armed_resume_hold(_disc_session_id, client_id)
                    _trace.emit(
                        "safety.explicit_disarmed",
                        client_id=client_id,
                    )
                elif not _was_armed and armed:
                    _trace.emit(
                        "safety.explicit_armed",
                        client_id=client_id,
                    )
                await ws_send_json(ws, {"type": "reply", "ok": True, "armed": armed})
                continue

            if msg.get("cmd") == "safety_trip_ack":
                # Clearing the banner is independent of clearing the latch: the
                # HAL latch stays set (fault-out TRUE) until the operator's
                # E-Stop Reset. Because evaluate_trip_latch only re-banners on a
                # clean FALSE→TRUE transition, the still-TRUE level after this ack
                # produces no edge and the ack sticks; a genuinely new trip
                # (latch reset → FALSE, then TRUE again) still fires.
                _unacked_trip = None
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            if msg.get("cmd") == "get_settings":
                _loop = asyncio.get_event_loop()
                _ss = await _loop.run_in_executor(None, load_settings)
                await ws_send_json(ws, {"type": "settings_init", "settings": _ss, "armed": armed})
                continue

            if msg.get("cmd") == "save_settings":
                section = msg.get("section", "")
                if section not in _VALID_SETTINGS_SECTIONS:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": f"Unknown settings section: {section}"})
                    continue
                _loop = asyncio.get_event_loop()
                try:
                    await _loop.run_in_executor(None, save_settings_section, section, msg.get("data"))
                except Exception as _se:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": f"{type(_se).__name__}: {_se}"})
                    continue
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            if msg.get("cmd") == "client_diag":
                # Periodic browser-side diagnostics (heap, Three.js counters,
                # connection state). Forwarded straight to the trace bus so a
                # renderer crash ("Aw Snap") still leaves a usable timeline in
                # trace.ndjson — the gateway file outlives the browser.
                data = msg.get("data")
                if isinstance(data, dict):
                    _trace.emit("client.diag", client_id=client_id, **data)
                # Fire-and-forget — no reply, no ack, to keep this off the hot path.
                continue

            if msg.get("cmd") == "timing_log":
                global _timing_log_enabled, _timing_log_path
                _timing_log_enabled = bool(msg.get("enable", False))
                if _timing_log_enabled:
                    from datetime import datetime
                    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    base = _trace.log_dir()
                    if not base:
                        _trace.emit("timing.log_no_log_dir", level="error")
                        _timing_log_enabled = False
                        await ws_send_json(ws, {"type": "reply", "ok": False, "enabled": False})
                        continue
                    timing_dir = os.path.join(base, "timing")
                    os.makedirs(timing_dir, exist_ok=True)
                    _timing_log_path = os.path.join(timing_dir, f"timing-{stamp}.jsonl")
                    _trace.emit("timing.log_started", path=_timing_log_path)
                else:
                    if _timing_log_path:
                        _trace.emit("timing.log_stopped", path=_timing_log_path)
                await ws_send_json(ws, {"type": "reply", "ok": True, "enabled": _timing_log_enabled})
                continue

            if msg.get("cmd") == "halshow_live":
                on = bool(msg.get("on", False))
                if client_id in _clients:
                    _clients[client_id].halshow_live = on
                if not on:
                    _halshow_topology_sent.pop(client_id, None)
                else:
                    # Drop any stale flag so subscriber gets a fresh snapshot
                    _halshow_topology_sent.pop(client_id, None)
                    _ensure_halshow_loop()
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            if msg.get("cmd") == "halshow_refresh":
                # Force a topology rebuild (P5) — e.g. after an external HAL reload.
                # Clears every subscriber's sent-flag so the rebuilt graph re-sends.
                _invalidate_halshow_topology()
                _halshow_topology_sent.clear()
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            if msg.get("cmd") == "tab_visibility":
                # Frontend reports document.visibilityState. While hidden, the
                # browser stops draining the WS and the kernel TCP buffer fills,
                # which is the storm trigger we measured. Status fan-out skips
                # this client while hidden; resumes on the next tick after the
                # tab returns to visible. State-only update — does not require
                # `armed` and never affects machine state.
                #
                # NO REPLY: the very client sending this command is, by
                # definition, the one whose tab just became hidden — i.e. the
                # one whose WS is about to back up. Awaiting `ws_send_json`
                # for a reply would re-introduce the slow-consumer hang we're
                # trying to escape (a 495 ms stall observed in the first storm
                # test with the reply present). Frontend fire-and-forgets.
                hidden = bool(msg.get("hidden", False))
                if client_id in _clients:
                    _clients[client_id].hidden = hidden
                _trace.emit(
                    "ws.tab_visibility",
                    client_id=client_id, hidden=hidden,
                )
                continue

            if msg.get("cmd") == "simulate_probe_trip":
                if not armed:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "Not armed"})
                    continue
                if not lcnc_connected:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "LinuxCNC not connected"})
                    continue
                try:
                    _loop = asyncio.get_event_loop()
                    # Unlink any existing writer on probe-in (e.g. qtpyvcp.probe-in.out)
                    # so halcmd sets works. The pin may not exist in non-qtpyvcp
                    # configs — that's fine. Log other failures (halcmd missing,
                    # permission denied, syntax error) so they don't disappear.
                    _unlink_res = await _loop.run_in_executor(None, lambda: subprocess.run(
                        ['halcmd', 'unlinkp', 'qtpyvcp.probe-in.out'],
                        capture_output=True, text=True, timeout=2))
                    if _unlink_res.returncode != 0:
                        _err = (_unlink_res.stderr or "").strip()
                        if _err and "does not exist" not in _err and "no such" not in _err.lower():
                            _trace.emit("halcmd.unlinkp_failed", level="warn",
                                        pin="qtpyvcp.probe-in.out",
                                        rc=_unlink_res.returncode, stderr=_err)
                    await _loop.run_in_executor(None, lambda: subprocess.run(
                        ['halcmd', 'sets', 'probe-in', '1'],
                        capture_output=True, text=True, timeout=2, check=True))
                    await asyncio.sleep(0.02)
                    await _loop.run_in_executor(None, lambda: subprocess.run(
                        ['halcmd', 'sets', 'probe-in', '0'],
                        capture_output=True, text=True, timeout=2, check=True))
                    await ws_send_json(ws, {"type": "reply", "ok": True})
                except Exception as e:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": f"simulate_probe_trip: {e}"})
                continue

            if msg.get("cmd") == "confirm_tool_change":
                if not armed:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "Not armed"})
                    continue
                if not lcnc_connected:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "LinuxCNC not connected"})
                    continue
                _loop = asyncio.get_event_loop()
                await _loop.run_in_executor(None, _hal_send, {"tool_changed": True})
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            if msg.get("cmd") == "set_compensation":
                if not armed:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "Not armed"})
                    continue
                enable = bool(msg.get("enable", False))
                _loop = asyncio.get_event_loop()
                await _loop.run_in_executor(None, _hal_send, {"compensation_enable": enable})
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            if msg.get("cmd") == "set_compensation_method":
                if not armed:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": "Not armed"})
                    continue
                # Validated inline: this handler runs BEFORE the handle_command
                # dispatch boundary, so a bad cast here would escape to the
                # WebSocketDisconnect/RuntimeError catch and tear the socket
                # down instead of returning a bounded error (issue #27).
                try:
                    method = finite_int(msg.get("method", 2))
                except (ValueError, TypeError) as _e:
                    await ws_send_json(ws, {"type": "reply", "ok": False, "error": f"Invalid method: {_e}"})
                    continue
                _loop = asyncio.get_event_loop()
                await _loop.run_in_executor(None, _hal_send, {"compensation_method": method})
                await ws_send_json(ws, {"type": "reply", "ok": True})
                continue

            _set_phase(f"handle_command cmd={msg.get('cmd', '?')} client#{client_id}")
            try:
                reply = await handle_command(msg, armed)
            except (ValueError, TypeError, KeyError, PermissionError, OverflowError) as _val_e:
                # Malformed payload or failed precondition (bad numeric cast,
                # missing field, not-armed). Return a bounded structured error
                # rather than letting it bubble out of the receive loop — an
                # uncaught exception here would tear down the socket and trip
                # the armed-disconnect side effects in `finally` (issue #27).
                _trace.emit("ws.command_invalid", level="warn",
                            client_id=client_id, cmd=msg.get("cmd"),
                            exc=type(_val_e).__name__, msg=str(_val_e))
                reply = {"ok": False, "error": f"{type(_val_e).__name__}: {_val_e}"}
            else:
                if msg.get("cmd") == "unload_file" and reply.get("ok"):
                    # reset_interpreter doesn't clear stat.file, so the shared
                    # poller's file-change edge won't fire. Clear the shared
                    # cache and bump the version so every client's status_loop
                    # sends an empty viewer_gcode on the next cycle.
                    _gcode_preview_pending = None
                    _gcode_preview_bytes = None
                    _gcode_preview_bytes_gz = None
                    _gcode_preview_version += 1
                    _gcode_last_file = None
                    _gcode_last_mtime = None
            await ws_send_json(ws, {"type": "reply", "cmd": msg.get("cmd"), **reply})

    except (WebSocketDisconnect, RuntimeError) as _disc_e:
        _set_phase(f"ws_endpoint.WebSocketDisconnect_caught client#{client_id}")
        _disc_reason = type(_disc_e).__name__
    finally:
        _finally_t0 = time.monotonic()
        _set_phase(f"ws_endpoint.finally.entry client#{client_id}")
        _clients.pop(client_id, None)
        _halshow_topology_sent.pop(client_id, None)
        # Disconnect of an armed client: jog-stop any in-flight jog this
        # client started, then register a 10s armed-resume hold keyed by
        # session_id so a Ctrl-R / Wi-Fi blip can transparently re-arm.
        #
        # We do NOT abort a running program here. Per the architecture
        # cleanup: client liveness is not safety. The HAL chain (oneshot.0
        # + trip-latch) is the real safety, and it independently trips on
        # gateway hang or all-clients-out (via _start_disconnect_grace).
        # A single armed client dropping while viewers (or even nothing)
        # remain must not abort the program — if everyone is gone, HAL
        # grace expires and the trip-latch handles it for real.
        #
        # During global lifespan shutdown the gateway has already broadcast
        # server_shutdown and closed sockets; skip the jog-stop in that
        # window so N clients don't all serialize on _cmd_lock during a
        # LinuxCNC teardown.
        if armed and CMD is not None and not _shutting_down:
            _set_phase(f"ws_endpoint.finally.armed_jog_stop client#{client_id}")
            try:
                async with _get_cmd_lock():
                    _set_phase(f"ws_endpoint.finally.armed_jog_stop.cmd_lock_held client#{client_id}")
                    STAT.poll()
                    await _jog_stop_for_client()
            except Exception as _e:
                _trace.emit(
                    "safety.disconnect_jog_stop_failed", level="error",
                    client_id=client_id, exc=type(_e).__name__, err=str(_e),
                )
            # Only arm-resume a client that was beating normally right up to the
            # drop (clean Ctrl-R / wifi blip). A client whose heartbeat was already
            # lagging was struggling/overloaded — don't silently auto-restore armed
            # on reconnect; the operator must explicitly re-arm (safety clarity).
            _hb_age = (time.time() - _clients[client_id].last_hb) if client_id in _clients else 1e9
            if _hb_age <= _RESUME_MAX_HB_AGE:
                _register_armed_resume_hold(_disc_session_id, client_id)
                _trace.emit("safety.disconnect_disarmed", client_id=client_id)
            else:
                _trace.emit(
                    "safety.disconnect_disarmed_no_resume", level="warn",
                    client_id=client_id, hb_age_ms=round(_hb_age * 1000),
                )
        _set_phase(f"ws_endpoint.finally.cancel_status_task client#{client_id}")
        status_task.cancel()
        # Clear estop hold if no clients remain
        if not _clients:
            _estop_hold = False
        # Update HAL pins to reflect this client is gone
        has_clients = bool(_clients)
        if has_clients:
            _set_phase(f"ws_endpoint.finally.hal_send_disconnected client#{client_id}")
            _hal_send({"connected": True, "heartbeat": False})
        else:
            # Grace period: delay dropping connected pin to allow page refresh
            _set_phase(f"ws_endpoint.finally.start_disconnect_grace client#{client_id}")
            _start_disconnect_grace()
        _set_phase(f"ws_endpoint.finally.done client#{client_id}")
        _trace.emit(
            "ws.disconnect",
            client_id=client_id, peer=client_ip,
            reason=_disc_reason,
            cleanup_ms=round((time.monotonic() - _finally_t0) * 1000, 1),
            session_ms=round((time.monotonic() - _conn_t0) * 1000, 1),
            remaining_clients=len(_clients),
        )


# ── Camera streaming (optional — requires LCNC_CAMERA_SOURCE env var) ────
try:
    import cv2
except ImportError:
    cv2 = None  # type: ignore[assignment]

_camera: Any = None  # cv2.VideoCapture or None
_camera_lock = threading.Lock()

def _camera_init() -> bool:
    """Lazy-init camera from env var. Returns True if available."""
    global _camera
    if cv2 is None:
        return False
    source = os.environ.get("LCNC_CAMERA_SOURCE", "")
    if not source:
        return False
    # Hold _camera_lock across the whole init: two concurrent /camera requests
    # are each dispatched to the threadpool, so without the lock both could
    # construct a VideoCapture and leak one device handle. Double-check inside.
    with _camera_lock:
        if _camera is not None:
            return _camera.isOpened()
        try:
            src: Any = int(source)
        except ValueError:
            src = source
        _camera = cv2.VideoCapture(src)
        res = os.environ.get("LCNC_CAMERA_RESOLUTION", "1280x720")
        try:
            w, h = (int(x) for x in res.split("x"))
            _camera.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            _camera.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        except ValueError:
            pass  # safe-silent: malformed resolution string, camera keeps default
        return _camera.isOpened()

def _camera_grab_jpeg(quality: int = 80) -> Optional[bytes]:
    """Grab one frame, return JPEG bytes or None."""
    with _camera_lock:
        if not _camera or not _camera.isOpened():
            return None
        ok, frame = _camera.read()
        if not ok:
            return None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes()

def _camera_release():
    global _camera
    with _camera_lock:
        if _camera is not None:
            _camera.release()
            _camera = None


class _CameraBroker:
    """Single-producer camera fan-out (P3).

    Previously every `/camera/stream` viewer ran its own capture+JPEG-encode loop
    (`_camera_grab_jpeg` per frame), so N viewers paid N× the read+`imencode` cost
    (serialized behind `_camera_lock`, so extra viewers added queued duplicate work
    rather than throughput). Here one producer task captures+encodes at `CAMERA_FPS`
    into a shared latest JPEG + a monotonic sequence; consumers await the next
    sequence and stream the *same* bytes — newest-wins, stale frames dropped. The
    device opens on the first subscriber and releases a short grace after the last.
    All state is touched only on the event loop (single-threaded); the blocking cv2
    calls stay off-loop via `to_thread` (and `_camera_lock`)."""

    _GRACE_S = 2.0

    def __init__(self) -> None:
        self._subscribers = 0
        self._task: Optional[asyncio.Task] = None
        self._cond = asyncio.Condition()
        self._latest: Optional[bytes] = None
        self._seq = 0
        self._stop_handle: Optional[asyncio.TimerHandle] = None

    async def subscribe(self) -> bool:
        """Register a viewer; start the producer on the first. False if no camera."""
        if self._stop_handle is not None:          # a viewer arrived within the grace
            self._stop_handle.cancel()
            self._stop_handle = None
        self._subscribers += 1
        if self._task is None:
            ok = await asyncio.to_thread(_camera_init)
            if not ok:
                self._subscribers -= 1
                return False
            self._task = register_bg_task(asyncio.create_task(self._produce()))
        return True

    def unsubscribe(self) -> None:
        self._subscribers = max(0, self._subscribers - 1)
        if self._subscribers == 0 and self._task is not None and self._stop_handle is None:
            loop = asyncio.get_event_loop()
            self._stop_handle = loop.call_later(self._GRACE_S, self._stop)

    def _stop(self) -> None:
        self._stop_handle = None
        if self._subscribers > 0:                  # someone re-subscribed in the grace
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        register_bg_task(asyncio.create_task(asyncio.to_thread(_camera_release)))

    async def _produce(self) -> None:
        fps = int(os.environ.get("LCNC_CAMERA_FPS", "15"))
        delay = 1.0 / max(1, fps)
        while True:
            jpeg = await asyncio.to_thread(_camera_grab_jpeg)
            if jpeg is not None:
                async with self._cond:
                    self._latest = jpeg
                    self._seq += 1
                    self._cond.notify_all()
            await asyncio.sleep(delay)

    async def frames(self):
        """Yield the latest shared JPEG, newest-wins (a slow viewer drops frames,
        never the producer or other viewers)."""
        last_seq = -1
        while True:
            async with self._cond:
                await self._cond.wait_for(lambda: self._seq != last_seq)
                last_seq = self._seq
                jpeg = self._latest
            if jpeg is not None:
                yield jpeg


_camera_broker = _CameraBroker()

# ---- Server-Side Settings Endpoints ----

@app.get("/settings")
async def get_settings():
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, load_settings)
    return {"ok": True, "settings": data}


@app.put("/settings/{section}", dependencies=[Depends(require_token)])
@app.post("/settings/{section}", dependencies=[Depends(require_token)])  # POST used by sendBeacon on page exit
async def put_settings_section(section: str, request: Request):
    if section not in _VALID_SETTINGS_SECTIONS:
        return JSONResponse({"ok": False, "error": f"Unknown section: {section}"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict) or "data" not in body:
        return JSONResponse({"ok": False, "error": "Missing 'data'"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, save_settings_section, section, body["data"])
    except Exception as e:
        # e.g. refuse-to-clobber when settings.json is corrupt — surface it.
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=409)
    return {"ok": True}


@app.delete("/settings", dependencies=[Depends(require_token)])
async def delete_settings():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, reset_settings)
    return {"ok": True}


@app.get("/camera/stream")
async def camera_stream():
    # One shared producer feeds all viewers (P3): subscribing starts it on the
    # first viewer; the `finally` releases this viewer's slot so the device is
    # freed a short grace after the last one leaves.
    ok = await _camera_broker.subscribe()
    if not ok:
        return JSONResponse({"error": "No camera configured"}, status_code=503)

    async def generate():
        try:
            async for jpeg in _camera_broker.frames():
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
        finally:
            _camera_broker.unsubscribe()

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )

@app.get("/camera/status")
async def camera_status():
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, _camera_init)
    return {"available": ok, "source": os.environ.get("LCNC_CAMERA_SOURCE", "")}


# ── Production SPA mount (only when LCNC_WEBUI_DIST_DIR is set) ──────────
# MUST be after @app.websocket("/ws") — Starlette matches routes in order,
# and mount("/") is a catch-all that would swallow WebSocket connections.
_DIST_DIR = os.environ.get("LCNC_WEBUI_DIST_DIR")
if _DIST_DIR and Path(_DIST_DIR).is_dir():
    # Serve index.html ourselves so we can inject the auth token (issue #17):
    # the SPA reads window.__LCNC_TOKEN__ and presents it on the WS URL + REST
    # headers. Anyone the gateway serves the page to is already a client it
    # chose to serve, so handing them the LAN-admission token in that page does
    # not weaken it against the real threats (cross-origin WS hijack, which
    # can't read this page cross-origin, and unauthenticated REST mutation).
    def _serve_index() -> Response:
        try:
            html = (Path(_DIST_DIR) / "index.html").read_text(encoding="utf-8")
        except Exception:
            raise HTTPException(status_code=404, detail="index.html not found")
        inject = f"<script>window.__LCNC_TOKEN__ = {json.dumps(WEBUI_TOKEN)};</script>"
        html = html.replace("</head>", inject + "</head>", 1) if "</head>" in html else inject + html
        return Response(content=html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def spa_index():
        return _serve_index()

    @app.get("/index.html")
    async def spa_index_html():
        return _serve_index()

    # Catch-all for assets — MUST come after the explicit index routes above.
    app.mount("/", StaticFiles(directory=_DIST_DIR, html=True), name="spa")
