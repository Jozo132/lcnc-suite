import { describe, it, expect } from "vitest";
import { extractParams, syncMacroParams } from "./macroParams";

describe("extractParams", () => {
  it("extracts unique placeholder names in command order", () => {
    expect(extractParams("G0 Z{depth} F{feed} then {depth}")).toEqual(["depth", "feed"]);
  });
  it("returns [] when there are no placeholders", () => {
    expect(extractParams("G0 X0 Y0")).toEqual([]);
  });
});

describe("syncMacroParams", () => {
  it("creates a default param for each new placeholder", () => {
    expect(syncMacroParams("G0 Z{depth}", [])).toEqual([
      { name: "depth", label: "depth", default: "" },
    ]);
  });

  it("preserves a kept param's edits by reusing the object reference", () => {
    // This is the behaviour the old computed side-effect protected: editing a
    // param then changing the command must not revert the edit.
    const existing = [{ name: "depth", label: "Depth (mm)", default: "5" }];
    const result = syncMacroParams("G0 Z{depth} F{feed}", existing);
    expect(result[0]).toBe(existing[0]);           // same reference -> edits survive
    expect(result[1]).toEqual({ name: "feed", label: "feed", default: "" });
  });

  it("drops a param no longer referenced by the command", () => {
    const existing = [
      { name: "depth", label: "Depth", default: "5" },
      { name: "feed", label: "Feed", default: "100" },
    ];
    expect(syncMacroParams("G0 Z{depth}", existing).map(p => p.name)).toEqual(["depth"]);
  });

  it("orders by the command, not by the existing array", () => {
    const existing = [
      { name: "feed", label: "Feed", default: "" },
      { name: "depth", label: "Depth", default: "" },
    ];
    expect(syncMacroParams("{depth} {feed}", existing).map(p => p.name)).toEqual(["depth", "feed"]);
  });
});
