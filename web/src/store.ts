import { create } from "zustand";

import { emit } from "./bus";
import type {
  AgentRole,
  AgentRung,
  Ledger,
  NodeInfo,
  NodeState,
  Probe,
  Rung,
  StreamEvent,
} from "./types";
import { ALL_CLASSES } from "./theme";

export interface AgentState {
  id: string;
  role: AgentRole;
  model: string;
  rung: AgentRung;
  incident_id: number | null;
  node_id: string | null;
  thought: string;
  thoughtAt: number;
  tool: { name: string; ok: boolean; args: string; result: string; at: number } | null;
  claim: string | null;
  spawnedAt: number;
  doneAt: number | null;
  escalatedAt: number | null;
  lastEventAt: number;
}

export interface Stamp {
  key: number;
  node_id: string;
  incident_id: number;
  verdict: "verified" | "rejected";
  quality: number | null;
  /** The first failing probe, for example "burst 3/4" (bursts passed), empty when the verdict is verified. */
  failing: string;
  /** What that probe reported, for example "connection refused". */
  failDetail: string;
  probes: Probe[];
  note: string;
  at: number;
}

export interface Caption {
  key: number;
  text: string;
  priority: "low" | "high";
  at: number;
}

export interface IncidentState {
  id: number;
  node_id: string;
  symptom: string;
  severity: "low" | "high";
  openedAt: number;
  closedAt: number | null;
  fault_class: string | null;
}

export interface LadderMove {
  key: number;
  fault_class: string;
  from_rung: Rung;
  to_rung: Rung;
  direction: "promote" | "demote";
  passed: number;
  total: number;
  mean_cost_usd: number;
  at: number;
}

export interface SkillNote {
  version: number;
  summary: string;
  at: number;
}

export const CAPTION_TTL_MS = 6000;
export const STAMP_TTL_MS = 7000;
export const AGENT_LINGER_MS = 3500;
/** The backend reports a node as down, opens its incident and marks it repairing in the same instant, so the red
 * state would never be on screen. Down stays visible for this long before the next status shows. */
export const DOWN_HOLD_MS = 3500;
const MAX_CAPTIONS = 4;
const MAX_MOVES = 30;
const SEEN_LIMIT = 4000;
const STALE_AGENT_MS = 4 * 60_000;
/** Hydrated agents look this old, so the scene seats them at their rack instead of launching them again. */
const HYDRATE_AGE_MS = 12_000;
const HYDRATE_TYPES = new Set<string>([
  "agent_spawned",
  "agent_thought",
  "tool_call",
  "fix_claimed",
  "agent_done",
  "incident_opened",
  "incident_closed",
  "skill_updated",
]);

export interface StreamState {
  connected: boolean;
  everConnected: boolean;
  lastEventAt: number;
  nodes: Record<string, NodeInfo>;
  nodeOrder: string[];
  /** Nodes whose red state is still being shown, with the status that arrived during the hold. */
  nodeHolds: Record<string, { until: number; next: NodeState | null }>;
  ledger: Ledger | null;
  /** Tokens served since the ledger last reported, so the counter can run between ledger events. */
  tokensSinceLedger: number;
  ladder: Record<string, Rung>;
  ladderMoves: LadderMove[];
  agents: Record<string, AgentState>;
  incidents: Record<number, IncidentState>;
  stamps: Record<string, Stamp>;
  captions: Caption[];
  skillNotes: Record<string, SkillNote>;
  muted: boolean;
  selectedClass: string | null;
  briefing: string | null;
  setConnected: (connected: boolean) => void;
  handle: (event: StreamEvent) => void;
  /** Applies recent history without publishing it, so a page opened mid-incident shows the agents already at work. */
  hydrate: (events: StreamEvent[]) => void;
  sweep: (now?: number) => void;
  setLadder: (ladder: Record<string, string>) => void;
  toggleMuted: () => void;
  selectClass: (name: string | null) => void;
  setBriefing: (text: string | null) => void;
  pushCaption: (text: string, priority?: "low" | "high") => void;
}

let counter = 0;
const seen = new Set<string>();
const seenOrder: string[] = [];

