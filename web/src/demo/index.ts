/** Starts and stops the in-browser demo, and answers the API calls the controls make while it runs. */
import { resetStream, useStream } from "../store";
import { DemoSim } from "./sim";

/** Story seconds per wall-clock second: a first incident in ~2 s, a rejected claim and a promotion inside ~30 s. */
export const DEMO_SPEED = 2.5;
const TICK_MS = 100;

let sim: DemoSim | null = null;
let timer: ReturnType<typeof setInterval> | undefined;
let lastTick = 0;

function param(name: string): boolean {
  try {
    return new URLSearchParams(window.location.search).has(name);
  } catch {
    return false;
  }
}

/** ?demo=1 forces the demo on; ?live=1 keeps it off even when the backend never answers. */
export function demoForced(): boolean {
  return param("demo");
}

export function liveOnly(): boolean {
  return param("live");
}

export function demoSim(): DemoSim | null {
  return sim;
}

export function startDemo(): void {
  if (sim) return;
  sim = new DemoSim((event) => useStream.getState().handle(event));
  useStream.getState().setDemo(true);
  sim.start();
  lastTick = Date.now();
  timer = setInterval(() => {
    const now = Date.now();
    // Clamp so a tab that slept does not replay minutes of story in one frame.
    const dt = Math.min(1, Math.max(0, (now - lastTick) / 1000));
    lastTick = now;
    sim?.advance(dt * DEMO_SPEED);
  }, TICK_MS);
  useStream
    .getState()
    .pushCaption("Demo mode: no live backend here, so a simulated fleet is running in your browser. Numbers are synthetic.", "high");
}

/** Stops the demo. With `reset`, clears what it put in the store so a live stream starts clean. */
export function stopDemo(reset = false): void {
  if (!sim) return;
  clearInterval(timer);
  timer = undefined;
  sim = null;
  if (reset) resetStream();
  useStream.getState().setDemo(false);
}
