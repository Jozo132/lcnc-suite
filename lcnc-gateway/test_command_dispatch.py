"""Integration tests for the command dispatch — backend policy enforcement
(issue #19) and the estop/enabled HAL-merge (issues #14 + #19).

Imports the REAL gateway module under a fake linuxcnc so handlers run
off-machine. Requires the gateway venv (fastapi/msgspec are real deps):

    .venv/bin/python3 -m unittest test_command_dispatch
"""
import asyncio
import unittest
from types import SimpleNamespace

import fake_linuxcnc
linuxcnc = fake_linuxcnc.install()   # MUST precede `import gateway`
import gateway  # noqa: E402  (import after the fake is installed)


def _run(coro):
    # Fresh cmd lock per call: it's a lazily-created asyncio.Lock and asyncio.run
    # spins a new loop each time, so a cached lock would bind to a stale loop.
    gateway._cmd_lock = None
    return asyncio.run(coro)


def _payload(**over):
    """Duck-typed status snapshot exposing only the fields the policy reads."""
    base = dict(
        estop=False, enabled=True, emc_enable_in=True, homed=True,
        interp_state=linuxcnc.INTERP_IDLE, paused=False, eoffset_enabled=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


# Reasons check_command/_deny_reason can return — used to assert a reply is (or
# is not) a policy denial without coupling to exact wording.
POLICY_DENIALS = {
    "Not armed", "E-stop active", "Machine not on", "Machine not homed",
    "Machine not idle",
}


class TestPolicyStateMerge(unittest.TestCase):
    """_policy_state_from_payload — the estop/enabled merge that moved
    server-side from the frontend isEstop/isEnabled computeds (issue #14)."""

    def test_safety_chain_open_forces_estop_and_disables(self):
        s = gateway._policy_state_from_payload(
            _payload(estop=False, enabled=True, emc_enable_in=False), armed=True)
        self.assertTrue(s.is_estop)     # estop OR chain-open
        self.assertFalse(s.is_enabled)  # enabled AND emc is not False

    def test_stat_estop_alone_is_estop(self):
        self.assertTrue(
            gateway._policy_state_from_payload(_payload(estop=True), armed=True).is_estop)

    def test_emc_none_is_not_chain_open(self):
        # None (reader stale / pin unavailable) must NOT read as a chain trip.
        s = gateway._policy_state_from_payload(_payload(emc_enable_in=None), armed=True)
        self.assertFalse(s.is_estop)
        self.assertTrue(s.is_enabled)

    def test_running_and_paused_derivation(self):
        run = gateway._policy_state_from_payload(
            _payload(interp_state=linuxcnc.INTERP_READING), armed=True)
        self.assertTrue(run.is_running)
        self.assertFalse(run.is_idle)
        self.assertFalse(run.is_paused)
        paused = gateway._policy_state_from_payload(
            _payload(interp_state=linuxcnc.INTERP_READING, paused=True), armed=True)
        self.assertTrue(paused.is_paused)
        self.assertFalse(paused.is_running)

    def test_armed_passthrough(self):
        self.assertTrue(gateway._policy_state_from_payload(_payload(), armed=True).armed)
        self.assertFalse(gateway._policy_state_from_payload(_payload(), armed=False).armed)


class TestDispatchEnforcement(unittest.TestCase):
    """handle_command rejects forbidden commands BEFORE they reach a handler."""

    def setUp(self):
        gateway.lcnc_connected = True

    def _send(self, msg, armed=True, **state):
        gateway._shared_status = _payload(**state)
        return _run(gateway.handle_command(msg, armed))

    def test_cycle_start_denied_unhomed(self):
        r = self._send({"cmd": "cycle_start"}, homed=False)
        self.assertFalse(r["ok"])
        self.assertIn("homed", r["error"].lower())

    def test_mdi_denied_while_running(self):
        r = self._send({"cmd": "mdi", "text": "G0X0"}, interp_state=linuxcnc.INTERP_READING)
        self.assertFalse(r["ok"])

    def test_touchoff_denied_with_eoffset(self):
        r = self._send({"cmd": "set_wcs"}, eoffset_enabled=True)
        self.assertFalse(r["ok"])
        self.assertIn("compensation", r["error"].lower())

    def test_tool_edit_denied_while_running(self):
        r = self._send({"cmd": "save_tool", "tool_number": 1},
                       interp_state=linuxcnc.INTERP_READING)
        self.assertFalse(r["ok"])

    def test_not_armed_denied(self):
        r = self._send({"cmd": "cycle_start"}, armed=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "Not armed")

    def test_machine_on_denied_in_estop(self):
        r = self._send({"cmd": "machine_on"}, estop=True)
        self.assertFalse(r["ok"])


class TestNotOverBlocked(unittest.TestCase):
    """The enforcement must not reject a conforming command on a ready machine.
    Verified at the gateway's policy seam (merge + check_command together) so no
    LinuxCNC-touching handler has to run."""

    def test_ready_machine_passes_every_common_command(self):
        st = gateway._policy_state_from_payload(_payload(), armed=True)
        for cmd in ("mdi", "cycle_start", "jog_cont", "save_tool", "set_wcs",
                    "home", "spindle_forward", "set_feed_override"):
            self.assertIsNone(
                gateway.check_command(cmd, st),
                f"{cmd} wrongly denied on a fully-ready machine")

    def test_jog_stop_never_policy_denied(self):
        # Safety: stopping must pass even fully disarmed / estopped.
        st = gateway._policy_state_from_payload(
            _payload(estop=True), armed=False)
        self.assertIsNone(gateway.check_command("jog_stop", st))


if __name__ == "__main__":
    unittest.main()
