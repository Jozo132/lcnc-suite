"""Unit tests for settings_store (issue #33). Pure — a temp file + an injected
INI-key callable; no linuxcnc/gateway import."""
import os
import tempfile
import unittest
from pathlib import Path

from settings_store import SettingsStore, VALID_SECTIONS


class TestSettingsStore(unittest.TestCase):
    def setUp(self):
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(p)  # start with no file on disk
        self.path = Path(p)
        self.store = SettingsStore(self.path, lambda: "ini-A")

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_save_then_load_section(self):
        self.store.save_section("macros", {"x": 1})
        self.assertEqual(self.store.load(), {"macros": {"x": 1}})

    def test_version_bumps_on_save_and_reset(self):
        v0 = self.store.version
        self.store.save_section("viewer", {})
        self.assertEqual(self.store.version, v0 + 1)
        self.store.reset()
        self.assertEqual(self.store.version, v0 + 2)

    def test_writes_preserve_other_inis(self):
        # The per-INI merge: writing under one INI key must not clobber another's.
        self.store.save_section("macros", {"a": 1})                    # ini-A
        SettingsStore(self.path, lambda: "ini-B").save_section("macros", {"b": 2})
        self.assertEqual(SettingsStore(self.path, lambda: "ini-A").load(), {"macros": {"a": 1}})
        self.assertEqual(SettingsStore(self.path, lambda: "ini-B").load(), {"macros": {"b": 2}})

    def test_reset_clears_only_current_ini(self):
        self.store.save_section("macros", {"a": 1})
        self.store.reset()
        self.assertEqual(self.store.load(), {})

    def test_refuses_to_clobber_unparseable_file(self):
        self.path.write_text("{ not valid json")
        errors = []
        store = SettingsStore(self.path, lambda: "ini-A", on_load_error=errors.append)
        with self.assertRaises(RuntimeError):
            store.save_section("macros", {"x": 1})
        self.assertEqual(len(errors), 1)  # on_load_error fired once

    def test_valid_sections(self):
        self.assertIn("macros", VALID_SECTIONS)
        self.assertIn("machine", VALID_SECTIONS)


if __name__ == "__main__":
    unittest.main()
