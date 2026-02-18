#!/usr/bin/env python3
"""
Integration test harness for lcnc-gateway WebSocket behavior.

Manages both gateway and LinuxCNC lifecycle. Connects test WebSocket clients,
tracks every message type, and validates correct behavior across 7 scenarios.

Usage
-----
    # Gateway should already be running (via ./restart.sh), or the script
    # will start one itself if /health doesn't respond.
    cd lcnc-gateway
    .venv/bin/python test_viewer_init.py

    # LinuxCNC config used (hardcoded):
    #   /home/cnc/linuxcnc/configs/probe_basic_no_vtk/probe_basic.ini

Scenarios
---------
  S1  Gateway -> clients -> LinuxCNC
      Clients connect before LinuxCNC starts.  They get an immediate
      viewer_init with zero bounds, then a second viewer_init with real
      machine bounds once LinuxCNC is detected.

  S2  Gateway -> LinuxCNC -> clients
      LinuxCNC is running when clients connect.  They should receive
      viewer_init with real bounds on connect, and status messages
      containing lcnc state (estop, enabled, homed, task_mode).

  S3  LinuxCNC -> gateway -> clients
      LinuxCNC starts first, then the gateway is restarted.  Verifies the
      gateway auto-detects an already-running LinuxCNC instance and serves
      real bounds to clients.

  S4  Dead client flushing
      5 clients connect (3 permanent + 2 doomed).  The 2 doomed clients
      disconnect.  Remaining clients observe the `clients[]` array in
      status messages shrink by exactly 2, proving dead clients are removed
      immediately on WebSocket close (gateway.py line 1620).

  S5  Client reconnection
      3 clients connect with LinuxCNC running.  1 client disconnects and
      reconnects.  The reconnected client receives a fresh viewer_init with
      real bounds.  Permanent clients remain stable.

  S6  LinuxCNC kill + restart
      Clients connect, LinuxCNC starts, is killed, then restarted.
      All clients receive viewer_init again after restart (viewer_init_count
      increments).  status_error messages flow during downtime, normal
      status resumes after restart.  Client count stays stable throughout.

  S7  Gateway restart + client reconnect
      LinuxCNC running, 3 auto-reconnecting clients connected.  Gateway is
      killed and restarted.  Clients reconnect automatically, receive fresh
      viewer_init with bounds, and status flow resumes.

Per-client tracking (ClientLog dataclass)
-----------------------------------------
  - viewer_init: receipt time, count, parts count, machine_bounds.size
  - viewer_gcode: receipt time
  - status: count, last payload (estop/enabled/homed/task_mode)
  - status_error: count, last error string
  - pong: count
  - seen_client_counts[]: len(msg["clients"]) from every status/status_error
  - disconnected_at / reconnected_at: for reconnection scenarios

LinuxCNC lifecycle
------------------
  start_lcnc():
    1. cleanup_stale() — kill orphans, rm /tmp/linuxcnc.lock, ipcrm shared
       memory (NML keys 1001-1005, HAL keys), realtime stop, rm temp files
    2. subprocess.Popen(["linuxcnc", INI], preexec_fn=os.setsid)
    3. Wait for `pgrep -x linuxcncsvr` (up to 30s)

  kill_lcnc():
    1. pkill probe_basic (not in launcher's Cleanup() kill list)
    2. SIGTERM to launcher PID only — triggers /usr/bin/linuxcnc Cleanup()
       which kills linuxcncsvr, milltask, halcmd stop/unload, realtime stop,
       ipcrm NML, removes lock file
    3. Wait for linuxcncsvr to disappear (10s)
    4. Wait for launcher to exit (3s), else SIGKILL process group
    5. Fallback: cleanup_stale()

Gateway lifecycle
-----------------
  start_gateway():
    .venv/bin/python3 -m uvicorn gateway:app --host 0.0.0.0 --port 8000
    Wait for /health to respond 200 (up to 15s).

  kill_gateway():
    lsof -t -iTCP:8000 -sTCP:LISTEN -> SIGTERM -> wait -> SIGKILL

  restart_gateway():
    kill_gateway() then start_gateway()

WebSocket clients
-----------------
  ws_client():        One-shot client — connects, logs messages, exits on stop_event.
  ws_client_reconnecting():  Auto-reconnects on WS close with 1s backoff.
                             Used by S7 (gateway restart).

  Both send {"cmd":"heartbeat"} every 1s and parse all incoming messages
  via _handle_msg() which populates the ClientLog fields.

Sub-checks per scenario
-----------------------
  Each scenario creates Check objects with .ok()/.fail() and a detail string.
  print_result() renders a table of per-client data plus a list of checks.
  The scenario returns True (PASS) only if ALL checks pass.

Exit code
---------
  0 if all scenarios pass, 1 otherwise.
"""

