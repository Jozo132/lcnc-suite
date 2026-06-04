#!/usr/bin/env python3
"""Backend command authorization policy (issue #19).

Pure, ``linuxcnc``-free mirror of the frontend permission model
(``lcnc-webui/src/permissions.ts``). The frontend disables controls the
operator should not use; this module lets the GATEWAY refuse commands a
direct or buggy websocket client could otherwise send in a forbidden machine
state. Same intent, enforced on the trusted side.

Scope is the SAFETY-RELEVANT subset of the frontend's classes, not a 1:1
mirror of every UX nicety: homed-before-motion, idle-before-mode-change,
no-run/MDI-while-running, and eoffset-contamination. This is AUTHORIZATION,
not abort-safety — it only refuses *new* commands; motion-abort safety lives
in the HAL chain (see feedback_armed_is_authorization_not_deadman).

Deliberately omitted vs. the frontend ``MachineState``:
  - ``busy``: a client-side debounce/settling flag the gateway cannot observe,
    and a UX concern rather than a safety floor. Dropping it makes the backend
    gate intentionally COARSER (no ``!busy`` term), so the policy never rejects
    a command the machine would actually accept — it only ever blocks the
    clear, dangerous cases.
  - ``hasFile``: not a safety gate.

Keep this file pure: stdlib only, no ``linuxcnc`` import, no import-time side
effects, so it is unit-testable on a plain developer machine.
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class MachineState:
    """The machine-state inputs the policy needs. Built on the gateway from the
    same ``safe_get`` / ``normalize_homed`` / reader values the status broadcast
    uses, so the backend gate agrees with what the frontend sees."""
    armed: bool
    is_estop: bool
    is_enabled: bool
    is_homed: bool
    is_idle: bool
    is_running: bool
    is_paused: bool
    eoffset_enabled: bool


def evaluate_permissions(s: MachineState) -> Dict[str, bool]:
    """Python port of ``evaluatePermissions()`` — MUST stay in lockstep with
    ``lcnc-webui/src/permissions.ts``. The ``!busy`` term present in the
    frontend is dropped on purpose (see module docstring), which only ever
    makes these gates more permissive than the UI, never less."""
    base = s.armed and not s.is_estop and s.is_enabled
    return {
        "idle":     base and s.is_idle,
        "jog":      base and s.is_idle and s.is_homed,
        "override": base,
        "ready":    base and s.is_idle and s.is_homed,
        "pause":    base and s.is_running and not s.is_paused,
        "resume":   base and s.is_paused,
        "step":     base and ((s.is_idle and s.is_homed) or s.is_paused),
        "abort":    base,
        "probe":    base and s.is_idle and s.is_homed and not s.eoffset_enabled,
        "zero":     base and s.is_idle and not s.eoffset_enabled,
        "safety":   s.armed and not s.is_estop,
        "setup":    s.armed and not s.is_estop and s.is_idle,
        "armed":    s.armed,
        "always":   True,
    }


# Each mutating command -> the permission gate it requires. ``always`` means the
# command is never blocked by machine state (its own handler-side guard — e.g.
# ``require_armed`` or a confirmation dialog — is the gate). Read-only queries
# are dispatched before this check and are intentionally absent here. The unit
# tests assert this table plus READ_ONLY_COMMANDS covers every command the
# gateway handles, so a newly added command cannot silently land ungated.
COMMAND_GATES: Dict[str, str] = {
    # --- unconditional / safety handshake ---
    "arm": "always",
    "estop": "always",
    "estop_reset": "always",       # runs WHILE in estop; handler require_armed gates it
    "shutdown": "always",          # confirmation dialog is the safety gate
    "abort": "abort",
    "machine_on": "safety",
    "machine_off": "safety",
    # --- mode selection ---
    "set_mode": "idle",
    # --- jogging (stopping is always allowed) ---
    "jog_cont": "jog",
    "jog_incr": "jog",
    "jog_cont_multi": "jog",
    "jog_incr_multi": "jog",
    "jog_stop": "always",
    "jog_stop_multi": "always",
    # --- homing ---
    "home": "zero",
    "home_all": "zero",
    "unhome": "zero",
    "unhome_all": "zero",
    # --- program execution ---
    "cycle_start": "ready",
    "auto_run": "ready",
    "auto_step": "step",
    "cycle_pause": "pause",
    "cycle_resume": "resume",
    "mdi": "ready",
    # --- spindle / coolant ---
    "spindle_forward": "ready",
    "spindle_reverse": "ready",
    "spindle_stop": "ready",
    "spindle_increase": "ready",
    "spindle_decrease": "ready",
    "flood_on": "ready",
    "flood_off": "ready",
    "mist_on": "ready",
    "mist_off": "ready",
    # --- overrides (intentionally usable during execution) ---
    "set_feed_override": "override",
    "set_spindle_override": "override",
    "set_rapid_override": "override",
    "set_max_velocity": "override",
    "set_block_delete": "override",
    "set_optional_stop": "override",
    # --- tool change (M6 — runs motion, must not contaminate via eoffset) ---
    "tool_change": "probe",
    # --- work offsets / probing setup ---
    "set_wcs": "probe",
    "clear_wcs": "probe",
    "set_probe_vars": "ready",
    # --- tool-table edits (no machine-enabled needed) ---
    "save_tool": "setup",
    "add_tool": "setup",
    "delete_tool": "setup",
    "renumber_tool": "setup",
    # --- file ops ---
    "load_file": "setup",
    "unload_file": "setup",
}

# Read-only queries handled before the policy check — no machine-state gate.
READ_ONLY_COMMANDS = frozenset({
    "get_tool_table", "get_probe_results", "get_comp_grid",
    "get_probe_vars", "get_wcs_table", "list_probe_macros",
})


def check_command(cmd: str, state: MachineState) -> Optional[str]:
    """Return ``None`` if ``cmd`` is allowed in ``state``, else a short,
    operator-readable deny reason.

    Unknown commands and read-only queries return ``None`` — it is not the
    policy's job to reject them (the dispatcher reports unknown commands, and
    read-only queries do not mutate the machine)."""
    gate = COMMAND_GATES.get(cmd)
    if gate is None:
        return None
    if evaluate_permissions(state).get(gate, False):
        return None
    return _deny_reason(gate, state)


def _deny_reason(gate: str, s: MachineState) -> str:
    """Best-effort explanation for a denied command — most specific unmet
    precondition first. The authoritative decision is the gate evaluation
    above; this only produces the message."""
    if not s.armed:
        return "Not armed"
    if s.is_estop:
        return "E-stop active"
    if not s.is_enabled and gate not in ("safety", "setup"):
        return "Machine not on"
    if gate in ("probe", "zero") and s.eoffset_enabled:
        return "Surface compensation active — clear the eoffset first"
    if gate in ("ready", "jog", "probe", "step") and not s.is_homed:
        return "Machine not homed"
    if gate in ("idle", "ready", "zero", "setup", "probe") and not s.is_idle:
        return "Machine not idle"
    if gate == "pause":
        return "No program running to pause"
    if gate == "resume":
        return "No program paused to resume"
    return f"Not permitted in current state (requires '{gate}')"
