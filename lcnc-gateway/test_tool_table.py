"""Unit tests for tool_table — the pure tool.tbl + tool_library helpers extracted
from gateway.py (issue #33). No linuxcnc/gateway import; runs anywhere."""
import os
import tempfile
import unittest

from tool_table import parse_tool_table, write_tool_table, _merge_tool_data


class TestToolTableRoundTrip(unittest.TestCase):
    def _roundtrip(self, tools):
        fd, path = tempfile.mkstemp(suffix=".tbl")
        os.close(fd)
        try:
            write_tool_table(path, tools)
            return parse_tool_table(path)
        finally:
            os.unlink(path)

    def test_write_then_parse_preserves_core_fields(self):
        out = self._roundtrip([{"T": 1, "P": 2, "Z": -1.5, "D": 6.0, "remark": "endmill"}])
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertEqual((t["T"], t["P"]), (1, 2))
        self.assertAlmostEqual(t["Z"], -1.5, places=5)
        self.assertAlmostEqual(t["D"], 6.0, places=5)
        self.assertEqual(t["remark"], "endmill")

    def test_sorted_by_tool_number(self):
        out = self._roundtrip([{"T": 5, "P": 5, "Z": 0, "D": 0, "remark": ""},
                               {"T": 1, "P": 1, "Z": 0, "D": 0, "remark": ""}])
        self.assertEqual([t["T"] for t in out], [1, 5])

    def test_defaults_for_missing_fields(self):
        out = self._roundtrip([{"T": 3, "P": 3}])
        self.assertEqual(out[0]["Z"], 0.0)
        self.assertEqual(out[0]["D"], 0.0)

    def test_parse_skips_comments_and_blanks(self):
        fd, path = tempfile.mkstemp(suffix=".tbl")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write(";header\n\nT1 P1 Z+000000.000000  D+00000.000000\n# c\n")
            out = parse_tool_table(path)
        finally:
            os.unlink(path)
        self.assertEqual([t["T"] for t in out], [1])


class TestMergeToolData(unittest.TestCase):
    def test_merges_library_metadata(self):
        merged = _merge_tool_data(
            [{"T": 1, "P": 1, "Z": -1.0, "D": 6.0, "remark": "rough"}],
            {"1": {"type": "endmill", "flutes": 4, "material": "carbide"}})
        self.assertEqual(merged[0]["type"], "endmill")
        self.assertEqual(merged[0]["flutes"], 4)
        self.assertEqual(merged[0]["material"], "carbide")

    def test_description_falls_back_to_remark(self):
        merged = _merge_tool_data([{"T": 1, "P": 1, "Z": 0, "D": 0, "remark": "my tool"}], {})
        self.assertEqual(merged[0]["description"], "my tool")

    def test_missing_meta_fields_default(self):
        merged = _merge_tool_data([{"T": 1, "P": 1, "Z": 0, "D": 0, "remark": ""}], {})
        self.assertIsNone(merged[0]["flutes"])   # generic field -> None
        self.assertEqual(merged[0]["type"], "")  # type -> ""


if __name__ == "__main__":
    unittest.main()