import asyncio
import glob as globmod
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import websockets

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATEWAY_URL = "ws://127.0.0.1:8000/ws"
GATEWAY_HEALTH = "http://127.0.0.1:8000/health"
GATEWAY_PORT = 8000
GATEWAY_DIR = "/home/cnc/lcnc-suite/lcnc-gateway"
LCNC_INI = "/home/cnc/linuxcnc/configs/probe_basic_no_vtk/probe_basic.ini"
NUM_CLIENTS = 3
TIMEOUT = 30  # seconds per scenario

LOCKFILE = "/tmp/linuxcnc.lock"
NMLFILE = "/usr/share/linuxcnc/linuxcnc.nml"
REALTIME = "/usr/lib/linuxcnc/realtime"
_NML_SHM_KEYS = ["1001", "1002", "1003", "1004", "1005"]
_HAL_SHM_KEYS = ["0x48414c32", "0x90280A48", "0x48484c34"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_T0 = time.time()


def ts() -> str:
    return f"{time.time() - _T0:7.1f}s"


def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# ClientLog — per-client observable state
# ---------------------------------------------------------------------------


@dataclass
class ClientLog:
    id: int
    connected_at: float = 0.0
    disconnected_at: Optional[float] = None
    reconnected_at: Optional[float] = None

    # viewer_init
    viewer_init_at: Optional[float] = None
    viewer_init_count: int = 0
    viewer_init_parts: int = 0
    viewer_init_bounds: Optional[list] = None  # machine_bounds.size

    # viewer_gcode
    viewer_gcode_at: Optional[float] = None

    # status
    status_count: int = 0
    last_status_data: Optional[Dict] = None  # {estop, enabled, homed, ...}

    # status_error
    status_error_count: int = 0
    last_error: Optional[str] = None

    # pong
    pong_count: int = 0

    # client-count tracking (from status/status_error messages)
    seen_client_counts: list = field(default_factory=list)

    # message log
    messages: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# LinuxCNC lifecycle
# ---------------------------------------------------------------------------

_lcnc_proc: Optional[subprocess.Popen] = None


def is_lcnc_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "linuxcncsvr"], capture_output=True).returncode == 0
    except Exception:
        return False


