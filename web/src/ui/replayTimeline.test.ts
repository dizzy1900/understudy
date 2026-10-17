import { describe, expect, it } from "vitest";

import type { HistoryPayload } from "../api";
import history from "../__fixtures__/real-history.json";
import { buildTimeline } from "./replayTimeline";
import type { ReplayStats, Step } from "./replayTimeline";

const payload = history as unknown as HistoryPayload;

function statsAt(steps: Step[], ts: number): ReplayStats {
  const out: ReplayStats = { incidents: null, rejected: null, costPerIncident: null, cacheHit: null };
  for (const step of steps) {
    if (step.ts > ts) break;
    if (step.kind === "stats") Object.assign(out, step.patch);
  }
  return out;
}

describe("replay timeline from the history endpoint", () => {
  const steps = buildTimeline(payload, { rejected: 1, cacheHit: 0.274 });

  it("is ordered and covers the whole run, not just the capped event window", () => {
    const ts = steps.map((s) => s.ts);
    expect(ts).toEqual([...ts].sort((a, b) => a - b));
    const eventsStart = Math.min(...payload.events.map((e) => e.ts));
    expect(ts[0]).toBeLessThan(eventsStart - 60);
  });

  it("ends on the numbers the ledger reports", () => {
    const end = statsAt(steps, Infinity);
    expect(end.incidents).toBe(113);
    expect(end.rejected).toBe(1);
    expect(end.cacheHit).toBe(0.274);
    expect(end.costPerIncident).toBeCloseTo(0.011, 2);
  });

  it("starts at one incident with no rejections yet", () => {
    const start = statsAt(steps, steps[0].ts);
    expect(start.incidents).toBe(1);
    expect(start.rejected).toBe(0);
  });

  it("replays every ladder move in order", () => {
    const moves = steps.filter((s) => s.kind === "ladder");
    expect(moves).toHaveLength(payload.ladder_events?.length ?? -1);
    for (const step of moves) {
      if (step.kind !== "ladder") continue;
      expect(step.move.direction).toBe("promote");
      expect(step.move.total).toBe(5);
    }
  });

  it("counts a rejection from the event window on top of the inferred baseline", () => {
    const rejection = {
      type: "verification",
      ts: Math.max(...payload.events.map((e) => e.ts)) + 1,
      data: { incident_id: 1, node_id: "r1-01", verdict: "rejected", probes: [], note: "" },
    };
    const events = [...payload.events, rejection];
    const inWindow = events.filter((e) => e.type === "verification" && e.data.verdict === "rejected").length;
    const withOne = buildTimeline({ ...payload, events } as unknown as HistoryPayload, {
      rejected: inWindow + 2,
      cacheHit: null,
    });
    const windowStart = Math.min(...events.map((e) => e.ts));
    expect(statsAt(withOne, withOne[0].ts).rejected).toBe(0);
    expect(statsAt(withOne, windowStart).rejected).toBe(2);
    expect(statsAt(withOne, Infinity).rejected).toBe(inWindow + 2);
  });

  it("falls back to the event stream when there are no series", () => {
    const events = [
      { type: "incident_opened", ts: 10, data: { incident_id: 1, node_id: "r1-01", symptom: "x", severity: "low" } },
      { type: "incident_opened", ts: 20, data: { incident_id: 2, node_id: "r1-02", symptom: "x", severity: "low" } },
      { type: "incident_closed", ts: 30, data: { incident_id: 1, node_id: "r1-01", fault_class: "disk_full", rung: "super", steps: 3, cost_usd: 0.02, seconds: 5 } },
    ];
    const fallback = buildTimeline({ events } as unknown as HistoryPayload, { rejected: null, cacheHit: null });
    const end = statsAt(fallback, Infinity);
    expect(end.incidents).toBe(2);
    expect(end.costPerIncident).toBeCloseTo(0.02);
  });

  it("returns nothing for an empty or missing payload", () => {
    expect(buildTimeline(null, { rejected: null, cacheHit: null })).toEqual([]);
    expect(buildTimeline({ events: [] }, { rejected: null, cacheHit: null })).toEqual([]);
  });
});
