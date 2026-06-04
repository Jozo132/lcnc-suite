"""Unit tests for command_policy — the pure, linuxcnc-free backend authz policy
(issue #19). Run via ``python3 -m unittest test_command_policy`` or pytest.
"""

import unittest

from command_policy import (
    MachineState,
    evaluate_permissions,
    check_command,
    COMMAND_GATES,
    READ_ONLY_COMMANDS,
)


# Every command the gateway's handle_command dispatches (kept in sync with
# gateway.py). The coverage test asserts each is either gated or read-only, so a
# new command cannot land without a deliberate policy decision.
ALL_HANDLED_COMMANDS = {
    "abort", "add_tool", "arm", "auto_run", "auto_step", "clear_wcs",
    "cycle_pause", "cycle_resume", "cycle_start", "delete_tool", "estop",
    "estop_reset", "flood_off", "flood_on", "get_comp_grid",
    "get_probe_results", "get_probe_vars", "get_tool_table", "get_wcs_table",
    "home", "home_all", "jog_cont", "jog_cont_multi", "jog_incr",
    "jog_incr_multi", "jog_stop", "jog_stop_multi", "list_probe_macros",
    "load_file", "machine_off", "machine_on", "mdi", "mist_off", "mist_on",
    "renumber_tool", "save_tool", "set_block_delete", "set_feed_override",
    "set_max_velocity", "set_mode", "set_optional_stop", "set_probe_vars",
    "set_rapid_override", "set_spindle_override", "set_wcs", "shutdown",
    "spindle_decrease", "spindle_forward", "spindle_increase",
    "spindle_reverse", "spindle_stop", "tool_change", "unhome", "unhome_all",
    "unload_file",
}


def state(**over) -> MachineState:
    """A homed, idle, enabled, armed machine — override fields per test."""
    base = dict(
        armed=True, is_estop=False, is_enabled=True, is_homed=True,
        is_idle=True, is_running=False, is_paused=False, eoffset_enabled=False,
    )
    base.update(over)
    return MachineState(**base)


class TestCoverage(unittest.TestCase):
    def test_every_command_is_gated_or_read_only(self):
        for cmd in ALL_HANDLED_COMMANDS:
            self.assertTrue(
                cmd in COMMAND_GATES or cmd in READ_ONLY_COMMANDS,
                f"command {cmd!r} has no policy gate and is not read-only",
            )

    def test_no_stale_gate_entries(self):
        # Catch a gate entry for a command the gateway no longer handles.
        for cmd in COMMAND_GATES:
            self.assertIn(cmd, ALL_HANDLED_COMMANDS, f"stale gate for {cmd!r}")

    def test_gates_reference_real_permission_classes(self):
        valid = set(evaluate_permissions(state()).keys())
        for cmd, gate in COMMAND_GATES.items():
            self.assertIn(gate, valid, f"{cmd!r} -> unknown gate {gate!r}")


class TestPermissionPort(unittest.TestCase):
    """Spot-check the port matches permissions.ts semantics."""

    def test_disconnected_allows_only_unconditional(self):
        p = evaluate_permissions(state(armed=False))
        self.assertFalse(p["ready"])
        self.assertFalse(p["safety"])
        self.assertTrue(p["always"])

    def test_base_requires_armed_estop_enabled(self):
        self.assertFalse(evaluate_permissions(state(is_enabled=False))["ready"])
        self.assertFalse(evaluate_permissions(state(is_estop=True))["ready"])

    def test_ready_needs_homed_and_idle(self):
        self.assertTrue(evaluate_permissions(state())["ready"])
        self.assertFalse(evaluate_permissions(state(is_homed=False))["ready"])
        self.assertFalse(evaluate_permissions(state(is_idle=False))["ready"])

    def test_eoffset_blocks_probe_and_zero_only(self):
        p = evaluate_permissions(state(eoffset_enabled=True))
        self.assertFalse(p["probe"])
        self.assertFalse(p["zero"])
        self.assertTrue(p["ready"])  # ready is NOT eoffset-gated

    def test_pause_resume_are_mutually_exclusive(self):
        running = evaluate_permissions(state(is_idle=False, is_running=True))
        self.assertTrue(running["pause"])
        self.assertFalse(running["resume"])
        paused = evaluate_permissions(state(is_idle=False, is_paused=True))
        self.assertTrue(paused["resume"])
        self.assertFalse(paused["pause"])

    def test_safety_works_during_estop(self):
        # Machine On/Off must be reachable to clear estop flows; safety drops the
        # enabled/idle terms.
        self.assertTrue(evaluate_permissions(state(is_enabled=False))["safety"])


class TestCheckCommand(unittest.TestCase):
    def test_cycle_start_blocked_unhomed(self):
        self.assertIsNotNone(check_command("cycle_start", state(is_homed=False)))
        self.assertIn("homed", check_command("cycle_start", state(is_homed=False)).lower())

    def test_cycle_start_allowed_when_ready(self):
        self.assertIsNone(check_command("cycle_start", state()))

    def test_mdi_blocked_while_running(self):
        self.assertIsNotNone(check_command("mdi", state(is_idle=False, is_running=True)))

    def test_jog_stop_always_allowed(self):
        # Even fully disarmed / estopped — stopping must never be policy-denied.
        self.assertIsNone(check_command("jog_stop", state(armed=False, is_estop=True)))
        self.assertIsNone(check_command("jog_stop_multi", state(armed=False)))

    def test_jog_blocked_unhomed(self):
        self.assertIsNotNone(check_command("jog_cont", state(is_homed=False)))

    def test_touchoff_blocked_with_eoffset(self):
        r = check_command("set_wcs", state(eoffset_enabled=True))
        self.assertIsNotNone(r)
        self.assertIn("compensation", r.lower())

    def test_tool_table_edit_needs_idle_not_enabled(self):
        # setup gate: armed + !estop + idle, but NOT machine-on.
        self.assertIsNone(check_command("save_tool", state(is_enabled=False)))
        self.assertIsNotNone(check_command("save_tool", state(is_idle=False, is_running=True)))

    def test_not_armed_reason(self):
        self.assertEqual(check_command("cycle_start", state(armed=False)), "Not armed")

    def test_unknown_command_not_policy_denied(self):
        self.assertIsNone(check_command("totally_made_up", state(armed=False)))

    def test_read_only_not_policy_denied(self):
        self.assertIsNone(check_command("get_tool_table", state(armed=False)))

    def test_machine_on_blocked_in_estop(self):
        self.assertIsNotNone(check_command("machine_on", state(is_estop=True)))


if __name__ == "__main__":
    unittest.main()
