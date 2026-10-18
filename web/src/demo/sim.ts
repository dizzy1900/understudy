/**
 * In-browser port of scripts/mock_events.py. Emits the same StreamEvent objects the real backend sends, on a
 * virtual clock that the caller advances, so the deployed page has something to show without a backend.
 * Every number it produces is synthetic.
 */
import type { AgentRole, AgentRung, Ledger, LadderLogEntry, NodeInfo, NodeState, Probe, Rung, RungStat, StreamEvent } from "../types";
import { FAULTS, HIGH_SEVERITY, MODEL_IDS, TRAINING_FAULT_CLASSES, type DemoFault } from "./faults";

const RUNGS: Rung[] = ["super", "lightning", "script"];
/** When the story beats happen, in story seconds. */
export interface Pacing {
  maxDown: number;
  rejectEvery: number;
  firstReject: number;
  stormEvery: number;
  firstStorm: number;
  promoterFirst: number;
  promoterEvery: number;
  /** Seconds between new incidents, as [min, max]. */
  arrival: [number, number];
}

/** The original pace: a lot happens in the first 30 seconds. Used by ?pace=fast and the tests. */
export const FAST_PACING: Pacing = {
  maxDown: 6,
  rejectEvery: 60,
  firstReject: 28,
  stormEvery: 240,
  firstStorm: 75,
  promoterFirst: 18,
  promoterEvery: 40,
  arrival: [8, 18],
};

/**
 * A live walkthrough of about three minutes, at real time: one incident at a time to start with, a rejected false
 * fix near 0:45, the first promotion near 1:20, and no automatic storm before 3:20 so the presenter can press S.
 */
export const PRESENTER_PACING: Pacing = {
  maxDown: 3,
  rejectEvery: 90,
  firstReject: 40,
  stormEvery: 300,
  firstStorm: 200,
  promoterFirst: 70,
  promoterEvery: 75,
  arrival: [14, 24],
};
const PROVING_TRIALS = 5;
const HISTORY_LIMIT = 3000;
const COST_RANGE: Record<Rung, [number, number]> = {
  super: [0.02, 0.05],
  lightning: [0.003, 0.008],
  script: [0.0004, 0.001],
};
const CARD_SUMMARIES = ["Added pitfall from a verified fix", "Tightened fix steps", "Merged duplicate pitfalls"];

/** A process yields how many story seconds to sleep before it continues. */
type Proc = Generator<number, void, unknown>;

interface Task {
  at: number;
  seq: number;
  proc: Proc;
}

interface NodeSim {
  id: string;
  row: number;
  col: number;
  zone: "live" | "proving";
  status: NodeState;
  probe_ms: number;
  busy: boolean;
  next_request: number;
  window: [number, number][];
}

type Counts = { incidents: number; closed: number; verified: number; rejected: number; promotions: number; demotions: number };

