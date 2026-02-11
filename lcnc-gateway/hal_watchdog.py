#!/usr/bin/env python3
"""HAL watchdog subprocess for webui-safety.

Reads JSON pin updates from stdin, toggles HAL pins.
Exits on EOF (gateway crash) — which destroys the HAL component
and stops the heartbeat pin, tripping any downstream watchdog.
"""
import sys
import json
import hal

comp = hal.component("webui-safety")
comp.newpin("heartbeat", hal.HAL_BIT, hal.HAL_OUT)
comp.newpin("connected", hal.HAL_BIT, hal.HAL_OUT)
comp.ready()

print("OK", flush=True)  # signal to gateway that we're ready

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
        if "heartbeat" in msg:
            comp["heartbeat"] = bool(msg["heartbeat"])
        if "connected" in msg:
            comp["connected"] = bool(msg["connected"])
    except Exception:
        pass
