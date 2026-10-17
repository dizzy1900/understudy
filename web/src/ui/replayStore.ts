import { create } from "zustand";

import { api } from "../api";
import type { LadderMove } from "../store";
import { useStream } from "../store";
import { RUNGS, TRAINING_CLASSES } from "../theme";
import type { Rung, StreamEvent } from "../types";
import { say, shortError } from "./actions";

export const REPLAY_MS = 20_000;
const HOLD_MS = 1200;

export interface ReplayStats {
  incidents: number | null;
  rejected: number | null;
  costPerIncident: number | null;
  cacheHit: number | null;
}

const EMPTY_STATS: ReplayStats = { incidents: null, rejected: null, costPerIncident: null, cacheHit: null };

/** Written every frame by the engine and read by the banner's own rAF loop, so the clock never goes through React. */
export const replayClock = { first: 0, last: 0, ts: 0, progress: 0 };

interface ReplayState {
  active: boolean;
  loading: boolean;
  ladder: Record<string, Rung>;
  moves: LadderMove[];
  stats: ReplayStats;
  start: () => Promise<void>;
  stop: () => void;
  toggle: () => void;
}

let events: StreamEvent[] = [];
let cursor = 0;
let raf = 0;
let holdTimer = 0;
let startedAt = 0;
let token = 0;
let useLedger = false;
let moveKey = 0;
let opened = 0;
let rejected = 0;
let closed = 0;
let costSum = 0;

function parse(raw: unknown): StreamEvent[] {
  if (!Array.isArray(raw)) return [];
  const list = raw.filter((item): item is StreamEvent => {
    if (!item || typeof item !== "object") return false;
    const e = item as { type?: unknown; ts?: unknown; data?: unknown };
    return (
      typeof e.type === "string" &&
      typeof e.ts === "number" &&
      Number.isFinite(e.ts) &&
      !!e.data &&
      typeof e.data === "object"
    );
  });
  return list.sort((a, b) => a.ts - b.ts);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function freshLadder(): Record<string, Rung> {
  const names = new Set<string>([...TRAINING_CLASSES, ...Object.keys(useStream.getState().ladder)]);
  return Object.fromEntries([...names].map((name) => [name, "super" as Rung]));
}

function advance(ts: number): void {
  let ladder: Record<string, Rung> | null = null;
  let moves: LadderMove[] | null = null;
  let stats: ReplayStats | null = null;
  const current = useReplay.getState();

  while (cursor < events.length && events[cursor].ts <= ts) {
    const event = events[cursor++];
    switch (event.type) {
      case "ledger": {
        const d = event.data;
        stats = {
          incidents: num(d.incidents_total),
          rejected: num(d.rejected_claims),
          costPerIncident: num(d.cost_per_incident_usd),
          cacheHit: num(d.cache_hit_rate),
        };
        break;
      }
      case "incident_opened":
        opened += 1;
        if (!useLedger) stats = { ...(stats ?? current.stats), incidents: opened };
        break;
      case "verification":
        if (event.data.verdict === "rejected") {
          rejected += 1;
          if (!useLedger) stats = { ...(stats ?? current.stats), rejected };
        }
        break;
      case "incident_closed": {
        const cost = num(event.data.cost_usd);
        if (cost !== null) {
          closed += 1;
          costSum += cost;
          if (!useLedger) stats = { ...(stats ?? current.stats), costPerIncident: costSum / closed };
        }
        break;
      }
      case "ladder_update": {
        const d = event.data;
        if (typeof d.fault_class !== "string" || !RUNGS.includes(d.to_rung)) break;
        ladder = { ...(ladder ?? current.ladder), [d.fault_class]: d.to_rung };
        moves = [
          ...(moves ?? current.moves),
          {
            key: ++moveKey,
            fault_class: d.fault_class,
            from_rung: d.from_rung,
            to_rung: d.to_rung,
            direction: d.direction,
            passed: num(d.evidence?.passed) ?? 0,
            total: num(d.evidence?.total) ?? 0,
            mean_cost_usd: num(d.evidence?.mean_cost_usd) ?? 0,
            at: Date.now(),
          },
        ];
        break;
      }
      default:
        break;
    }
  }

  if (ladder || moves || stats) {
    useReplay.setState({
      ...(ladder ? { ladder } : null),
      ...(moves ? { moves } : null),
      ...(stats ? { stats } : null),
    });
  }
}

function frame(now: number): void {
  const progress = Math.min(1, (now - startedAt) / REPLAY_MS);
  const ts = replayClock.first + (replayClock.last - replayClock.first) * progress;
  replayClock.ts = ts;
  replayClock.progress = progress;
  advance(ts);
  if (progress >= 1) {
    raf = 0;
    holdTimer = window.setTimeout(() => useReplay.getState().stop(), HOLD_MS);
    return;
  }
  raf = requestAnimationFrame(frame);
}

export const useReplay = create<ReplayState>((set, get) => ({
  active: false,
  loading: false,
  ladder: {},
  moves: [],
  stats: EMPTY_STATS,

  start: async () => {
    const state = get();
    if (state.active || state.loading) return;
    const mine = ++token;
    set({ loading: true });

    let list: StreamEvent[];
    try {
      const payload = await api.history();
      list = parse(payload?.events);
    } catch (error) {
      if (mine !== token) return;
      set({ loading: false });
      say(`Replay unavailable: ${shortError(error)}`);
      return;
    }
    if (mine !== token) return;
    if (list.length === 0) {
      set({ loading: false });
      say("No history to replay yet.", "low");
      return;
    }

    events = list;
    cursor = 0;
    useLedger = list.some((e) => e.type === "ledger");
    opened = 0;
    rejected = 0;
    closed = 0;
    costSum = 0;
    const first = list[0].ts;
    const last = Math.max(list[list.length - 1].ts, first + 1);
    replayClock.first = first;
    replayClock.last = last;
    replayClock.ts = first;
    replayClock.progress = 0;

    set({ active: true, loading: false, ladder: freshLadder(), moves: [], stats: EMPTY_STATS });
    startedAt = performance.now();
    raf = requestAnimationFrame(frame);
  },

  stop: () => {
    token += 1;
    cancelAnimationFrame(raf);
    window.clearTimeout(holdTimer);
    raf = 0;
    events = [];
    set({ active: false, loading: false, moves: [], stats: EMPTY_STATS });
  },

  toggle: () => {
    const state = get();
    if (state.active || state.loading) state.stop();
    else void state.start();
  },
}));
