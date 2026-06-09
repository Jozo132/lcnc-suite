"""F4 / P3 + review #3/#4: the single-producer CameraBroker. The device opens once
regardless of viewer count — including under *concurrent* first subscribers (review #3)
— survives viewers leaving while others remain, reuses the producer on a re-subscribe
within the grace window, and tears down deterministically via an awaitable aclose()
that releases the device exactly once (review #4)."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fake_linuxcnc  # noqa: E402

fake_linuxcnc.install()  # MUST precede `import gateway`
import gateway  # noqa: E402


class TestCameraBroker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.init_calls = 0
        self.release_calls = 0
        self._orig = (
            gateway._camera_init,
            gateway._camera_grab_jpeg,
            gateway._camera_release,
            gateway.register_bg_task,
        )

        def _init():
            self.init_calls += 1
            return True

        def _rel():
            self.release_calls += 1

        gateway._camera_init = _init
        gateway._camera_grab_jpeg = lambda *a, **k: b"\xff\xd8jpeg"
        gateway._camera_release = _rel
        gateway.register_bg_task = lambda t: t  # don't touch the real _bg_tasks set

    def tearDown(self):
        (
            gateway._camera_init,
            gateway._camera_grab_jpeg,
            gateway._camera_release,
            gateway.register_bg_task,
        ) = self._orig

    async def test_one_producer_for_many_subscribers(self):
        b = gateway._CameraBroker()
        self.assertTrue(await b.subscribe())
        self.assertTrue(await b.subscribe())
        self.assertTrue(await b.subscribe())
        self.assertEqual(self.init_calls, 1)   # device opened ONCE for 3 viewers
        self.assertEqual(b._subscribers, 3)
        self.assertIsNotNone(b._task)
        b.unsubscribe()
        b.unsubscribe()
        self.assertEqual(b._subscribers, 1)
        self.assertIsNone(b._stop_handle)      # producer stays, no stop scheduled
        await b.aclose()

    async def test_concurrent_first_subscribers_start_one_producer(self):
        # review #3: two/three simultaneous first subscribers must not each open the
        # device + start a producer. The lock serializes the start transition.
        b = gateway._CameraBroker()
        results = await asyncio.gather(b.subscribe(), b.subscribe(), b.subscribe())
        self.assertEqual(results, [True, True, True])
        self.assertEqual(self.init_calls, 1)   # ONE device-open, not three
        self.assertEqual(b._subscribers, 3)
        self.assertIsNotNone(b._task)
        await b.aclose()

    async def test_grace_stop_releases_once(self):
        b = gateway._CameraBroker()
        await b.subscribe()
        b.unsubscribe()
        self.assertEqual(b._subscribers, 0)
        self.assertIsNotNone(b._stop_handle)   # grace timer armed
        b._stop_handle.cancel()                # drive the stop directly (skip the 2 s wait)
        b._stop_handle = None
        await b._grace_stop()
        self.assertIsNone(b._task)
        self.assertEqual(self.release_calls, 1)

    async def test_resubscribe_within_grace_reuses_producer(self):
        b = gateway._CameraBroker()
        await b.subscribe()
        producer = b._task
        b.unsubscribe()
        self.assertIsNotNone(b._stop_handle)
        self.assertTrue(await b.subscribe())   # back within the grace window
        self.assertIsNone(b._stop_handle)      # grace stop cancelled
        self.assertIs(b._task, producer)       # same producer reused
        self.assertEqual(self.init_calls, 1)   # device NOT re-opened
        await b.aclose()

    async def test_aclose_stops_and_releases(self):
        # review #4: shutdown is awaitable and releases exactly once.
        b = gateway._CameraBroker()
        await b.subscribe()
        self.assertIsNotNone(b._task)
        await b.aclose()
        self.assertIsNone(b._task)
        self.assertEqual(self.release_calls, 1)


if __name__ == "__main__":
    unittest.main()
