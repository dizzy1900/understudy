/** The slow pass across the hall that plays while the briefing is read. The camera rig reads this every frame. */
export const sweep = { active: false, startedAt: 0, durationMs: 0 };

/** The widest swing either side of where the camera started, in radians. */
export const SWEEP_SWING = 0.72;
/** How much closer the camera gets at the middle of the pass, as a fraction of its distance. */
export const SWEEP_DOLLY = 0.1;

const MIN_MS = 8000;
const MAX_MS = 40_000;

let finish: (() => void) | null = null;

export function sweepDuration(speechMs: number): number {
  return Math.min(MAX_MS, Math.max(MIN_MS, speechMs));
}

/** Camera offset for progress t in 0..1: out to one side, across to the other, and back to the start with no jump. */
export function sweepPose(t: number): { azimuth: number; distance: number } {
  const x = Math.min(1, Math.max(0, t));
  return {
    azimuth: -SWEEP_SWING * Math.sin(2 * Math.PI * x) * Math.sin(Math.PI * x),
    distance: 1 - SWEEP_DOLLY * Math.sin(Math.PI * x) ** 2,
  };
}

/** Resolves when the pass ends, or when something cancels it. */
export function startSweep(durationMs: number): Promise<void> {
  cancelSweep();
  sweep.active = true;
  sweep.startedAt = performance.now();
  sweep.durationMs = sweepDuration(durationMs);
  return new Promise((resolve) => {
    finish = resolve;
  });
}

export function endSweep(): void {
  sweep.active = false;
  const done = finish;
  finish = null;
  done?.();
}

export const cancelSweep = endSweep;
