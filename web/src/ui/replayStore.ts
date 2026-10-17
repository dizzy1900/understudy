import { create } from "zustand";

import { api } from "../api";
import type { LadderMove } from "../store";
import { useStream } from "../store";
import { TRAINING_CLASSES } from "../theme";
import type { Rung } from "../types";
import { say, shortError } from "./actions";
import type { ReplayStats, Step } from "./replayTimeline";
import { buildTimeline, num } from "./replayTimeline";

export type { ReplayStats } from "./replayTimeline";

export const REPLAY_MS = 20_000;
const HOLD_MS = 1200;

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

let steps: Step[] = [];
let cursor = 0;
let raf = 0;
let holdTimer = 0;
let startedAt = 0;
let token = 0;
let moveKey = 0;

function freshLadder(): Record<string, Rung> {
  const names = new Set<string>([...TRAINING_CLASSES, ...Object.keys(useStream.getState().ladder)]);
  return Object.fromEntries([...names].map((name) => [name, "super" as Rung]));
}

function advance(ts: number): void {
  const current = useReplay.getState();
  let ladder: Record<string, Rung> | null = null;
  let moves: LadderMove[] | null = null;
  let stats: ReplayStats | null = null;

  while (cursor < steps.length && steps[cursor].ts <= ts) {
    const step = steps[cursor++];
    if (step.kind === "stats") {
      stats = { ...(stats ?? current.stats), ...step.patch };
      continue;
    }
    const { move } = step;
    ladder = { ...(ladder ?? current.ladder), [move.fault_class]: move.to_rung };
    moves = [
      ...(moves ?? current.moves),
      {
        key: ++moveKey,
        fault_class: move.fault_class,
        from_rung: move.from_rung,
        to_rung: move.to_rung,
        direction: move.direction,
        passed: move.passed,
        total: move.total,
        mean_cost_usd: move.mean_cost_usd,
        at: Date.now(),
      },
    ];
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

    let timeline: Step[];
    try {
      const payload = await api.history();
      const ledger = useStream.getState().ledger;
      timeline = buildTimeline(payload, {
        rejected: num(ledger?.rejected_claims),
        cacheHit: num(ledger?.cache_hit_rate),
      });
    } catch (error) {
      if (mine !== token) return;
      set({ loading: false });
      say(`Replay unavailable: ${shortError(error)}`);
      return;
    }
    if (mine !== token) return;
    if (timeline.length === 0) {
      set({ loading: false });
      say("No history to replay yet.", "low");
      return;
    }

    steps = timeline;
    cursor = 0;
    const first = timeline[0].ts;
    const last = Math.max(timeline[timeline.length - 1].ts, first + 1);
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
    steps = [];
    set({ active: false, loading: false, moves: [], stats: EMPTY_STATS });
  },

  toggle: () => {
    const state = get();
    if (state.active || state.loading) state.stop();
    else void state.start();
  },
}));
