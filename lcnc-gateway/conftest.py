"""pytest bootstrap: install the fake linuxcnc before any test imports gateway.

Harmless under plain `unittest` (conftest is a pytest concept and isn't loaded
there) — the gateway test modules also call fake_linuxcnc.install() at their top
so they work under either runner.
"""
import fake_linuxcnc

fake_linuxcnc.install()
