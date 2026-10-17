import { api, wsUrl } from "./api";
import { useStream } from "./store";
import type { StreamEvent } from "./types";

const BACKOFF_START_MS = 500;
const BACKOFF_CAP_MS = 8000;

/** Opens the event stream and keeps it open. Returns a function that closes it. */
export function connect(): () => void {
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let closed = false;
  let delay = BACKOFF_START_MS;

  const store = () => useStream.getState();

  const schedule = () => {
    if (closed) return;
    const wait = delay + Math.random() * delay * 0.3;
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
      store().setConnected(true);
      // The socket sends a fleet_snapshot and a ledger on connect; the ladder is a cheap backstop.
      api.ladder().then((body) => store().setLadder(body.ladder)).catch(() => undefined);
    };
    ws.onmessage = (message) => {
      try {
        store().handle(JSON.parse(message.data) as StreamEvent);
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

  open();
  const sweeper = setInterval(() => store().sweep(), 500);

  return () => {
    closed = true;
    clearTimeout(timer);
    clearInterval(sweeper);
    socket?.close();
  };
}
