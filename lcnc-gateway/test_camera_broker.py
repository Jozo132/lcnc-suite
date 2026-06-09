"""M5 / P3 (+ review #3/#4): the single-producer CameraBroker, now a standalone module
with injected device I/O — tested in isolation (no gateway / fake_linuxcnc). The device
opens once regardless of viewer count (including concurrent first subscribers), survives
viewers leaving while others remain, reuses the producer on a re-subscribe within the
grace window, and tears down via an awaitable aclose() that releases exactly once."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from camera_broker import CameraBroker  # noqa: E402


class TestCameraBroker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.init_calls = 0
        self.release_calls = 0

    def _make(self) -> CameraBroker:
        def _init():
            self.init_calls += 1
            return True

        def _rel():
            self.release_calls += 1

        return CameraBroker(
            grab_jpeg=lambda: b"\xff\xd8jpeg",
            device_init=_init,
            device_release=_rel,
            register_bg_task=lambda t: t,  # identity — don't touch any real registry
        )

    async def test_one_producer_for_many_subscribers(self):
        b = self._make()
        self.assertTrue(await b.subscribe())
        self.assertTrue(await b.subscribe())
        self.assertTrue(await b.subscribe())
        self.assertEqual(self.init_calls, 1)   # device opened ONCE for 3 viewers
        self.assertEqual(b._subscribers, 3)
        self.assertIsNotNone(b._task)
        b.unsubscribe()
        b.unsubscribe()
        self.assertEqual(b._subscribers, 1)
        self.assertIsNone(b._stop_handle)
        await b.aclose()

    async def test_concurrent_first_subscribers_start_one_producer(self):
        b = self._make()
        results = await asyncio.gather(b.subscribe(), b.subscribe(), b.subscribe())
        self.assertEqual(results, [True, True, True])
        self.assertEqual(self.init_calls, 1)   # ONE device-open, not three (review #3)
        self.assertEqual(b._subscribers, 3)
        await b.aclose()

    async def test_grace_stop_releases_once(self):
        b = self._make()
        await b.subscribe()
        b.unsubscribe()
        self.assertEqual(b._subscribers, 0)
        self.assertIsNotNone(b._stop_handle)
        b._stop_handle.cancel()                # drive the stop directly (skip the 2 s wait)
        b._stop_handle = None
        await b._grace_stop()
        self.assertIsNone(b._task)
        self.assertEqual(self.release_calls, 1)

    async def test_resubscribe_within_grace_reuses_producer(self):
        b = self._make()
        await b.subscribe()
        producer = b._task
        b.unsubscribe()
        self.assertIsNotNone(b._stop_handle)
        self.assertTrue(await b.subscribe())   # back within the grace window
        self.assertIsNone(b._stop_handle)
        self.assertIs(b._task, producer)       # same producer reused
        self.assertEqual(self.init_calls, 1)   # device NOT re-opened
        await b.aclose()

    async def test_aclose_stops_and_releases(self):
        b = self._make()
        await b.subscribe()
        self.assertIsNotNone(b._task)
        await b.aclose()                       # review #4: awaitable, releases once
        self.assertIsNone(b._task)
        self.assertEqual(self.release_calls, 1)


if __name__ == "__main__":
    unittest.main()