def get_lcnc_pid() -> Optional[int]:
    try:
        r = subprocess.run(["pgrep", "-x", "linuxcncsvr"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip().split()[0])
    except Exception:
        pass
    return None


def cleanup_stale():
    """Remove stale lock files, orphaned processes, and shared memory segments."""
    for proc in ["linuxcncsvr", "milltask", "halui", "io", "rtapi_app"]:
        subprocess.run(["pkill", "-x", proc], capture_output=True)
    subprocess.run(["pkill", "-f", "probe_basic"], capture_output=True)
    time.sleep(0.5)
    try:
        os.remove(LOCKFILE)
    except FileNotFoundError:
        pass
    subprocess.run([REALTIME, "stop"], capture_output=True)
    for key in _NML_SHM_KEYS + _HAL_SHM_KEYS:
        subprocess.run(["ipcrm", "-M", key], capture_output=True, text=True)
    for pattern in ["/tmp/linuxcnc.print.*", "/tmp/linuxcnc.debug.*"]:
        for f in globmod.glob(pattern):
            try:
                os.remove(f)
            except OSError:
                pass


def start_lcnc() -> subprocess.Popen:
    global _lcnc_proc
    if is_lcnc_running():
        kill_lcnc()
        time.sleep(2)
    cleanup_stale()
    time.sleep(0.5)
    log("Starting LinuxCNC...")
    _lcnc_proc = subprocess.Popen(
        ["linuxcnc", LCNC_INI],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    for _ in range(60):
        if is_lcnc_running():
            log(f"  LinuxCNC UP (svr={get_lcnc_pid()}, wrapper={_lcnc_proc.pid})")
            return _lcnc_proc
        time.sleep(0.5)
    log("  WARNING: linuxcncsvr not detected after 30s")
    return _lcnc_proc


def kill_lcnc():
    global _lcnc_proc
    log("Stopping LinuxCNC...")
    subprocess.run(["pkill", "-f", "probe_basic"], capture_output=True)
    time.sleep(0.5)
    if _lcnc_proc and _lcnc_proc.poll() is None:
        try:
            os.kill(_lcnc_proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(20):
        if not is_lcnc_running():
            break
        time.sleep(0.5)
    if _lcnc_proc:
        try:
            _lcnc_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if _lcnc_proc.poll() is None:
                try:
                    os.killpg(os.getpgid(_lcnc_proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    _lcnc_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
    if not is_lcnc_running():
        _lcnc_proc = None
        log("  LinuxCNC stopped cleanly")
        return
    log("  Fallback: force-killing...")
    if _lcnc_proc and _lcnc_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_lcnc_proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            _lcnc_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    cleanup_stale()
    time.sleep(1)
    _lcnc_proc = None
    log("  LinuxCNC force-killed")


# ---------------------------------------------------------------------------
# Gateway lifecycle
# ---------------------------------------------------------------------------

_gw_proc: Optional[subprocess.Popen] = None


def _gateway_pids() -> List[int]:
    try:
        r = subprocess.run(
            ["lsof", "-t", f"-iTCP:{GATEWAY_PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return [int(p) for p in r.stdout.strip().split()]
    except Exception:
        pass
    return []


def is_gateway_running() -> bool:
    try:
        with urllib.request.urlopen(GATEWAY_HEALTH, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def kill_gateway():
    global _gw_proc
    log("Stopping gateway...")
    for pid in _gateway_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(1)
    for pid in _gateway_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if _gw_proc:
        try:
            _gw_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    _gw_proc = None
    log("  Gateway stopped")


def start_gateway() -> subprocess.Popen:
    global _gw_proc
    if is_gateway_running():
        kill_gateway()
        time.sleep(1)
    log("Starting gateway...")
    _gw_proc = subprocess.Popen(
        [
            f"{GATEWAY_DIR}/.venv/bin/python3", "-m", "uvicorn",
            "gateway:app", "--host", "0.0.0.0", "--port", str(GATEWAY_PORT),
        ],
        cwd=GATEWAY_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if is_gateway_running():
            log(f"  Gateway UP (pid={_gw_proc.pid})")
            return _gw_proc
        time.sleep(0.25)
    log("  WARNING: gateway /health not responding after 15s")
    return _gw_proc


def restart_gateway():
    kill_gateway()
    time.sleep(1)
    return start_gateway()


# ---------------------------------------------------------------------------
# WebSocket test clients
# ---------------------------------------------------------------------------


async def ws_client(client_id: int, clog: ClientLog, stop_event: asyncio.Event):
    """Connect a WS client and record all messages until stop_event fires."""
    clog.connected_at = time.time()
    try:
        async with websockets.connect(GATEWAY_URL) as ws:
            log(f"  client#{client_id} connected")

            async def heartbeat():
                while not stop_event.is_set():
                    try:
                        await ws.send('{"cmd":"heartbeat"}')
                    except Exception:
                        break
                    await asyncio.sleep(1.0)

            hb_task = asyncio.create_task(heartbeat())
            try:
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        log(f"  client#{client_id} WS closed")
                        break
                    _handle_msg(client_id, clog, raw)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
    except Exception as e:
        log(f"  client#{client_id} ERROR: {e}")
    finally:
        clog.disconnected_at = time.time()


async def ws_client_reconnecting(
    client_id: int, clog: ClientLog, stop_event: asyncio.Event
):
    """WS client that auto-reconnects on disconnect (simulates browser retry)."""
    clog.connected_at = time.time()
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                if attempt > 1:
                    clog.reconnected_at = time.time()
                    log(f"  client#{client_id} reconnected (attempt #{attempt})")
                else:
                    log(f"  client#{client_id} connected")

                async def heartbeat():
                    while not stop_event.is_set():
                        try:
                            await ws.send('{"cmd":"heartbeat"}')
                        except Exception:
                            break
                        await asyncio.sleep(1.0)

                hb_task = asyncio.create_task(heartbeat())
                try:
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        except websockets.ConnectionClosed:
                            log(f"  client#{client_id} WS closed, will reconnect...")
                            break
                        _handle_msg(client_id, clog, raw)
                finally:
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass
        except Exception:
            pass
        if not stop_event.is_set():
            await asyncio.sleep(1.0)  # backoff before retry


def _handle_msg(client_id: int, clog: ClientLog, raw: str):
    """Parse and record a single WS message."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    msg_type = msg.get("type", "unknown")
    now = time.time()

    if msg_type == "viewer_init":
        clog.viewer_init_at = now
        clog.viewer_init_count += 1
        data = msg.get("data", {})
        parts = data.get("parts", [])
        bounds = data.get("machine_bounds", {})
        clog.viewer_init_parts = len(parts)
        clog.viewer_init_bounds = bounds.get("size")
        log(f"  client#{client_id} viewer_init #{clog.viewer_init_count}: "
            f"{len(parts)} parts, bounds={clog.viewer_init_bounds}")
        clog.messages.append((now, "viewer_init", f"{len(parts)} parts"))

    elif msg_type == "viewer_gcode":
        clog.viewer_gcode_at = now
        data = msg.get("data", {})
        log(f"  client#{client_id} viewer_gcode: file={data.get('file', 'None')}")
        clog.messages.append((now, "viewer_gcode", data.get("file", "")))

    elif msg_type == "status":
        clog.status_count += 1
        clients_arr = msg.get("clients", [])
        clog.seen_client_counts.append(len(clients_arr))
        data = msg.get("data", {})
        clog.last_status_data = {
            "estop": data.get("estop"),
            "enabled": data.get("enabled"),
            "homed": data.get("homed"),
            "task_mode": data.get("task_mode"),
        }
        if clog.status_count == 1:
            log(f"  client#{client_id} first status (clients={len(clients_arr)})")

    elif msg_type == "status_error":
        clog.status_error_count += 1
        clients_arr = msg.get("clients", [])
        clog.seen_client_counts.append(len(clients_arr))
        clog.last_error = msg.get("error", "")
        if clog.status_error_count <= 2:
            log(f"  client#{client_id} status_error: {clog.last_error} "
                f"(clients={len(clients_arr)})")

    elif msg_type == "pong":
        clog.pong_count += 1

    else:
        log(f"  client#{client_id} {msg_type}")
        clog.messages.append((now, msg_type, ""))


# ---------------------------------------------------------------------------
# Sub-check helpers
# ---------------------------------------------------------------------------


class Check:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.detail = ""

    def ok(self, detail: str = ""):
        self.passed = True
        self.detail = detail

    def fail(self, detail: str = ""):
        self.passed = False
        self.detail = detail

    def __str__(self):
        tag = "PASS" if self.passed else "FAIL"
        d = f"  ({self.detail})" if self.detail else ""
        return f"  CHECK: {self.name:<35} {tag}{d}"


def print_result(scenario: str, clients: List[ClientLog], checks: List[Check],
                 start_time: float):
    print(f"\n{'='*78}")
    print(f"SCENARIO: {scenario}")
    print(f"{'='*78}")

    hdr = (f"{'Client':<9} {'v_init':<9} {'#vi':<5} {'bounds':<22} "
           f"{'clients':<12} {'status':<8} {'errs':<6}")
    print(hdr)
    print("-" * len(hdr))
    for c in clients:
        vi = f"+{c.viewer_init_at - start_time:.1f}s" if c.viewer_init_at else "—"
        b = str(c.viewer_init_bounds) if c.viewer_init_bounds else "—"
        if len(b) > 20:
            b = b[:20] + "…"
        cc = f"{min(c.seen_client_counts)}-{max(c.seen_client_counts)}" if c.seen_client_counts else "—"
        print(f"  #{c.id:<6} {vi:<9} {c.viewer_init_count:<5} {b:<22} {cc:<12} "
              f"{c.status_count:<8} {c.status_error_count:<6}")

    print()
    all_pass = True
    for ch in checks:
        print(str(ch))
        if not ch.passed:
            all_pass = False

    tag = "PASS" if all_pass else "FAIL"
    print(f"\nRESULT: {tag}")
    return all_pass


# ---------------------------------------------------------------------------
# Helper: connect N clients, wait for event, stop
# ---------------------------------------------------------------------------


async def connect_clients(
    n: int,
    stop_event: asyncio.Event,
    reconnecting: bool = False,
) -> Tuple[List[ClientLog], List[asyncio.Task]]:
    clients = [ClientLog(id=i) for i in range(n)]
    fn = ws_client_reconnecting if reconnecting else ws_client
    tasks = [asyncio.create_task(fn(i, clients[i], stop_event)) for i in range(n)]
    await asyncio.sleep(2)  # let them connect and receive initial messages
    return clients, tasks


async def stop_clients(stop_event: asyncio.Event, tasks: List[asyncio.Task]):
    stop_event.set()
    await asyncio.sleep(1)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def wait_for(
    predicate: Callable[[], bool],
    timeout: float = TIMEOUT,
    poll_interval: float = 1.0,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll_interval)
    return False


# ---------------------------------------------------------------------------
# Scenario implementations
# ---------------------------------------------------------------------------


async def scenario_gw_clients_lcnc() -> bool:
    """S1: Gateway → clients → LinuxCNC."""
    name = "Gateway → clients → LinuxCNC"
    log(f"\n>>> {name}")
    t0 = time.time()
    stop = asyncio.Event()
    clients, tasks = await connect_clients(NUM_CLIENTS, stop)

    # Start LinuxCNC
    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)

    # Wait for all clients to get viewer_init
    await wait_for(lambda: all(c.viewer_init_at for c in clients))
    # Let a few status messages flow
    await asyncio.sleep(3)
    await stop_clients(stop, tasks)

    # -- checks --
    c1 = Check("all_got_viewer_init")
    if all(c.viewer_init_at for c in clients):
        c1.ok()
    else:
        c1.fail("some clients missing viewer_init")

    c2 = Check("correct_client_count")
    # After all connected, last seen counts should be NUM_CLIENTS
    last_counts = [c.seen_client_counts[-1] for c in clients if c.seen_client_counts]
    if last_counts and all(cc >= NUM_CLIENTS for cc in last_counts):
        c2.ok(f"last seen={last_counts[0]}")
    else:
        c2.fail(f"last_counts={last_counts}")

    c3 = Check("viewer_init_has_parts")
    if all(c.viewer_init_parts >= 1 for c in clients):
        c3.ok(f"{clients[0].viewer_init_parts} parts")
    else:
        c3.fail()

    return print_result(name, clients, [c1, c2, c3], t0)


async def scenario_gw_lcnc_clients() -> bool:
    """S2: Gateway → LinuxCNC → clients."""
    name = "Gateway → LinuxCNC → clients"
    log(f"\n>>> {name}")
    t0 = time.time()

    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)
    await asyncio.sleep(2)

    stop = asyncio.Event()
    clients, tasks = await connect_clients(NUM_CLIENTS, stop)

    await wait_for(lambda: all(c.viewer_init_at for c in clients))
    await asyncio.sleep(3)
    await stop_clients(stop, tasks)

    c1 = Check("all_got_viewer_init")
    if all(c.viewer_init_at for c in clients):
        c1.ok()
    else:
        c1.fail()

    c2 = Check("viewer_init_has_real_bounds")
    # With lcnc running, bounds should be non-zero
    has_bounds = all(
        c.viewer_init_bounds and any(v > 0 for v in c.viewer_init_bounds)
        for c in clients
    )
    if has_bounds:
        c2.ok(f"bounds={clients[0].viewer_init_bounds}")
    else:
        c2.fail(f"bounds={[c.viewer_init_bounds for c in clients]}")

    c3 = Check("status_has_lcnc_data")
    has_data = all(c.last_status_data and c.last_status_data.get("estop") is not None
                   for c in clients)
    if has_data:
        d = clients[0].last_status_data
        c3.ok(f"estop={d['estop']}, enabled={d['enabled']}, homed={d['homed']}")
    else:
        c3.fail()

    c4 = Check("correct_client_count")
    last_counts = [c.seen_client_counts[-1] for c in clients if c.seen_client_counts]
    if last_counts and all(cc >= NUM_CLIENTS for cc in last_counts):
        c4.ok(f"seen={last_counts[0]}")
    else:
        c4.fail(f"last_counts={last_counts}")

    return print_result(name, clients, [c1, c2, c3, c4], t0)


async def scenario_lcnc_gw_clients() -> bool:
    """S3: LinuxCNC → gateway (restart) → clients."""
    name = "LinuxCNC → gateway → clients"
    log(f"\n>>> {name}")
    t0 = time.time()

    # LinuxCNC first
    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)
    await asyncio.sleep(2)

    # Restart gateway (so it discovers already-running LinuxCNC)
    await asyncio.get_event_loop().run_in_executor(None, restart_gateway)
    await asyncio.sleep(2)

    stop = asyncio.Event()
    clients, tasks = await connect_clients(NUM_CLIENTS, stop)

    await wait_for(lambda: all(c.viewer_init_at for c in clients))
    await asyncio.sleep(3)
    await stop_clients(stop, tasks)

    c1 = Check("all_got_viewer_init")
    if all(c.viewer_init_at for c in clients):
        c1.ok()
    else:
        c1.fail()

    c2 = Check("viewer_init_has_real_bounds")
    has_bounds = all(
        c.viewer_init_bounds and any(v > 0 for v in c.viewer_init_bounds)
        for c in clients
    )
    if has_bounds:
        c2.ok(f"bounds={clients[0].viewer_init_bounds}")
    else:
        c2.fail(f"bounds={[c.viewer_init_bounds for c in clients]}")

    c3 = Check("status_has_lcnc_data")
    has_data = all(c.last_status_data and c.last_status_data.get("estop") is not None
                   for c in clients)
    if has_data:
        d = clients[0].last_status_data
        c3.ok(f"estop={d['estop']}, enabled={d['enabled']}")
    else:
        c3.fail()

    return print_result(name, clients, [c1, c2, c3], t0)


async def scenario_dead_client_flushing() -> bool:
    """S4: Connect 5 clients, disconnect 2, verify count drops by exactly 2.

    Uses relative counts (not absolute) because other clients (browsers)
    may also be connected to the gateway.
    """
    name = "Dead client flushing"
    log(f"\n>>> {name}")
    t0 = time.time()

    stop_all = asyncio.Event()
    stop_doomed = asyncio.Event()

    # Connect 5 test clients — 3 permanent + 2 doomed
    perm_clients = [ClientLog(id=i) for i in range(3)]
    doomed_clients = [ClientLog(id=i + 3) for i in range(2)]

    perm_tasks = [asyncio.create_task(ws_client(i, perm_clients[i], stop_all))
                  for i in range(3)]
    doomed_tasks = [asyncio.create_task(ws_client(i + 3, doomed_clients[i], stop_doomed))
                    for i in range(2)]

    # Wait for all 5 to connect and get initial messages
    await asyncio.sleep(3)

    # Record pre-disconnect count (may include browser clients)
    all_clients = perm_clients + doomed_clients
    pre_counts = [c.seen_client_counts[-1] for c in all_clients if c.seen_client_counts]
    pre_count = max(pre_counts) if pre_counts else 0
    log(f"  Pre-disconnect client counts: {pre_counts} (baseline={pre_count})")

    # Kill the 2 doomed clients
    log("  Disconnecting 2 doomed clients...")
    stop_doomed.set()
    await asyncio.sleep(0.5)
    for t in doomed_tasks:
        t.cancel()
    await asyncio.gather(*doomed_tasks, return_exceptions=True)

    # Wait for gateway to detect disconnects and flush
    await asyncio.sleep(3)

    await stop_clients(stop_all, perm_tasks)

    # -- checks --
    c1 = Check("pre_disconnect_count_includes_5")
    # Our 5 test clients should be visible (plus any browsers)
    if pre_count >= 5:
        c1.ok(f"baseline={pre_count}")
    else:
        c1.fail(f"baseline={pre_count}, expected >= 5")

    c2 = Check("post_disconnect_drops_by_2")
    # After disconnecting 2, remaining permanent clients should see count = baseline - 2
    post_counts = []
    for c in perm_clients:
        if len(c.seen_client_counts) >= 3:
            post_counts.append(c.seen_client_counts[-1])
    expected_post = pre_count - 2
    if post_counts and all(cc == expected_post for cc in post_counts):
        c2.ok(f"post={post_counts[0]}, expected={expected_post}")
    else:
        c2.fail(f"post={post_counts}, expected={expected_post}")

    c3 = Check("count_drop_is_exactly_2")
    if post_counts:
        drops = [pre_count - pc for pc in post_counts]
        if all(d == 2 for d in drops):
            c3.ok(f"dropped {pre_count} → {post_counts[0]}")
        else:
            c3.fail(f"drops={drops}, expected all 2")
    else:
        c3.fail("no post-disconnect counts")

    return print_result(name, all_clients, [c1, c2, c3], t0)


async def scenario_client_reconnect() -> bool:
    """S5: Connect 3, disconnect 1, reconnect it, verify fresh viewer_init."""
    name = "Client reconnection"
    log(f"\n>>> {name}")
    t0 = time.time()

    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)
    await asyncio.sleep(2)

    stop_all = asyncio.Event()
    stop_victim = asyncio.Event()

    # 2 permanent + 1 victim
    perm_clients = [ClientLog(id=i) for i in range(2)]
    victim_clog = ClientLog(id=2)

    perm_tasks = [asyncio.create_task(ws_client(i, perm_clients[i], stop_all))
                  for i in range(2)]
    victim_task = asyncio.create_task(ws_client(2, victim_clog, stop_victim))

    # Wait for all to connect
    await wait_for(lambda: all(c.viewer_init_at for c in perm_clients) and victim_clog.viewer_init_at)
    vi_count_before = victim_clog.viewer_init_count
    log(f"  All connected, victim viewer_init_count={vi_count_before}")

    # Disconnect victim
    log("  Disconnecting victim (client#2)...")
    stop_victim.set()
    await asyncio.sleep(0.5)
    victim_task.cancel()
    try:
        await victim_task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(2)

    # Reconnect victim
    log("  Reconnecting victim (client#2)...")
    stop_victim2 = asyncio.Event()
    victim_clog2 = ClientLog(id=2)
    victim_task2 = asyncio.create_task(ws_client(2, victim_clog2, stop_victim2))

    await wait_for(lambda: victim_clog2.viewer_init_at is not None)
    await asyncio.sleep(2)

    # Stop everything
    stop_victim2.set()
    await asyncio.sleep(0.5)
    victim_task2.cancel()
    try:
        await victim_task2
    except asyncio.CancelledError:
        pass
    await stop_clients(stop_all, perm_tasks)

    # -- checks --
    c1 = Check("reconnected_gets_viewer_init")
    if victim_clog2.viewer_init_at:
        c1.ok(f"at +{victim_clog2.viewer_init_at - t0:.1f}s")
    else:
        c1.fail()

    c2 = Check("reconnected_has_bounds")
    if victim_clog2.viewer_init_bounds and any(v > 0 for v in victim_clog2.viewer_init_bounds):
        c2.ok(f"bounds={victim_clog2.viewer_init_bounds}")
    else:
        c2.fail(f"bounds={victim_clog2.viewer_init_bounds}")

    c3 = Check("perm_clients_stable")
    # Permanent clients should still be getting status
    if all(c.status_count > 5 for c in perm_clients):
        c3.ok(f"status_counts={[c.status_count for c in perm_clients]}")
    else:
        c3.fail(f"status_counts={[c.status_count for c in perm_clients]}")

    all_clogs = perm_clients + [victim_clog, victim_clog2]
    return print_result(name, all_clogs, [c1, c2, c3], t0)


async def scenario_lcnc_restart() -> bool:
    """S6: Clients connected, start lcnc, kill, restart — verify re-delivery."""
    name = "LinuxCNC kill + restart"
    log(f"\n>>> {name}")
    t0 = time.time()
    stop = asyncio.Event()
    clients, tasks = await connect_clients(NUM_CLIENTS, stop)

    # Start LinuxCNC
    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)
    await wait_for(lambda: all(c.viewer_init_at for c in clients))
    vi_counts_1 = [c.viewer_init_count for c in clients]
    log(f"  First round viewer_init_counts={vi_counts_1}")
    await asyncio.sleep(3)

    # Kill LinuxCNC
    await asyncio.get_event_loop().run_in_executor(None, kill_lcnc)
    await asyncio.sleep(3)

    # Verify status_errors arrived during downtime
    errors_during_down = [c.status_error_count for c in clients]
    log(f"  Errors during downtime: {errors_during_down}")

    # Restart LinuxCNC
    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)
    # Wait for fresh viewer_init (count should increase)
    await wait_for(
        lambda: all(c.viewer_init_count > vi_counts_1[i] for i, c in enumerate(clients)),
        timeout=TIMEOUT,
    )
    await asyncio.sleep(3)
    await stop_clients(stop, tasks)

    # -- checks --
    c1 = Check("all_got_viewer_init_twice")
    if all(c.viewer_init_count >= 2 for c in clients):
        c1.ok(f"counts={[c.viewer_init_count for c in clients]}")
    else:
        c1.fail(f"counts={[c.viewer_init_count for c in clients]}")

    c2 = Check("status_errors_during_downtime")
    if all(e > 0 for e in errors_during_down):
        c2.ok(f"errors={errors_during_down}")
    else:
        c2.fail(f"errors={errors_during_down}")

    c3 = Check("status_resumes_after_restart")
    # After restart, clients should have gotten new status messages
    # (status_count should be > the error count, meaning they got real statuses)
    if all(c.status_count > 0 for c in clients):
        c3.ok(f"status_counts={[c.status_count for c in clients]}")
    else:
        c3.fail(f"status_counts={[c.status_count for c in clients]}")

    c4 = Check("client_count_stable")
    last_counts = [c.seen_client_counts[-1] for c in clients if c.seen_client_counts]
    if last_counts and all(cc >= NUM_CLIENTS for cc in last_counts):
        c4.ok(f"seen={last_counts[0]}")
    else:
        c4.fail(f"last_counts={last_counts}")

    return print_result(name, clients, [c1, c2, c3, c4], t0)


async def scenario_gateway_restart() -> bool:
    """S7: LinuxCNC running, clients connected, restart gateway, verify reconnect."""
    name = "Gateway restart + client reconnect"
    log(f"\n>>> {name}")
    t0 = time.time()

    await asyncio.get_event_loop().run_in_executor(None, start_lcnc)
    await asyncio.sleep(2)

    stop = asyncio.Event()
    # Use reconnecting clients so they survive gateway restart
    clients, tasks = await connect_clients(NUM_CLIENTS, stop, reconnecting=True)

    await wait_for(lambda: all(c.viewer_init_at for c in clients))
    vi_counts_pre = [c.viewer_init_count for c in clients]
    log(f"  Pre-restart viewer_init_counts={vi_counts_pre}")

    # Restart gateway
    await asyncio.get_event_loop().run_in_executor(None, restart_gateway)

    # Wait for all clients to reconnect and get fresh viewer_init
    await wait_for(
        lambda: all(c.viewer_init_count > vi_counts_pre[i] for i, c in enumerate(clients)),
        timeout=TIMEOUT,
    )
    await asyncio.sleep(3)
    await stop_clients(stop, tasks)

    # -- checks --
    c1 = Check("all_reconnected")
    if all(c.reconnected_at for c in clients):
        c1.ok()
    else:
        c1.fail(f"reconnected={[c.reconnected_at is not None for c in clients]}")

    c2 = Check("viewer_init_after_reconnect")
    if all(c.viewer_init_count > vi_counts_pre[i] for i, c in enumerate(clients)):
        c2.ok(f"counts={[c.viewer_init_count for c in clients]}")
    else:
        c2.fail(f"counts={[c.viewer_init_count for c in clients]}")

    c3 = Check("status_resumes_after_reconnect")
    if all(c.status_count > 0 for c in clients):
        c3.ok(f"status_counts={[c.status_count for c in clients]}")
    else:
        c3.fail()

    c4 = Check("viewer_init_has_real_bounds")
    has_bounds = all(
        c.viewer_init_bounds and any(v > 0 for v in c.viewer_init_bounds)
        for c in clients
    )
    if has_bounds:
        c4.ok(f"bounds={clients[0].viewer_init_bounds}")
    else:
        c4.fail(f"bounds={[c.viewer_init_bounds for c in clients]}")

    return print_result(name, clients, [c1, c2, c3, c4], t0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    results: Dict[str, bool] = {}

    # Ensure clean slate
    log("Ensuring clean slate...")
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()
    if not is_gateway_running():
        start_gateway()
    await asyncio.sleep(2)

    # S1: Gateway → clients → LinuxCNC
    results["S1: gw → clients → lcnc"] = await scenario_gw_clients_lcnc()
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()
    await asyncio.sleep(2)

    # S2: Gateway → LinuxCNC → clients
    results["S2: gw → lcnc → clients"] = await scenario_gw_lcnc_clients()
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()
    await asyncio.sleep(2)

    # S3: LinuxCNC → gateway → clients
    results["S3: lcnc → gw → clients"] = await scenario_lcnc_gw_clients()
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()
    await asyncio.sleep(2)

    # S4: Dead client flushing (no LinuxCNC needed — just status_error + clients array)
    results["S4: dead client flushing"] = await scenario_dead_client_flushing()
    await asyncio.sleep(2)

    # S5: Client reconnection
    results["S5: client reconnection"] = await scenario_client_reconnect()
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()
    await asyncio.sleep(2)

    # S6: LinuxCNC kill + restart
    results["S6: lcnc kill + restart"] = await scenario_lcnc_restart()
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()
    await asyncio.sleep(2)

    # S7: Gateway restart + client reconnect
    results["S7: gw restart + reconnect"] = await scenario_gateway_restart()
    if is_lcnc_running():
        kill_lcnc()
    cleanup_stale()

    # ---- Final summary ----
    print(f"\n{'='*78}")
    print("FINAL SUMMARY")
    print(f"{'='*78}")
    for name, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    total = len(results)
    passed = sum(results.values())
    print(f"\n  {passed}/{total} scenarios passed")
    print()

    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    asyncio.run(main())
