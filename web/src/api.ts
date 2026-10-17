import type { Ledger, LadderLogEntry, SkillCard, StreamEvent } from "./types";

function resolveBase(): string {
  const configured = import.meta.env.VITE_API_BASE as string | undefined;
  if (configured !== undefined && configured !== "") return configured.replace(/\/+$/, "");
  // A built bundle served next to its API (the Vercel layout) talks to its own origin.
  return import.meta.env.DEV ? "http://localhost:8000" : "";
}

export const API_BASE = resolveBase();

export function wsUrl(): string {
  const override = import.meta.env.VITE_WS_URL as string | undefined;
  if (override) return override;
  if (API_BASE === "") {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${window.location.host}/ws`;
  }
  return `${API_BASE.replace(/^http/, "ws")}/ws`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* keep the status text */
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export interface HistoryPayload {
  events: StreamEvent[];
  ladder_events?: LadderLogEntry[];
  incidents?: unknown[];
  cost_per_incident?: unknown[];
}

export interface BackPayload {
  since: number;
  briefing: string;
  ledger?: Ledger;
}

export const api = {
  ledger: () => request<Ledger>("/api/ledger"),
  ladder: () => request<{ ladder: Record<string, string>; events: LadderLogEntry[] }>("/api/ladder"),
  skills: () => request<SkillCard[]>("/api/skills"),
  history: (since = 0) => request<HistoryPayload>(`/api/history?since=${since}`),
  storm: (rows: number[]) => post<{ injected: unknown[] }>("/api/storm", { rows }),
  inject: (node_id: string, fault_class: string) =>
    post<{ injected: unknown[] }>("/api/inject", { node_id, fault_class }),
  chaos: (command: string) => post<{ injected: unknown[] }>("/api/chaos", { command }),
  back: () => post<BackPayload>("/api/back"),
  away: () => post<{ away_since: number }>("/api/away"),
};
