import type { HistoryPayload } from "../api";
import { RUNGS } from "../theme";
import type { LadderLogEntry, Rung, StreamEvent } from "../types";

export interface ReplayStats {
  incidents: number | null;
  rejected: number | null;
  costPerIncident: number | null;
  cacheHit: number | null;
}

export interface LadderStep {
  fault_class: string;
  from_rung: Rung;
  to_rung: Rung;
  direction: "promote" | "demote";
  passed: number;
  total: number;
  mean_cost_usd: number;
}

export type Step =
  | { ts: number; kind: "stats"; patch: Partial<ReplayStats> }
  | { ts: number; kind: "ladder"; move: LadderStep };

/** What the live ledger says now, used for the numbers history does not carry as a series. */
export interface LiveTotals {
  rejected: number | null;
  cacheHit: number | null;
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isRung(value: unknown): value is Rung {
  return typeof value === "string" && (RUNGS as readonly string[]).includes(value);
}

export function parseEvents(raw: unknown): StreamEvent[] {
  if (!Array.isArray(raw)) return [];
  const list = raw.filter((item): item is StreamEvent => {
    if (!item || typeof item !== "object") return false;
    const e = item as { type?: unknown; ts?: unknown; data?: unknown };
    return typeof e.type === "string" && num(e.ts) !== null && !!e.data && typeof e.data === "object";
  });
  return list.sort((a, b) => a.ts - b.ts);
}

function series(raw: unknown, key: string): { ts: number; value: number }[] {
  if (!Array.isArray(raw)) return [];
  const out: { ts: number; value: number }[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const ts = num(row.ts);
    const value = num(row[key]);
    if (ts !== null && value !== null) out.push({ ts, value });
  }
  return out.sort((a, b) => a.ts - b.ts);
}

function ladderStep(source: {
  fault_class?: unknown;
  from_rung?: unknown;
  to_rung?: unknown;
  direction?: unknown;
  evidence?: { passed?: unknown; total?: unknown; mean_cost_usd?: unknown } | null;
  passed?: unknown;
  total?: unknown;
  mean_cost_usd?: unknown;
}): LadderStep | null {
  if (typeof source.fault_class !== "string" || !isRung(source.to_rung) || !isRung(source.from_rung)) return null;
  const evidence = source.evidence ?? source;
  return {
    fault_class: source.fault_class,
    from_rung: source.from_rung,
    to_rung: source.to_rung,
    direction: source.direction === "demote" ? "demote" : "promote",
    passed: num(evidence.passed) ?? 0,
    total: num(evidence.total) ?? 0,
    mean_cost_usd: num(evidence.mean_cost_usd) ?? 0,
  };
}

function ladderSteps(entries: LadderLogEntry[] | undefined, events: StreamEvent[]): Step[] {
  const steps: Step[] = [];
  if (Array.isArray(entries) && entries.length > 0) {
    for (const entry of entries) {
      const ts = num(entry?.ts);
      const move = entry ? ladderStep(entry) : null;
      if (ts !== null && move) steps.push({ ts, kind: "ladder", move });
    }
    return steps;
  }
  for (const event of events) {
    if (event.type !== "ladder_update") continue;
    const move = ladderStep(event.data);
    if (move) steps.push({ ts: event.ts, kind: "ladder", move });
  }
  return steps;
}

/** Rebuilds the numbers from the full-run series the history endpoint keeps in the database. */
function fromSeries(payload: HistoryPayload, events: StreamEvent[], live: LiveTotals): Step[] {
  const steps: Step[] = [];
  for (const point of series(payload.incidents, "total")) {
    steps.push({ ts: point.ts, kind: "stats", patch: { incidents: point.value } });
  }
  for (const point of series(payload.cost_per_incident, "value")) {
    steps.push({ ts: point.ts, kind: "stats", patch: { costPerIncident: point.value } });
  }
  steps.push(...ladderSteps(payload.ladder_events, events));

  // The event window is capped, so rejections before it are inferred from the ledger total,
  // which counts live-zone verifications only. Their times are unknown: they land at the window start.
  const rejections = events.filter(
    (e) => e.type === "verification" && e.data.verdict === "rejected" && !String(e.data.node_id).startsWith("pg-"),
  );
  const first = steps.reduce((min, s) => Math.min(min, s.ts), Infinity);
  const windowStart = events.length > 0 ? events[0].ts : first;
  let rejected = Math.max(0, (live.rejected ?? rejections.length) - rejections.length);
  steps.push({ ts: first, kind: "stats", patch: { rejected: 0, cacheHit: live.cacheHit } });
  steps.push({ ts: Math.max(first, windowStart), kind: "stats", patch: { rejected } });
  for (const event of rejections) {
    rejected += 1;
    steps.push({ ts: event.ts, kind: "stats", patch: { rejected } });
  }
  return steps;
}

/** Counts from the event stream alone, for servers that publish ledger snapshots or no series. */
function fromEvents(events: StreamEvent[]): Step[] {
  const steps: Step[] = [];
  const hasLedger = events.some((e) => e.type === "ledger");
  let opened = 0;
  let rejected = 0;
  let closed = 0;
  let costSum = 0;

  for (const event of events) {
    switch (event.type) {
      case "ledger": {
        const d = event.data;
        steps.push({
          ts: event.ts,
          kind: "stats",
          patch: {
            incidents: num(d.incidents_total),
            rejected: num(d.rejected_claims),
            costPerIncident: num(d.cost_per_incident_usd),
            cacheHit: num(d.cache_hit_rate),
          },
        });
        break;
      }
      case "incident_opened":
        if (hasLedger) break;
        opened += 1;
        steps.push({ ts: event.ts, kind: "stats", patch: { incidents: opened } });
        break;
      case "verification":
        if (hasLedger || event.data.verdict !== "rejected") break;
        rejected += 1;
        steps.push({ ts: event.ts, kind: "stats", patch: { rejected } });
        break;
      case "incident_closed": {
        const cost = num(event.data.cost_usd);
        if (hasLedger || cost === null) break;
        closed += 1;
        costSum += cost;
        steps.push({ ts: event.ts, kind: "stats", patch: { costPerIncident: costSum / closed } });
        break;
      }
      default:
        break;
    }
  }
  steps.push(...ladderSteps(undefined, events));
  return steps;
}

export function buildTimeline(payload: HistoryPayload | null | undefined, live: LiveTotals): Step[] {
  if (!payload) return [];
  const events = parseEvents(payload.events);
  const hasSeries = series(payload.incidents, "total").length > 0;
  const steps = hasSeries ? fromSeries(payload, events, live) : fromEvents(events);
  return steps.sort((a, b) => a.ts - b.ts);
}
