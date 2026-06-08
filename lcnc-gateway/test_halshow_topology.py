"""F5 / P5: the HAL topology is built once (3 halcmd subprocesses) and cached for
the gateway lifetime, not rebuilt per subscriber. Invalidation forces a rebuild."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fake_linuxcnc  # noqa: E402

fake_linuxcnc.install()  # MUST precede `import gateway`
import gateway  # noqa: E402


class TestHalshowTopologyCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway._halshow_topology_cache = None
        self.calls = {"pins": 0, "signals": 0, "params": 0}
        self._orig = (gateway._parse_hal_pins, gateway._parse_hal_signals, gateway._parse_hal_params)

        def _pins():
            self.calls["pins"] += 1
            return [{"name": "x"}]

        def _signals():
            self.calls["signals"] += 1
            return [{"name": "s"}]

        def _params():
            self.calls["params"] += 1
            return [{"name": "p"}]

        gateway._parse_hal_pins = _pins
        gateway._parse_hal_signals = _signals
        gateway._parse_hal_params = _params

    def tearDown(self):
        gateway._parse_hal_pins, gateway._parse_hal_signals, gateway._parse_hal_params = self._orig
        gateway._halshow_topology_cache = None

    async def test_built_once_then_cached(self):
        t1 = await gateway._halshow_topology()
        t2 = await gateway._halshow_topology()
        t3 = await gateway._halshow_topology()
        self.assertEqual(t1["pins"], [{"name": "x"}])
        self.assertIs(t1, t2)  # same cached object
        self.assertIs(t2, t3)
        self.assertEqual(self.calls, {"pins": 1, "signals": 1, "params": 1})  # 3 subprocs ONCE

    async def test_invalidate_forces_rebuild(self):
        await gateway._halshow_topology()
        gateway._invalidate_halshow_topology()
        await gateway._halshow_topology()
        self.assertEqual(self.calls, {"pins": 2, "signals": 2, "params": 2})  # rebuilt after invalidation


if __name__ == "__main__":
    unittest.main()