export interface SkillCardData {
  fault_class: string;
  version: number;
  signature: string;
  diagnosis: string;
  fix_steps: string[];
  pitfalls: string[];
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class DemoSim {
  /** Story clock, seconds since start. */
  t = 0;
  private queue: Task[] = [];
  private taskSeq = 0;
  private emitSeq = 0;
  private readonly epoch: number;
  private rand: () => number;
  private seq = 0;
  nodes: Record<string, NodeSim> = {};
  history: StreamEvent[] = [];
  ladder: Record<string, Rung> = {};
  ladderLog: LadderLogEntry[] = [];
  skills: Record<string, SkillCardData> = {};
  counts: Counts = { incidents: 0, closed: 0, verified: 0, rejected: 0, promotions: 0, demotions: 0 };
  private costIncidents = 0;
  private costOther = 0;
  private tokensServed = 0;
  private promptTokens = 0;
  private cachedTokens = 0;
  private byRung: Record<Rung, RungStat> = { super: { incidents: 0, cost_usd: 0 }, lightning: { incidents: 0, cost_usd: 0 }, script: { incidents: 0, cost_usd: 0 } };
  private rungStarted: Record<Rung, number> = { super: 0, lightning: 0, script: 0 };
  private promoterCycles = 0;
  private promoting = false;
  private nextRejection = 0;
  private away: { since: number; counts: Counts; cost: number } | null = null;

  constructor(
    private readonly sink: (event: StreamEvent) => void,
    seed = 7,
    epoch = Date.now() / 1000,
    private readonly pacing: Pacing = FAST_PACING,
  ) {
    this.rand = mulberry32(seed);
    this.nextRejection = pacing.firstReject;
    this.epoch = epoch;
    for (let row = 1; row <= 3; row++)
      for (let col = 1; col <= 4; col++) this.addNode(`r${row}-0${col}`, row, col, "live");
    for (let col = 1; col <= 4; col++) this.addNode(`pg-0${col}`, 1, col, "proving");
    for (const c of TRAINING_FAULT_CLASSES) this.ladder[c] = "super";
  }

  private addNode(id: string, row: number, col: number, zone: "live" | "proving"): void {
    this.nodes[id] = { id, row, col, zone, status: "healthy", probe_ms: 300, busy: false, next_request: this.uniform(0, 10), window: [] };
  }

  // clock -------------------------------------------------------------------------------------------

  /** Wall-clock seconds for the current story time, as the backend stamps events. */
  now(): number {
    return this.epoch + this.t;
  }

  private uniform(lo: number, hi: number): number {
    return lo + this.rand() * (hi - lo);
  }

  private randint(lo: number, hi: number): number {
    return Math.floor(this.uniform(lo, hi + 1));
  }

  private choice<T>(items: readonly T[]): T {
    return items[Math.floor(this.rand() * items.length)];
  }

  private spawn(proc: Proc, delay = 0): void {
    this.queue.push({ at: this.t + delay, seq: ++this.taskSeq, proc });
  }

  /** Runs every process due within the next `seconds` of story time. */
  advance(seconds: number): void {
    const end = this.t + Math.max(0, seconds);
    for (;;) {
      let best = -1;
      for (let i = 0; i < this.queue.length; i++) {
        const q = this.queue[i];
        if (q.at > end) continue;
        if (best < 0 || q.at < this.queue[best].at || (q.at === this.queue[best].at && q.seq < this.queue[best].seq)) best = i;
      }
      if (best < 0) break;
      const task = this.queue.splice(best, 1)[0];
      this.t = Math.max(this.t, task.at);
      let step: IteratorResult<number, void>;
      try {
        step = task.proc.next();
      } catch (error) {
        console.warn("demo process failed", error);
        continue;
      }
      if (!step.done) this.queue.push({ at: this.t + Math.max(0, step.value), seq: ++this.taskSeq, proc: task.proc });
    }
    this.t = end;
  }

  start(): void {
    this.emit({ type: "fleet_snapshot", ts: 0, data: this.snapshot() });
    this.emit({ type: "ledger", ts: 0, data: this.ledger() });
    this.spawn(this.ticker());
    this.spawn(this.traffic());
    this.spawn(this.scenario());
    this.spawn(this.promoter());
  }

  // plumbing ------------------------------------------------------------------------------------------

  private emit(event: StreamEvent): void {
    // A tiny per-event offset keeps identical payloads in the same instant distinct for the store's duplicate filter.
    const stamped = { ...event, ts: this.now() + ++this.emitSeq * 1e-6 } as StreamEvent;
    if (stamped.type !== "tokens") {
      this.history.push(stamped);
      if (this.history.length > HISTORY_LIMIT) this.history.splice(0, this.history.length - HISTORY_LIMIT);
    }
    this.sink(stamped);
  }

  private nextId(): number {
    return ++this.seq;
  }

  snapshot(): { nodes: NodeInfo[] } {
    return {
      nodes: Object.values(this.nodes).map((n) => {
        n.window = n.window.filter(([at]) => this.t - at <= 60);
        return {
          id: n.id,
          row: n.row,
          col: n.col,
          zone: n.zone,
          status: n.status,
          tokens_per_min: n.window.reduce((sum, [, tok]) => sum + tok, 0),
          last_probe_ms: Math.round(n.probe_ms * 10) / 10,
        };
      }),
    };
  }

  ledger(): Ledger {
    const closed = this.counts.closed;
    const by_rung: Partial<Record<Rung, RungStat>> = {};
    for (const r of RUNGS) by_rung[r] = { ...this.byRung[r] };
    return {
      since: this.epoch,
      incidents_total: this.counts.incidents,
      verified: this.counts.verified,
      rejected_claims: this.counts.rejected,
      promotions: this.counts.promotions,
      demotions: this.counts.demotions,
      cost_total_usd: round(this.costIncidents + this.costOther, 4),
      cost_per_incident_usd: closed ? round(this.costIncidents / closed, 4) : 0,
      tokens_served: this.tokensServed,
      cache_hit_rate: this.promptTokens ? round(this.cachedTokens / this.promptTokens, 3) : 0,
      by_rung,
      ladder: { ...this.ladder },
    };
  }

  private downCount(): number {
    return Object.values(this.nodes).filter((n) => n.zone === "live" && n.status !== "healthy").length;
  }

  private setStatus(nodeId: string, status: NodeState, reason: string | null = null): void {
    this.nodes[nodeId].status = status;
    this.emit({ type: "node_status", ts: 0, data: { node_id: nodeId, status, reason } });
  }

  private sampleCost(rung: Rung): number {
    const [lo, hi] = COST_RANGE[rung];
    return this.uniform(lo, hi);
  }

  // agents -------------------------------------------------------------------------------------------

  private spawnAgent(
    role: AgentRole,
    rung: AgentRung,
    opts: { model?: string; prefix?: string; incident_id?: number | null; node_id?: string | null } = {},
  ): string {
    const agent_id = `${opts.prefix ?? role}-${this.nextId()}`;
    this.emit({
      type: "agent_spawned",
      ts: 0,
      data: {
        agent_id,
        role,
        model: opts.model ?? MODEL_IDS[rung] ?? rung,
        rung,
        incident_id: opts.incident_id ?? null,
        node_id: opts.node_id ?? null,
      },
    });
    return agent_id;
  }

  private thought(agent_id: string, text: string): void {
    this.emit({ type: "agent_thought", ts: 0, data: { agent_id, text } });
  }

  private done(agent_id: string): void {
    this.emit({ type: "agent_done", ts: 0, data: { agent_id } });
  }

  private tool(agent_id: string, tool: string, args_summary: string, result_summary: string, ok = true): void {
    this.emit({ type: "tool_call", ts: 0, data: { agent_id, tool, args_summary, ok, result_summary } });
  }

  private *shortAgent(role: AgentRole, rung: AgentRung, text: string, incident_id: number, node_id: string): Proc {
    const agent = this.spawnAgent(role, rung, { incident_id, node_id });
    yield this.uniform(0.4, 1.0);
    this.thought(agent, text);
    yield this.uniform(0.4, 1.0);
    this.done(agent);
  }

  private *narrate(text: string, priority: "low" | "high" = "low", incident_id: number | null = null): Proc {
    const agent = this.spawnAgent("narrator", "nano", { incident_id });
    yield this.uniform(0.3, 0.8);
    this.emit({ type: "narration", ts: 0, data: { text, priority } });
    this.done(agent);
  }

  // loops ----------------------------------------------------------------------------------------------

  private *ticker(): Proc {
    for (let tick = 1; ; tick++) {
      yield 2;
      this.emit({ type: "ledger", ts: 0, data: this.ledger() });
      if (tick % 2 === 0) this.emit({ type: "fleet_snapshot", ts: 0, data: this.snapshot() });
    }
  }

  private *traffic(): Proc {
    for (;;) {
      for (const node of Object.values(this.nodes)) {
        if (node.zone !== "live" || node.status !== "healthy" || this.t < node.next_request) continue;
        node.next_request = this.t + this.uniform(8, 12);
        const tokens = this.randint(18, 90);
        node.window.push([this.t, tokens]);
        node.probe_ms = this.uniform(220, 1400);
        this.tokensServed += tokens;
        this.emit({ type: "tokens", ts: 0, data: { node_id: node.id, tokens } });
      }
      yield 1;
    }
  }

  private *scenario(): Proc {
    yield 1;
    let lastStorm = this.t - (this.pacing.stormEvery - this.pacing.firstStorm);
    for (;;) {
      if (this.t - lastStorm >= this.pacing.stormEvery) {
        lastStorm = this.t;
        this.storm(null);
      } else if (this.downCount() < this.pacing.maxDown) {
        const free = this.freeLiveNodes();
        if (free.length) this.inject(this.choice(free), this.choice(TRAINING_FAULT_CLASSES));
      }
      yield this.uniform(...this.pacing.arrival);
    }
  }

  private *promoter(): Proc {
    yield this.pacing.promoterFirst;
    for (;;) {
      if (!this.promoting) yield* this.promoteCycle();
      yield this.pacing.promoterEvery;
    }
  }

  // injection ------------------------------------------------------------------------------------------

  freeLiveNodes(rows: number[] | null = null): string[] {
    return Object.values(this.nodes)
      .filter((n) => n.zone === "live" && n.status === "healthy" && !n.busy && (rows === null || rows.includes(n.row)))
      .map((n) => n.id);
  }

  inject(nodeId: string, faultClass: string, storm = false): boolean {
    const node = this.nodes[nodeId];
    if (!node || !FAULTS[faultClass] || node.zone !== "live" || node.busy || node.status !== "healthy") return false;
    node.busy = true;
    const self = this;
    this.spawn(
      (function* (): Proc {
        try {
          yield* self.incident(nodeId, faultClass, storm);
        } finally {
          node.busy = false;
        }
      })(),
    );
    return true;
  }

  storm(rows: number[] | null): { node_id: string; fault_class: string }[] {
    const candidates = this.freeLiveNodes(rows);
    const room = Math.max(FAST_PACING.maxDown, this.pacing.maxDown) - this.downCount();
    const count = Math.min(this.randint(3, 6), candidates.length, Math.max(room, 0));
    const pool = [...candidates];
    const injected: { node_id: string; fault_class: string }[] = [];
    for (let i = 0; i < count; i++) {
      const node_id = pool.splice(Math.floor(this.rand() * pool.length), 1)[0];
      const fault_class = this.choice(TRAINING_FAULT_CLASSES);
      if (this.inject(node_id, fault_class, true)) injected.push({ node_id, fault_class });
    }
    if (injected.length) {
      this.spawn(this.narrate(`Storm: ${injected.length} nodes failing at once. Responders are being dispatched.`, "high"));
    }
    return injected;
  }

  /** Injects a fault class on a named node, or on any free live node when that one is busy. */
  injectAnywhere(nodeId: string | null, faultClass: string): { node_id: string; fault_class: string }[] {
    if (nodeId && this.inject(nodeId, faultClass)) return [{ node_id: nodeId, fault_class: faultClass }];
    const free = this.freeLiveNodes();
    if (!free.length) return [];
    const pick = this.choice(free);
    return this.inject(pick, faultClass) ? [{ node_id: pick, fault_class: faultClass }] : [];
  }

  // incidents ------------------------------------------------------------------------------------------

  private takeRejection(): boolean {
    if (this.t < this.nextRejection) return false;
    this.nextRejection = this.t + this.pacing.rejectEvery;
    return true;
  }

  private *incident(nodeId: string, faultClass: string, storm: boolean): Proc {
    const fault = FAULTS[faultClass];
    const inc = this.nextId();
    const started = this.t;

    this.setStatus(nodeId, "degraded", fault.symptom);
    yield this.uniform(2, 3.5);
    this.setStatus(nodeId, "down", fault.symptom);
    this.counts.incidents += 1;
    const severity = storm || HIGH_SEVERITY.has(faultClass) ? "high" : "low";
    this.emit({ type: "incident_opened", ts: 0, data: { incident_id: inc, node_id: nodeId, symptom: fault.symptom, severity } });

    const rung: Rung = this.ladder[faultClass] ?? "super";
    let cost = 0.0006 + 0.004 + 0.005;
    yield* this.shortAgent("triage", "nano", `Log and probe evidence match ${faultClass}.`, inc, nodeId);
    yield* this.shortAgent("commander", "super", `${faultClass} is on the ${rung} rung. Spawning a ${rung} responder.`, inc, nodeId);
    yield* this.narrate(`${nodeId} is down: ${fault.symptom}. A ${rung} responder is on it.`, storm ? "high" : "low", inc);
    this.setStatus(nodeId, "repairing");

    let steps = 0;
    let current: Rung = rung;
    let escalating = current !== "super" && this.rungStarted[current] % 4 === 3;
    this.rungStarted[current] += 1;
    let agent = "";
    for (;;) {
      agent = this.spawnAgent("responder", current, { incident_id: inc, node_id: nodeId });
      const plan = escalating ? fault.steps.slice(0, 3) : fault.steps;
      for (const [thought, tool, args, result] of plan) {
        if (current === "script") {
          yield this.uniform(0.3, 0.8);
        } else {
          this.thought(agent, thought);
          yield current === "lightning" ? this.uniform(1, 2) : this.uniform(1.5, 3);
        }
        this.tool(agent, tool, args, result);
        steps += 1;
      }
      if (!escalating) break;
      cost += this.sampleCost(current) * 0.4;
      const watchdog = this.spawnAgent("watchdog", current, { model: "code", incident_id: inc, node_id: nodeId });
      const higher = RUNGS[RUNGS.indexOf(current) - 1];
      this.thought(watchdog, `${current} responder is looping. Escalating to ${higher}.`);
      this.emit({ type: "escalation", ts: 0, data: { incident_id: inc, from_rung: current, to_rung: higher, reason: "repeated action 3x without progress" } });
      this.done(watchdog);
      this.done(agent);
      yield* this.narrate(`${nodeId}: ${current} responder stalled, escalating to ${higher}.`, "high", inc);
      current = higher;
      escalating = false;
    }

    cost += this.sampleCost(current);
    const quality = yield* this.verifyLoop(inc, nodeId, fault, agent, current);
    steps += 1;
    this.setStatus(nodeId, "healthy");
    this.done(agent);

    const prompt = this.randint(5000, 9000);
    this.promptTokens += prompt;
    this.cachedTokens += this.counts.closed === 0 ? 0 : Math.floor(prompt * this.uniform(0.5, 0.9));
    this.counts.verified += 1;
    this.counts.closed += 1;
    this.costIncidents += cost;
    this.byRung[current].incidents += 1;
    this.byRung[current].cost_usd = round(this.byRung[current].cost_usd + cost, 6);
    this.emit({
      type: "incident_closed",
      ts: 0,
      data: { incident_id: inc, node_id: nodeId, fault_class: faultClass, rung: current, steps, cost_usd: round(cost, 5), seconds: round(this.t - started, 1) },
    });
    if (quality >= 4) yield* this.maybeUpdateSkill(faultClass, fault);
  }

  private *verifyLoop(inc: number, nodeId: string, fault: DemoFault, agent: string, rung: Rung): Generator<number, number, unknown> {
    for (;;) {
      this.tool(agent, "claim_fixed", fault.claim.slice(0, 60), "referee running");
      this.emit({ type: "fix_claimed", ts: 0, data: { incident_id: inc, agent_id: agent, summary: fault.claim } });
      const referee = this.spawnAgent("verifier", "script", { model: "referee", incident_id: inc, node_id: nodeId });
      yield this.uniform(5, 7);

      if (this.takeRejection()) {
        this.counts.rejected += 1;
        const failsA = (fault.fails_at ?? "probe_a") === "probe_a";
        const probes: Probe[] = [
          { name: "probe_a", ok: !failsA, ms: this.uniform(200, 900), detail: failsA ? fault.probe_error : "200 OK" },
        ];
        if (fault.fails_at === "burst") probes.push({ name: "burst", ok: false, ms: this.uniform(900, 2500), detail: fault.probe_error });
        this.emit({
          type: "verification",
          ts: 0,
          data: { incident_id: inc, node_id: nodeId, verdict: "rejected", probes, note: "Claim disproved by the referee; evidence returned to the responder." },
        });
        this.done(referee);
        yield* this.narrate(`${nodeId}: fix claim rejected (${fault.probe_error}). Sent back with the evidence.`, "high", inc);
        this.thought(agent, `Rejected: ${fault.probe_error}. Checking the log again.`);
        yield this.uniform(1.5, 3);
        const fix = fault.steps[fault.steps.length - 3];
        this.tool(agent, "tail_log", "n=80", `still failing: ${fault.probe_error}`);
        this.tool(agent, fix[1], fix[2], "re-applied");
        continue;
      }

      const weights = rung === "super" ? [0.55, 0.35, 0.1] : [0.35, 0.45, 0.2];
      const r = this.rand();
      const quality = r < weights[0] ? 5 : r < weights[0] + weights[1] ? 4 : 3;
      yield* this.shortAgent("verifier", "ultra", `Root cause addressed, change is minimal. Score ${quality}.`, inc, nodeId);
      const probes: Probe[] = [
        { name: "probe_a", ok: true, ms: this.uniform(200, 900), detail: "200 OK, model matches" },
        { name: "burst", ok: true, ms: this.uniform(600, 2400), detail: "4/4 ok" },
        { name: "probe_b", ok: true, ms: this.uniform(200, 900), detail: "200 OK" },
      ];
      this.emit({ type: "verification", ts: 0, data: { incident_id: inc, node_id: nodeId, verdict: "verified", probes, quality, note: "All referee probes passed." } });
      this.done(referee);
      return quality;
    }
  }

  private *maybeUpdateSkill(faultClass: string, fault: DemoFault): Proc {
    let card = this.skills[faultClass];
    if (card && this.counts.verified % 3 !== 1) return;
    const agent = this.spawnAgent("librarian", "super", { prefix: "lib" });
    this.thought(agent, `Folding the verified fix for ${faultClass} into its card.`);
    yield this.uniform(1.5, 3);
    let summary: string;
    if (!card) {
      card = {
        fault_class: faultClass,
        version: 0,
        signature: fault.symptom,
        diagnosis: fault.steps[0][0],
        fix_steps: fault.steps.slice(1).map(([, tool, args]) => `${tool} ${args}`.trim()),
        pitfalls: [fault.pitfall],
      };
      this.skills[faultClass] = card;
      summary = "Created card from a verified fix";
    } else {
      summary = this.choice(CARD_SUMMARIES);
    }
    card.version += 1;
    this.costOther += 0.008;
    this.emit({ type: "skill_updated", ts: 0, data: { fault_class: faultClass, version: card.version, summary } });
    this.done(agent);
  }

  // promoter -------------------------------------------------------------------------------------------

  private nextPromotion(): string | null {
    const movable = TRAINING_FAULT_CLASSES.filter((c) => this.ladder[c] !== "script");
    if (!movable.length) return null;
    return [...movable].sort(
      (a, b) => RUNGS.indexOf(this.ladder[a]) - RUNGS.indexOf(this.ladder[b]) || TRAINING_FAULT_CLASSES.indexOf(a) - TRAINING_FAULT_CLASSES.indexOf(b),
    )[0];
  }

  *promoteCycle(): Proc {
    if (this.promoting) return;
    this.promoting = true;
    try {
      this.promoterCycles += 1;
      const raised = TRAINING_FAULT_CLASSES.filter((c) => this.ladder[c] !== "super");
      if (this.promoterCycles % 4 === 0 && raised.length) {
        yield* this.demote(raised[raised.length - 1]);
        return;
      }
      const faultClass = this.nextPromotion();
      if (!faultClass) return;
      const current = this.ladder[faultClass];
      const target = RUNGS[RUNGS.indexOf(current) + 1];
      const agent = this.spawnAgent("promoter", target, { model: "code" });
      this.thought(agent, `Trying ${target} on ${PROVING_TRIALS} held-out ${faultClass} instances.`);
      let passed = 0;
      let trialCost = 0;
      const allowedFailures = this.promoterCycles % 2 ? 0 : 1;
      for (let trial = 0; trial < PROVING_TRIALS; trial++) {
        const nodeId = `pg-0${(trial % 4) + 1}`;
        const ok = trial >= allowedFailures;
        this.setStatus(nodeId, "down", `held-out ${faultClass}`);
        const responder = this.spawnAgent("responder", target, { prefix: "pgresp", node_id: nodeId });
        yield this.uniform(0.8, 1.5);
        this.setStatus(nodeId, "repairing");
        const step = FAULTS[faultClass].steps[2];
        this.tool(responder, step[1], step[2], ok ? step[3] : "node still failing", ok);
        yield this.uniform(0.8, 1.5);
        trialCost += this.sampleCost(target);
        passed += ok ? 1 : 0;
        this.done(responder);
        this.setStatus(nodeId, "healthy");
      }
      this.costOther += trialCost;
      const meanCost = round(trialCost / PROVING_TRIALS, 5);
      this.done(agent);
      if (passed >= 4) {
        this.counts.promotions += 1;
        this.setLadder(faultClass, current, target, "promote", passed, PROVING_TRIALS, meanCost);
        yield* this.narrate(`${faultClass} moves from ${current} to ${target}: ${passed}/${PROVING_TRIALS} held-out passes.`, "high");
      }
    } finally {
      this.promoting = false;
    }
  }

  private *demote(faultClass: string): Proc {
    const current = this.ladder[faultClass];
    const previous = RUNGS[RUNGS.indexOf(current) - 1];
    const agent = this.spawnAgent("promoter", current, { model: "code" });
    this.thought(agent, `Live ${faultClass} fixes without escalation fell below 60% on ${current}.`);
    yield this.uniform(1, 2);
    this.done(agent);
    this.counts.demotions += 1;
    this.setLadder(faultClass, current, previous, "demote", 2, 5, round(this.sampleCost(current), 5));
    yield* this.narrate(`${faultClass} moves back from ${current} to ${previous} after live regressions.`, "high");
  }

  private setLadder(faultClass: string, from_rung: Rung, to_rung: Rung, direction: "promote" | "demote", passed: number, total: number, mean_cost_usd: number): void {
    this.ladder[faultClass] = to_rung;
    const evidence = { passed, total, mean_cost_usd };
    this.ladderLog.push({ ts: this.now(), fault_class: faultClass, from_rung, to_rung, direction, evidence });
    this.emit({ type: "ladder_update", ts: 0, data: { fault_class: faultClass, from_rung, to_rung, direction, evidence } });
  }

  /** Starts a promotion cycle now, unless one is running. */
  runPromotion(): boolean {
    if (this.promoting) return false;
    this.spawn(this.promoteCycle());
    return true;
  }

  // operator-facing -------------------------------------------------------------------------------------

  goAway(): number {
    this.away = { since: this.now(), counts: { ...this.counts }, cost: this.costIncidents + this.costOther };
    return this.away.since;
  }

  briefing(): string {
    const away = this.away ?? { since: this.epoch, counts: { incidents: 0, closed: 0, verified: 0, rejected: 0, promotions: 0, demotions: 0 }, cost: 0 };
    const d = (k: keyof Counts) => this.counts[k] - away.counts[k];
    const spent = this.costIncidents + this.costOther - away.cost;
    return (
      `While you were away: ${d("incidents")} incidents opened, ${d("verified")} verified, ` +
      `${d("rejected")} fix claims rejected by the referee. ` +
      `${d("promotions")} promotions and ${d("demotions")} demotions. Spend $${spent.toFixed(2)}.`
    );
  }

  comeBack(): { since: number; briefing: string; ledger: Ledger } {
    const since = this.away?.since ?? this.epoch;
    const briefing = this.briefing();
    this.away = null;
    return { since, briefing, ledger: this.ledger() };
  }

  chaos(command: string): { node_id: string; fault_class: string }[] {
    const agent = this.spawnAgent("chaos", "nano");
    const text = command.toLowerCase();
    let injected: { node_id: string; fault_class: string }[];
    if (text.includes("storm")) {
      injected = this.storm(null);
    } else {
      const named = text.match(/r[1-3]-0[1-4]/g);
      const faultClass =
        Object.keys(FAULTS).find((c) => text.includes(c) || text.includes(c.replace(/_/g, " "))) ?? this.choice(TRAINING_FAULT_CLASSES);
      injected = named
        ? named.filter((n) => this.inject(n, faultClass)).map((node_id) => ({ node_id, fault_class: faultClass }))
        : this.injectAnywhere(null, faultClass);
    }
    const summary = injected.map((i) => `${i.fault_class} on ${i.node_id}`).join(", ") || "nothing to inject";
    this.thought(agent, `Parsed command: ${summary}.`);
    this.done(agent);
    return injected;
  }
}

function round(value: number, digits: number): number {
  const f = 10 ** digits;
  return Math.round(value * f) / f;
}
