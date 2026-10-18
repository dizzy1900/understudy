import { api, wsUrl } from "./api";
import { demoForced, demoSim, liveOnly, startDemo, stopDemo } from "./demo";
import { useStream } from "./store";
import type { StreamEvent } from "./types";

const BACKOFF_START_MS = 500;
const BACKOFF_CAP_MS = 8000;
const HYDRATE_WINDOW_S = 150;
/** With no live stream by then, the in-browser demo takes over so the page is never dead. */
export const DEMO_AFTER_MS = 1500;
/** While the demo runs, keep looking for a live backend, but quietly. */
const DEMO_RETRY_MS = 30_000;

/** Opens the event stream and keeps it open. Returns a function that closes it. */
export function connect(): () => void {
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let closed = false;
  let delay = BACKOFF_START_MS;
  let hydrate = false;

  const store = () => useStream.getState();

  const schedule = () => {
    if (closed) return;
    const wait = demoSim() ? DEMO_RETRY_MS : delay + Math.random() * delay * 0.3;
    delay = Math.min(BACKOFF_CAP_MS, delay * 2);
    timer = setTimeout(open, wait);
  };

  const open = () => {
    if (closed) return;
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl());
    } catch {
      schedule();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      delay = BACKOFF_START_MS;
      // A real backend answered: drop the simulated fleet and show the live one.
      if (demoSim()) stopDemo(true);
      hydrate = true;
      store().setConnected(true);
      // The socket sends a fleet_snapshot and a ledger on connect; the ladder is a cheap backstop.
      api.ladder().then((body) => store().setLadder(body.ladder)).catch(() => undefined);
    };
    ws.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as StreamEvent;
        store().handle(event);
        if (hydrate && event.type === "fleet_snapshot") {
          // Seat the agents that were already working, and catch up on anything missed while disconnected.
          hydrate = false;
          api
            .history(event.ts - HYDRATE_WINDOW_S)
            .then((body) => store().hydrate(body.events ?? []))
            .catch(() => undefined);
        }
      } catch (error) {
        console.warn("bad event", error);
      }
    };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      store().setConnected(false);
      schedule();
    };
    ws.onerror = () => ws.close();
  };

  // Deferred so React's dev-only double mount cancels before a socket is created.
  let demoTimer: ReturnType<typeof setTimeout> | undefined;
  if (demoForced()) {
    demoTimer = setTimeout(startDemo, 0);
  } else if (!liveOnly()) {
    demoTimer = setTimeout(() => {
      if (!closed && !store().connected) startDemo();
    }, DEMO_AFTER_MS);
  }
  if (!demoForced()) timer = setTimeout(open, 0);
  const sweeper = setInterval(() => store().sweep(), 500);

  return () => {
    closed = true;
    clearTimeout(timer);
    clearTimeout(demoTimer);
    clearInterval(sweeper);
    stopDemo();
    socket?.close();
  };
}
