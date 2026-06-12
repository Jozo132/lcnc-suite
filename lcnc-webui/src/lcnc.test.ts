import { describe, it, expect } from "vitest";
import { isQueueSafe } from "./lcnc";

describe("isQueueSafe (issue #18 — what may queue across a reconnect)", () => {
  it("allows read-only get_* commands", () => {
    expect(isQueueSafe("get_tool_table")).toBe(true);
    expect(isQueueSafe("get_probe_results")).toBe(true);
    expect(isQueueSafe("get_wcs_table")).toBe(true);
  });

  it("allows telemetry / visibility commands", () => {
    for (const c of ["heartbeat", "client_diag", "timing_log", "tab_visibility", "halshow_live", "safety_trip_ack"]) {
      expect(isQueueSafe(c)).toBe(true);
    }
  });

  it("DROPS motion / mutation commands", () => {
    for (const c of [
      "jog_cont", "jog_incr", "mdi", "cycle_start", "auto_run",
      "spindle_forward", "set_feed_override", "tool_change",
      "add_tool", "delete_tool", "renumber_tool", "load_file", "machine_on",
    ]) {
      expect(isQueueSafe(c)).toBe(false);
    }
  });
});
