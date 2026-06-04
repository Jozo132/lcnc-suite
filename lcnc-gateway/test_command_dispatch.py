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


class _RecordingCmd:
    """A CMD spy: records every call so a happy-path test can assert the handler
    reached LinuxCNC with the parsed arguments. wait_complete() returns 0
    (success), matching the real binding."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return 0
        return record

    def args_of(self, name):
        """Positional args of the first recorded call to `name`, or None."""
        for n, a, _k in self.calls:
            if n == name:
                return a
        return None


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


class TestHandlerExecution(unittest.TestCase):
    """Happy path: a valid command on a ready machine runs the REAL handler body
    to completion and reaches CMD.* with the parsed arguments — not just the
    policy seam. set_mode/_cmd_blocking are fire-and-forget here (no wait loop),
    so a recording CMD spy + fake STAT suffice; no stateful linkage needed."""

    def setUp(self):
        gateway.lcnc_connected = True
        gateway.STAT = linuxcnc.stat()
        self.cmd = _RecordingCmd()
        gateway.CMD = self.cmd

    def _send(self, msg):
        gateway._shared_status = _payload()   # ready -> passes policy
        return _run(gateway.handle_command(msg, True))

    def test_jog_cont_reaches_cmd_jog_with_parsed_args(self):
        r = self._send({"cmd": "jog_cont", "axis": 2, "vel": 3.5})
        self.assertTrue(r["ok"])
        args = self.cmd.args_of("jog")
        self.assertIsNotNone(args, "CMD.jog was never called")
        self.assertEqual(args[0], linuxcnc.JOG_CONTINUOUS)
        self.assertEqual(args[2], 2)      # axis (jf is args[1])
        self.assertEqual(args[3], 3.5)    # velocity

    def test_mdi_reaches_cmd_mdi_with_text(self):
        r = self._send({"cmd": "mdi", "text": "G0 X1"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.cmd.args_of("mdi"), ("G0 X1",))

    def test_home_reaches_cmd_home_with_joint(self):
        r = self._send({"cmd": "home", "joint": 2})
        self.assertTrue(r["ok"])
        self.assertEqual(self.cmd.args_of("home"), (2,))

    def test_mode_switched_before_motion(self):
        # set_mode runs first: CMD.mode(MODE_MANUAL) is recorded before CMD.jog.
        self._send({"cmd": "jog_cont", "axis": 0, "vel": 1.0})
        names = [n for n, _a, _k in self.cmd.calls]
        self.assertIn("mode", names)
        self.assertIn("jog", names)
        self.assertLess(names.index("mode"), names.index("jog"))


class TestPayloadValidation(unittest.TestCase):
    """Bad numeric payloads return a bounded {ok:false} via the dispatch, rather
    than crashing the socket or (the #27 bug) silently flowing a non-finite
    value into the machine. Runs on a ready machine so the policy passes and the
    handler reaches its casts; rejection happens before any CMD.* call."""

    def setUp(self):
        gateway.lcnc_connected = True
        # Some handlers call STAT.poll() directly (e.g. reject_if_auto_running),
        # not the None-safe safe_get — give them a fake stat with a no-op poll().
        gateway.STAT = linuxcnc.stat()

    def _send(self, msg):
        gateway._shared_status = _payload()   # ready -> passes policy
        return _run(gateway.handle_command(msg, True))

    def _assert_validation_rejection(self, r, contains):
        # Guard against passing for the wrong reason: the reply must be a real
        # ValueError from the validation layer (not an incidental AttributeError/
        # crash) AND carry the specific reason.
        self.assertFalse(r["ok"])
        err = r["error"].lower()
        self.assertIn("valueerror", err, f"not a validation rejection: {r['error']!r}")
        self.assertIn(contains, err, f"wrong reason: {r['error']!r}")

    def test_jog_garbage_axis_rejected(self):
        r = self._send({"cmd": "jog_cont", "axis": "x", "vel": 1.0})
        self._assert_validation_rejection(r, "convert")  # float('x') fails

    def test_jog_negative_axis_rejected(self):
        r = self._send({"cmd": "jog_cont", "axis": -1, "vel": 1.0})
        self._assert_validation_rejection(r, "minimum")  # finite_int lo=0

    def test_jog_non_finite_velocity_rejected(self):
        # The motion-value guard: Infinity must not reach CMD.jog.
        r = self._send({"cmd": "jog_cont", "axis": 0, "vel": "Infinity"})
        self._assert_validation_rejection(r, "non-finite")

    def test_add_tool_garbage_number_rejected(self):
        r = self._send({"cmd": "add_tool", "tool_number": "abc"})
        self._assert_validation_rejection(r, "convert")

    def test_add_tool_negative_number_rejected(self):
        r = self._send({"cmd": "add_tool", "tool_number": -5})
        self._assert_validation_rejection(r, "minimum")


if __name__ == "__main__":
    unittest.main()
