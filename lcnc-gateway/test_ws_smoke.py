"""WS endpoint smoke test — the status fan-out must actually deliver.

Regression guard for the M4 sweep bug: a NameError in status_loop's pre-init
killed every client's fan-out task before its first send. Nothing else caught
it — unit tests never execute the nested loop, and the perf matrix (then)
asserted only on lag windows and safety events, so the gate PASSED while no
client could receive a single status frame.

This test opens a real WebSocket session against the app (TestClient portal)
and pumps heartbeats — each pump yields a pong from the RECEIVE path, so the
drain never blocks even when the fan-out is dead — until a status-family
frame arrives. status_error counts: it proves the loop survived its pre-init
and reached a send, which is exactly the property the regression broke.
"""
import sys
import time
import unittest

import fake_linuxcnc

linuxcnc = fake_linuxcnc.install()  # MUST precede `import gateway`

import msgspec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import gateway  # noqa: E402

STATUS_FAMILY = {"status", "status_delta", "status_error"}


def _decode(message) -> dict:
    # TestClient.receive() yields the raw ASGI message dict.
    if message.get("bytes") is not None:
        return msgspec.msgpack.decode(message["bytes"])
    import json
    return json.loads(message["text"])


class TestStatusFanoutDelivers(unittest.TestCase):
    def test_status_family_frame_arrives(self):
        seen = []
        deadline = time.monotonic() + 15.0
        with TestClient(gateway.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_json({"cmd": "hello", "session": "smoke", "resume_armed": False})
                for _ in range(120):
                    self.assertLess(
                        time.monotonic(), deadline,
                        f"no status-family frame within budget; saw {sorted(set(seen))}")
                    # Pump: the pong reply guarantees the next receive returns
                    # even if the fan-out task is dead.
                    ws.send_json({"cmd": "heartbeat"})
                    msg = _decode(ws.receive())
                    t = msg.get("type", "?")
                    seen.append(t)
                    if t in STATUS_FAMILY:
                        return  # fan-out is alive — the loop survived pre-init
                    time.sleep(0.05)
        self.fail(f"no status-family frame in 120 frames; saw {sorted(set(seen))}")


if __name__ == "__main__":
    unittest.main()
