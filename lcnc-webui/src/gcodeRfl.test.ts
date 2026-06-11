import { describe, it, expect } from "vitest";
import { scanToolchangesBefore } from "./gcodeRfl";

describe("scanToolchangesBefore (RFL × M600 guard)", () => {
  it("finds a same-line T + M600 before the start line", () => {
    const text = "G21 G90\nT5 M600\nG0 X0\nG1 X10\n";
    expect(scanToolchangesBefore(text, 4)).toEqual({ count: 1, lastTool: 5, lastLine: 2 });
  });

  it("uses a T word from an earlier line (modal prepare)", () => {
    const text = "T13\nG4 P1\nM600\nG0 X0\n";
    expect(scanToolchangesBefore(text, 4)).toEqual({ count: 1, lastTool: 13, lastLine: 3 });
  });

  it("excludes a toolchange at or after the start line", () => {
    const text = "G21\nG0 X0\nT5 M600\nG1 X10\n";
    expect(scanToolchangesBefore(text, 3).count).toBe(0);
    expect(scanToolchangesBefore(text, 4).count).toBe(1);
  });

  it("ignores toolchanges inside comments", () => {
    const text = "; T5 M600 in a comment\n(M600 here too)\nG0 X0 (T9 M6)\nG1 X1\n";
    expect(scanToolchangesBefore(text, 5).count).toBe(0);
  });

  it("does not confuse M60/M66/M61 with M6, nor M6000 with M600", () => {
    const text = "M60\nM66 E0 L0\nM61 Q0\nM6000\nG0 X0\n";
    expect(scanToolchangesBefore(text, 6).count).toBe(0);
  });

  it("matches plain M6 and M06 with leading-zero T", () => {
    const text = "T07 M06\nG0 X0\n";
    expect(scanToolchangesBefore(text, 3)).toEqual({ count: 1, lastTool: 7, lastLine: 1 });
  });

  it("counts M601 as a toolchange", () => {
    const text = "T2\nM601\nG0 X0\n";
    expect(scanToolchangesBefore(text, 4)).toEqual({ count: 1, lastTool: 2, lastLine: 2 });
  });

  it("counts multiple toolchanges and reports the LAST tool", () => {
    const text = "T1 M600\nG1 X1\nT2 M600\nG1 X2\nG1 X3\n";
    const r = scanToolchangesBefore(text, 5);
    expect(r.count).toBe(2);
    expect(r.lastTool).toBe(2);
    expect(r.lastLine).toBe(3);
  });

  it("reports unknown tool (null) when M600 has no preceding T", () => {
    const text = "G21\nM600\nG0 X0\n";
    const r = scanToolchangesBefore(text, 4);
    expect(r.count).toBe(1);
    expect(r.lastTool).toBeNull();
  });

  it("returns empty for start line 1 or empty text", () => {
    expect(scanToolchangesBefore("", 10).count).toBe(0);
    expect(scanToolchangesBefore("T5 M600\n", 1).count).toBe(0);
  });

  it("handles T0 M600 (spindle unload) as tool 0", () => {
    const text = "T0 M600\nG0 X0\n";
    const r = scanToolchangesBefore(text, 3);
    expect(r.count).toBe(1);
    expect(r.lastTool).toBe(0);
  });
});
