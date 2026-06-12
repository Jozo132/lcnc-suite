"""R6 / P2.2: adaptive-poll active decision. A CONFIDENTLY idle machine selects the
idle rate; motion / AUTO / MDI / tool-change / not-in-position select the active rate;
and — the safety point — unknown (None) key fields or a stale reader select the active
rate too, so we never coast at the idle rate on uncertainty."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fake_linuxcnc  # noqa: E402

linuxcnc = fake_linuxcnc.install()  # MUST precede `import gateway`
import gateway  # noqa: E402


class _St:
    """Minimal stand-in for the shared status snapshot."""
    def __init__(self, interp=None, mode=None, vel=0.0, inpos=True, toolchg=False):
        self.interp_state = linuxcnc.INTERP_IDLE if interp is None else interp
        self.task_mode = linuxcnc.MODE_MANUAL if mode is None else mode
        self.current_vel = vel
        self.inpos = inpos
        self.tool_change_requested = toolchg


def active(st, reader_stale=False):
    return gateway._poll_is_active(st, reader_stale)


class TestAdaptivePollActive(unittest.TestCase):
    # ---- confidently idle → idle rate ----
    def test_confidently_idle_is_inactive(self):
        self.assertFalse(active(_St()))

    # ---- doing something → active ----
    def test_motion_is_active(self):
        self.assertTrue(active(_St(vel=5.0)))

    def test_auto_mode_is_active(self):
        self.assertTrue(active(_St(mode=linuxcnc.MODE_AUTO)))

    def test_mdi_mode_is_active(self):
        self.assertTrue(active(_St(mode=linuxcnc.MODE_MDI)))

    def test_interp_not_idle_is_active(self):
        self.assertTrue(active(_St(interp=linuxcnc.INTERP_READING)))

    def test_tool_change_is_active(self):
        self.assertTrue(active(_St(toolchg=True)))

    def test_not_in_position_is_active(self):
        self.assertTrue(active(_St(inpos=False)))

    # ---- unknown / stale → active (the safety guard) ----
    def test_unknown_interp_state_is_active(self):
        self.assertTrue(active(_unknown("interp_state")))

    def test_unknown_task_mode_is_active(self):
        self.assertTrue(active(_unknown("task_mode")))

    def test_unknown_current_vel_is_active(self):
        self.assertTrue(active(_unknown("current_vel")))

    def test_unknown_inpos_is_active(self):
        self.assertTrue(active(_unknown("inpos")))

    def test_stale_reader_is_active(self):
        self.assertTrue(active(_St(), reader_stale=True))

    def test_no_status_yet_is_active(self):
        self.assertTrue(active(None))


def _unknown(field):
    """An otherwise-idle status with one key field set to None (unknown)."""
    st = _St()
    setattr(st, field, None)
    return st


if __name__ == "__main__":
    unittest.main()
