"""F4 / P3: the single-producer CameraBroker. The device must open once regardless
of viewer count, survive viewers leaving while others remain, and a re-subscribe
within the grace window must reuse the running producer (not re-open the device)."""
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

    @staticmethod
    async def _cancel(task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_one_producer_for_many_subscribers(self):
        b = gateway._CameraBroker()
        self.assertTrue(await b.subscribe())
        self.assertTrue(await b.subscribe())
        self.assertTrue(await b.subscribe())
        self.assertEqual(self.init_calls, 1)   # device opened ONCE for 3 viewers
        self.assertEqual(b._subscribers, 3)
        self.assertIsNotNone(b._task)
        producer = b._task
        # two leave — producer stays, no stop scheduled
        b.unsubscribe()
        b.unsubscribe()
        self.assertEqual(b._subscribers, 1)
        self.assertIsNone(b._stop_handle)
        self.assertFalse(producer.done())
        await self._cancel(producer)

    async def test_last_unsubscribe_schedules_stop_then_releases(self):
        b = gateway._CameraBroker()
        await b.subscribe()
        producer = b._task
        b.unsubscribe()
        self.assertEqual(b._subscribers, 0)
        self.assertIsNotNone(b._stop_handle)   # grace stop armed
        b._stop()                              # fire it directly (skip the 2 s wait)
        self.assertIsNone(b._task)             # producer dropped
        await asyncio.sleep(0)                 # let the offloaded release task run
        await asyncio.sleep(0.01)
        self.assertEqual(self.release_calls, 1)
        await self._cancel(producer)

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
        await self._cancel(b._task)


if __name__ == "__main__":
    unittest.main()
