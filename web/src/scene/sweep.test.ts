import { describe, expect, it } from "vitest";

import { SWEEP_DOLLY, SWEEP_SWING, sweepDuration, sweepPose } from "./sweep";

describe("camera sweep", () => {
  it("starts and ends where the camera was, so there is no jump", () => {
    for (const t of [0, 1]) {
      const pose = sweepPose(t);
      expect(pose.azimuth).toBeCloseTo(0, 10);
      expect(pose.distance).toBeCloseTo(1, 10);
    }
  });

  it("passes across both sides of the hall within the swing", () => {
    const samples = Array.from({ length: 201 }, (_, i) => sweepPose(i / 200));
    const angles = samples.map((s) => s.azimuth);
    expect(Math.min(...angles)).toBeLessThan(-SWEEP_SWING * 0.6);
    expect(Math.max(...angles)).toBeGreaterThan(SWEEP_SWING * 0.6);
    expect(Math.max(...angles.map(Math.abs))).toBeLessThanOrEqual(SWEEP_SWING);
    expect(Math.min(...samples.map((s) => s.distance))).toBeGreaterThanOrEqual(1 - SWEEP_DOLLY);
  });

  it("moves slowly at both ends", () => {
    const step = 0.01;
    expect(Math.abs(sweepPose(step).azimuth)).toBeLessThan(0.002);
    expect(Math.abs(sweepPose(1 - step).azimuth)).toBeLessThan(0.002);
  });

  it("clamps progress outside the pass", () => {
    expect(sweepPose(-1)).toEqual(sweepPose(0));
    expect(sweepPose(2)).toEqual(sweepPose(1));
  });

  it("keeps the pass between 8 and 40 seconds", () => {
    expect(sweepDuration(1000)).toBe(8000);
    expect(sweepDuration(25_000)).toBe(25_000);
    expect(sweepDuration(90_000)).toBe(40_000);
  });
});
