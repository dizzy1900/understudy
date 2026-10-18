/** Starts and stops the in-browser demo, and answers the API calls the controls make while it runs. */
import { resetStream, useStream } from "../store";
import { DemoSim, FAST_PACING, PRESENTER_PACING } from "./sim";

/**
 * Story seconds per wall-clock second. The default is a presenter pace for a live walkthrough of about three
 * minutes; ?pace=fast restores the original pace, where a rejected claim and a promotion land inside ~30 s.
 */
const FAST_SPEED = 2.5;
const PRESENTER_SPEED = 1;
const TICK_MS = 100;

let sim: DemoSim | null = null;
let timer: ReturnType<typeof setInterval> | undefined;
let lastTick = 0;
let paused = false;

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

function fastPace(): boolean {
  try {
    return new URLSearchParams(window.location.search).get("pace") === "fast";
  } catch {
    return false;
  }
}

export function demoSim(): DemoSim | null {
  return sim;
}

export function startDemo(): void {
  if (sim) return;
  const fast = fastPace();
  const speed = fast ? FAST_SPEED : PRESENTER_SPEED;
  sim = new DemoSim((event) => useStream.getState().handle(event), 7, Date.now() / 1000, fast ? FAST_PACING : PRESENTER_PACING);
  paused = false;
  useStream.getState().setDemo(true);
  sim.start();
  lastTick = Date.now();
  timer = setInterval(() => {
    const now = Date.now();
    // Clamp so a tab that slept does not replay minutes of story in one frame.
    const dt = Math.min(1, Math.max(0, (now - lastTick) / 1000));
    lastTick = now;
    if (!paused) sim?.advance(dt * speed);
  }, TICK_MS);
  useStream
    .getState()
    .pushCaption("Demo mode: no live backend here, so a simulated fleet is running in your browser. Numbers are synthetic.", "high");
}

/** Pauses or resumes the simulated fleet (the P key), so a presenter can talk over a frozen moment. */
export function toggleDemoPause(): boolean {
  if (!sim) return false;
  paused = !paused;
  useStream.getState().pushCaption(paused ? "Demo paused. Press P to resume." : "Demo resumed.", "high");
  return true;
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