function firstSight(event: StreamEvent): boolean {
  const key = `${event.type}|${event.ts}|${JSON.stringify(event.data)}`;
  if (seen.has(key)) return false;
  seen.add(key);
  seenOrder.push(key);
  if (seenOrder.length > SEEN_LIMIT) seen.delete(seenOrder.shift()!);
  return true;
}

/** Clears the store and the duplicate filter. Used by tests and by a manual reset. */
export function resetStream(): void {
  seen.clear();
  seenOrder.length = 0;
  useStream.setState(useStream.getInitialState(), true);
}

function emptyLadder(): Record<string, Rung> {
  return Object.fromEntries(ALL_CLASSES.slice(0, 10).map((name) => [name, "super" as Rung]));
}

/** The commander is spawned once when the orchestrator starts, so a client that connects later only ever sees its
 * thoughts. Everything else is spawned per incident and arrives with its own agent_spawned. */
function withCommander(state: StreamState, agentId: string, now: number): StreamState["agents"] {
  if (state.agents[agentId] || !agentId.startsWith("commander")) return state.agents;
  const stub: AgentState = {
    id: agentId,
    role: "commander",
    model: "",
    rung: "super",
    incident_id: null,
    node_id: null,
    thought: "",
    thoughtAt: 0,
    tool: null,
    claim: null,
    spawnedAt: now - HYDRATE_AGE_MS,
    doneAt: null,
    escalatedAt: null,
    lastEventAt: now,
  };
  return { ...state.agents, [agentId]: stub };
}

function firstFailing(probes: Probe[]): { label: string; detail: string } {
  const bad = probes.find((p) => !p.ok);
  if (!bad) return { label: "", detail: "" };
  const bursts = probes.filter((p) => p.name.startsWith("burst"));
  const label = bad.name.startsWith("burst")
    ? `burst ${bursts.filter((p) => p.ok).length}/${Math.max(bursts.length, 4)}`
    : bad.name.replace(/_/g, " ");
  return { label, detail: bad.detail };
}

export const useStream = create<StreamState>((set, get) => ({
  connected: false,
  everConnected: false,
  lastEventAt: 0,
  nodes: {},
  nodeOrder: [],
  nodeHolds: {},
  ledger: null,
  tokensSinceLedger: 0,
  ladder: emptyLadder(),
  ladderMoves: [],
  agents: {},
  incidents: {},
  stamps: {},
  captions: [],
  skillNotes: {},
  muted: false,
  selectedClass: null,
  briefing: null,

  setConnected: (connected) =>
    set((state) => ({ connected, everConnected: state.everConnected || connected })),

  setLadder: (ladder) => set((state) => ({ ladder: { ...state.ladder, ...(ladder as Record<string, Rung>) } })),
  toggleMuted: () => set((state) => ({ muted: !state.muted })),
  selectClass: (name) => set({ selectedClass: name }),
  setBriefing: (text) => set({ briefing: text }),
  pushCaption: (text, priority = "low") =>
    set((state) => ({
      captions: [...state.captions, { key: ++counter, text, priority, at: Date.now() }].slice(-MAX_CAPTIONS),
    })),

  handle: (event) => {
    if (!firstSight(event)) return;
    const now = Date.now();
    set((state) => reduce(state, event, now));
    emit(event);
  },

  hydrate: (events) => {
    const now = Date.now() - HYDRATE_AGE_MS;
    const ordered = events
      .filter((event) => HYDRATE_TYPES.has(event.type))
      .sort((a, b) => a.ts - b.ts);
    set((state) => {
      let next: StreamState = state;
      for (const event of ordered) {
        if (!firstSight(event)) continue;
        next = { ...next, ...reduce(next, event, now) };
      }
      const agents = Object.fromEntries(Object.entries(next.agents).filter(([, a]) => a.doneAt === null));
      return { ...next, agents, lastEventAt: state.lastEventAt };
    });
  },

  sweep: (now = Date.now()) => {
    const state = get();
    let agents = state.agents;
    for (const agent of Object.values(state.agents)) {
      const gone = agent.doneAt !== null && now - agent.doneAt > AGENT_LINGER_MS;
      const stale = agent.role !== "commander" && now - agent.lastEventAt > STALE_AGENT_MS;
      if (gone || stale) {
        if (agents === state.agents) agents = { ...agents };
        delete agents[agent.id];
      }
    }
    let stamps = state.stamps;
    for (const stamp of Object.values(state.stamps)) {
      if (now - stamp.at > STAMP_TTL_MS + 1000) {
        if (stamps === state.stamps) stamps = { ...stamps };
        delete stamps[stamp.node_id];
      }
    }
    let nodes = state.nodes;
    let nodeHolds = state.nodeHolds;
    for (const [id, hold] of Object.entries(state.nodeHolds)) {
      if (now < hold.until) continue;
      if (nodeHolds === state.nodeHolds) nodeHolds = { ...nodeHolds };
      delete nodeHolds[id];
      const node = nodes[id];
      if (node && hold.next && hold.next !== node.status) {
        if (nodes === state.nodes) nodes = { ...nodes };
        nodes[id] = { ...node, status: hold.next };
      }
    }
    const captions = state.captions.filter((c) => now - c.at < CAPTION_TTL_MS + 1500);
    const patch: Partial<StreamState> = {};
    if (nodes !== state.nodes) patch.nodes = nodes;
    if (nodeHolds !== state.nodeHolds) patch.nodeHolds = nodeHolds;
    if (agents !== state.agents) patch.agents = agents;
    if (stamps !== state.stamps) patch.stamps = stamps;
    if (captions.length !== state.captions.length) patch.captions = captions;
    if (Object.keys(patch).length) set(patch);
  },
}));

