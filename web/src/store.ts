import { create } from "zustand";

export interface StreamEvent {
  type: string;
  ts: number;
  data: Record<string, unknown>;
}

interface StreamState {
  connected: boolean;
  counts: Record<string, number>;
  recent: StreamEvent[];
  push: (event: StreamEvent) => void;
  setConnected: (connected: boolean) => void;
}

const RECENT_LIMIT = 40;

export const useStream = create<StreamState>((set) => ({
  connected: false,
  counts: {},
  recent: [],
  push: (event) =>
    set((state) => ({
      counts: { ...state.counts, [event.type]: (state.counts[event.type] ?? 0) + 1 },
      recent: [event, ...state.recent].slice(0, RECENT_LIMIT),
    })),
  setConnected: (connected) => set({ connected }),
}));

const WS_URL = import.meta.env.VITE_WS_URL ?? "ws://127.0.0.1:8000/ws";

export function connect(): () => void {
  let socket: WebSocket | null = null;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let closed = false;

  const open = () => {
    socket = new WebSocket(WS_URL);
    socket.onopen = () => useStream.getState().setConnected(true);
    socket.onmessage = (message) => useStream.getState().push(JSON.parse(message.data));
    socket.onclose = () => {
      useStream.getState().setConnected(false);
      if (!closed) retry = setTimeout(open, 1000);
    };
  };
  open();

  return () => {
    closed = true;
    clearTimeout(retry);
    socket?.close();
  };
}
