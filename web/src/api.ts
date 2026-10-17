import { demoSim } from "./demo";
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
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch {
    throw new Error("backend unreachable");
  }
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
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error("backend returned no data");
  }
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
  text: string;
  stats: Record<string, unknown>;
}

export interface ChaosPayload {
  injected: unknown[];
  plan?: unknown;
  text?: string;
}

export interface VoiceStatus {
  available: boolean;
  path: "omni" | "browser";
  model?: string;
  /** Containers the endpoint takes as they are. Anything else is converted to WAV first. */
  formats?: string[];
  reason?: string;
}

function demoNotFree<T>(injected: T[]): { injected: T[] } {
  if (injected.length === 0) throw new Error("no healthy node is free right now");
  return { injected };
}

/** While the demo runs, each call is answered by the in-browser simulator instead of the network. */
function viaDemo<T>(live: () => Promise<T>, demo: (sim: NonNullable<ReturnType<typeof demoSim>>) => T): Promise<T> {
  const sim = demoSim();
  if (!sim) return live();
  try {
    return Promise.resolve(demo(sim));
  } catch (error) {
    return Promise.reject(error);
  }
}

export const api = {
  ledger: () => viaDemo(() => request<Ledger>("/api/ledger"), (sim) => sim.ledger()),
  ladder: () =>
    viaDemo(
      () => request<{ ladder: Record<string, string>; events: LadderLogEntry[] }>("/api/ladder"),
      (sim) => ({ ladder: { ...sim.ladder } as Record<string, string>, events: sim.ladderLog.slice(-50) }),
    ),
  skills: () => viaDemo(() => request<SkillCard[]>("/api/skills"), (sim) => Object.values(sim.skills) as SkillCard[]),
  history: (since = 0) =>
    viaDemo(
      () => request<HistoryPayload>(`/api/history?since=${since}`),
      (sim) => ({ events: sim.history.filter((e) => e.ts > since), ladder_events: sim.ladderLog.filter((e) => e.ts > since) }),
    ),
  storm: (rows: number[]) =>
    viaDemo(() => post<{ injected: unknown[] }>("/api/storm", { rows }), (sim) => demoNotFree<unknown>(sim.storm(rows))),
  inject: (node_id: string, fault_class: string) =>
    viaDemo(
      () => post<{ injected: unknown[] }>("/api/inject", { node_id, fault_class }),
      (sim) => demoNotFree<unknown>(sim.injectAnywhere(node_id, fault_class)),
    ),
  chaos: (command: string) =>
    viaDemo(() => post<ChaosPayload>("/api/chaos", { command }), (sim) => ({ injected: sim.chaos(command) })),
  back: () =>
    viaDemo(() => post<BackPayload>("/api/back"), (sim) => {
      const r = sim.comeBack() as unknown as { briefing?: string; text?: string };
      return { text: r.text ?? r.briefing ?? "", stats: {} };
    }),
  away: () => viaDemo(() => post<{ away_since: number }>("/api/away"), (sim) => ({ away_since: sim.goAway() })),
  promote: () =>
    viaDemo(
      () => post<{ started: boolean; reason?: string }>("/api/promote/run"),
      (sim) => ({ started: sim.runPromotion() }),
    ),
  voiceStatus: () =>
    viaDemo(
      () => request<VoiceStatus>("/api/voice/status"),
      () => ({ available: false, path: "browser" as const, reason: "voice runs against the live backend only" }),
    ),
  voice: (audio: Blob) =>
    request<{ transcript: string }>("/api/voice", {
      method: "POST",
      headers: { "Content-Type": audio.type || "application/octet-stream" },
      body: audio,
    }),
};