function reduce(state: StreamState, event: StreamEvent, now: number): Partial<StreamState> {
  const base = { lastEventAt: now };
  switch (event.type) {
    case "fleet_snapshot": {
      const nodes: Record<string, NodeInfo> = {};
      const holds = { ...state.nodeHolds };
      const order: string[] = [];
      for (const node of event.data.nodes) {
        const hold = holds[node.id];
        if (hold && now < hold.until && node.status !== "down") {
          nodes[node.id] = { ...node, status: "down" };
          holds[node.id] = { ...hold, next: node.status };
        } else {
          nodes[node.id] = node;
          delete holds[node.id];
        }
        order.push(node.id);
      }
      return { ...base, nodes, nodeOrder: order, nodeHolds: holds };
    }
    case "node_status": {
      const id = event.data.node_id;
      const node = state.nodes[id];
      if (!node) return base;
      const status = event.data.status as NodeState;
      const hold = state.nodeHolds[id];
      if (hold && now < hold.until) {
        return { ...base, nodeHolds: { ...state.nodeHolds, [id]: { ...hold, next: status === "down" ? null : status } } };
      }
      if (node.status === status) return base;
      const nodeHolds = { ...state.nodeHolds };
      delete nodeHolds[id];
      if (status === "down") nodeHolds[id] = { until: now + DOWN_HOLD_MS, next: null };
      return { ...base, nodes: { ...state.nodes, [id]: { ...node, status } }, nodeHolds };
    }
    case "tokens":
      return { ...base, tokensSinceLedger: state.tokensSinceLedger + event.data.tokens };
    case "incident_opened": {
      const d = event.data;
      return {
        ...base,
        incidents: {
          ...state.incidents,
          [d.incident_id]: {
            id: d.incident_id,
            node_id: d.node_id,
            symptom: d.symptom,
            severity: d.severity,
            openedAt: now,
            closedAt: null,
            fault_class: null,
          },
        },
      };
    }
    case "incident_closed": {
      const d = event.data;
      const prev = state.incidents[d.incident_id];
      return {
        ...base,
        incidents: {
          ...state.incidents,
          [d.incident_id]: {
            id: d.incident_id,
            node_id: d.node_id,
            symptom: prev?.symptom ?? "",
            severity: prev?.severity ?? "low",
            openedAt: prev?.openedAt ?? now,
            closedAt: now,
            fault_class: d.fault_class,
          },
        },
      };
    }
    case "agent_spawned": {
      const d = event.data;
      if (state.agents[d.agent_id]) return base;
      const agent: AgentState = {
        id: d.agent_id,
        role: d.role,
        model: d.model,
        rung: d.rung,
        incident_id: d.incident_id ?? null,
        node_id: d.node_id ?? null,
        thought: "",
        thoughtAt: 0,
        tool: null,
        claim: null,
        spawnedAt: now,
        doneAt: null,
        escalatedAt: null,
        lastEventAt: now,
      };
      return { ...base, agents: { ...state.agents, [d.agent_id]: agent } };
    }
    case "agent_thought": {
      const agents = withCommander(state, event.data.agent_id, now);
      const agent = agents[event.data.agent_id];
      if (!agent) return base;
      return {
        ...base,
        agents: { ...agents, [agent.id]: { ...agent, thought: event.data.text, thoughtAt: now, lastEventAt: now } },
      };
    }
    case "tool_call": {
      const d = event.data;
      const agents = withCommander(state, d.agent_id, now);
      const agent = agents[d.agent_id];
      if (!agent) return base;
      const tool = { name: d.tool, ok: d.ok, args: d.args_summary, result: d.result_summary, at: now };
      return { ...base, agents: { ...agents, [agent.id]: { ...agent, tool, lastEventAt: now } } };
    }
    case "fix_claimed": {
      const agent = state.agents[event.data.agent_id];
      if (!agent) return base;
      return {
        ...base,
        agents: { ...state.agents, [agent.id]: { ...agent, claim: event.data.summary, lastEventAt: now } },
      };
    }
    case "verification": {
      const d = event.data;
      const failure = d.verdict === "rejected" ? firstFailing(d.probes) : { label: "", detail: "" };
      const stamp: Stamp = {
        key: ++counter,
        node_id: d.node_id,
        incident_id: d.incident_id,
        verdict: d.verdict,
        quality: d.quality ?? null,
        failing: failure.label,
        failDetail: failure.detail,
        probes: d.probes,
        note: d.note ?? "",
        at: now,
      };
      return { ...base, stamps: { ...state.stamps, [d.node_id]: stamp } };
    }
    case "escalation": {
      const agents = { ...state.agents };
      for (const agent of Object.values(state.agents)) {
        if (agent.incident_id === event.data.incident_id && agent.role === "responder" && agent.doneAt === null) {
          agents[agent.id] = { ...agent, escalatedAt: now, lastEventAt: now };
        }
      }
      return { ...base, agents };
    }
    case "agent_done": {
      const agent = state.agents[event.data.agent_id];
      if (!agent || agent.doneAt !== null) return base;
      return { ...base, agents: { ...state.agents, [agent.id]: { ...agent, doneAt: now, lastEventAt: now } } };
    }
    case "skill_updated": {
      const d = event.data;
      return {
        ...base,
        skillNotes: { ...state.skillNotes, [d.fault_class]: { version: d.version, summary: d.summary, at: now } },
      };
    }
    case "ladder_update": {
      const d = event.data;
      const move: LadderMove = {
        key: ++counter,
        fault_class: d.fault_class,
        from_rung: d.from_rung,
        to_rung: d.to_rung,
        direction: d.direction,
        passed: d.evidence.passed,
        total: d.evidence.total,
        mean_cost_usd: d.evidence.mean_cost_usd,
        at: now,
      };
      return {
        ...base,
        ladder: { ...state.ladder, [d.fault_class]: d.to_rung },
        ladderMoves: [...state.ladderMoves, move].slice(-MAX_MOVES),
      };
    }
    case "narration": {
      const d = event.data;
      return {
        ...base,
        captions: [...state.captions, { key: ++counter, text: d.text, priority: d.priority, at: now }].slice(-MAX_CAPTIONS),
      };
    }
    case "ledger":
      return {
        ...base,
        ledger: event.data,
        tokensSinceLedger: 0,
        ladder: { ...state.ladder, ...event.data.ladder },
      };
    default:
      return base;
  }
}

/** Tokens served as the counter should show it: the last ledger value plus what has streamed since. */
export function tokensServed(state: Pick<StreamState, "ledger" | "tokensSinceLedger">): number {
  return (state.ledger?.tokens_served ?? 0) + state.tokensSinceLedger;
}

export function agentsOnNode(state: Pick<StreamState, "agents">, nodeId: string): AgentState[] {
  return Object.values(state.agents).filter((a) => a.node_id === nodeId);
}
