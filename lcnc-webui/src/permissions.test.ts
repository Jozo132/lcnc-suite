import { describe, it, expect } from "vitest";
import { evaluatePermissions, type MachineState } from "./permissions";

// A fully-ready machine: armed, estop cleared, enabled, homed, idle, no eoffset.
const READY: MachineState = {
  armed: true,
  isEstop: false,
  isEnabled: true,
  isHomed: true,
  isIdle: true,
  isRunning: false,
  isPaused: false,
  busy: false,
  hasFile: true,
  eoffsetEnabled: false,
};

const state = (over: Partial<MachineState>): MachineState => ({ ...READY, ...over });

describe("evaluatePermissions", () => {
  it("a fully-ready machine opens ready/probe/jog/zero", () => {
    const p = evaluatePermissions(READY);
    expect(p.ready).toBe(true);
    expect(p.probe).toBe(true);
    expect(p.jog).toBe(true);
    expect(p.zero).toBe(true);
    expect(p.always).toBe(true);
  });

  it("disarmed closes everything except always", () => {
    const p = evaluatePermissions(state({ armed: false }));
    expect(p.ready).toBe(false);
    expect(p.safety).toBe(false);
    expect(p.armed).toBe(false);
    expect(p.always).toBe(true);
  });

  it("estop keeps armed but closes safety and base gates", () => {
    const p = evaluatePermissions(state({ isEstop: true }));
    expect(p.armed).toBe(true);
    expect(p.safety).toBe(false);
    expect(p.ready).toBe(false);
    expect(p.abort).toBe(false);
  });

  it("not homed closes ready/jog/probe but keeps idle/zero", () => {
    const p = evaluatePermissions(state({ isHomed: false }));
    expect(p.ready).toBe(false);
    expect(p.jog).toBe(false);
    expect(p.probe).toBe(false);
    expect(p.idle).toBe(true);
    expect(p.zero).toBe(true);
  });

  it("active eoffset blocks probe and zero (contamination guard)", () => {
    const p = evaluatePermissions(state({ eoffsetEnabled: true }));
    expect(p.probe).toBe(false);
    expect(p.zero).toBe(false);
    expect(p.ready).toBe(true); // ready does not depend on eoffset
  });

  it("running allows pause/override but not ready/jog", () => {
    const p = evaluatePermissions(state({ isIdle: false, isRunning: true, busy: true }));
    expect(p.pause).toBe(true);
    expect(p.override).toBe(false); // override has a !busy gate
    expect(p.ready).toBe(false);
    expect(p.jog).toBe(false);
  });

  it("paused allows resume and step", () => {
    const p = evaluatePermissions(state({ isIdle: false, isRunning: true, isPaused: true }));
    expect(p.resume).toBe(true);
    expect(p.step).toBe(true);
    expect(p.pause).toBe(false);
  });
});
