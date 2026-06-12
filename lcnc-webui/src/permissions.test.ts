import { describe, it, expect } from "vitest";
import { applyClientOverlay, type MachinePermissions } from "./permissions";

// The policy itself (which machine state opens which gate) now lives on the
// backend and is tested in lcnc-gateway/test_command_policy.py. These tests
// cover only the FRONTEND's job: the client-local armed + busy overlay.

// What the backend broadcasts for a fully-ready machine (computed armed=true):
// every machine-state gate open except pause/resume (need running/paused).
const MACHINE_READY: MachinePermissions = {
  idle: true, jog: true, override: true, ready: true,
  pause: false, resume: false, step: true, abort: true,
  probe: true, zero: true, safety: true, setup: true,
  armed: true, always: true,
};

describe("applyClientOverlay", () => {
  it("armed + not busy passes the backend gates through", () => {
    const p = applyClientOverlay(MACHINE_READY, true, false);
    expect(p.ready).toBe(true);
    expect(p.probe).toBe(true);
    expect(p.jog).toBe(true);
    expect(p.zero).toBe(true);
    expect(p.always).toBe(true);
  });

  it("disarmed closes everything except always", () => {
    const p = applyClientOverlay(MACHINE_READY, false, false);
    expect(p.ready).toBe(false);
    expect(p.safety).toBe(false);
    expect(p.armed).toBe(false);
    expect(p.abort).toBe(false);
    expect(p.always).toBe(true);
  });

  it("busy closes the busy-subset but not jog/abort", () => {
    const p = applyClientOverlay(MACHINE_READY, true, true);
    // busy-subset gates close
    expect(p.idle).toBe(false);
    expect(p.override).toBe(false);
    expect(p.ready).toBe(false);
    expect(p.probe).toBe(false);
    expect(p.zero).toBe(false);
    expect(p.setup).toBe(false);
    // gates without a busy term stay open
    expect(p.jog).toBe(true);
    expect(p.abort).toBe(true);
  });

  it("absent backend permissions yield all-false except always (safe default)", () => {
    const p = applyClientOverlay(null, true, false);
    expect(p.ready).toBe(false);
    expect(p.jog).toBe(false);
    expect(p.safety).toBe(false);
    expect(p.always).toBe(true);
  });

  it("a backend-closed gate stays closed even when armed and idle", () => {
    const p = applyClientOverlay({ ...MACHINE_READY, ready: false }, true, false);
    expect(p.ready).toBe(false);
    expect(p.jog).toBe(true);
  });
});
