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
  /** The first failing probe, for example "burst 3/4", empty when the verdict is verified. */
  failing: string;
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
const MAX_CAPTIONS = 4;
const MAX_MOVES = 30;
const SEEN_LIMIT = 4000;
const STALE_AGENT_MS = 4 * 60_000;

export interface StreamState {
  connected: boolean;
  everConnected: boolean;
  lastEventAt: number;
  nodes: Record<string, NodeInfo>;
  nodeOrder: string[];
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

function emptyLadder(): Record<string, Rung> {
  return Object.fromEntries(ALL_CLASSES.slice(0, 10).map((name) => [name, "super" as Rung]));
}

function firstFailing(probes: Probe[]): string {
  const bad = probes.find((p) => !p.ok);
  if (!bad) return "";
  const burst = /^burst_?(\d+)/.exec(bad.name);
  if (burst) return `burst ${burst[1]}/${probes.filter((p) => p.name.startsWith("burst")).length || 4}`;
  return bad.name.replace(/_/g, " ");
}

export const useStream = create<StreamState>((set, get) => ({
  connected: false,
  everConnected: false,
  lastEventAt: 0,
  nodes: {},
  nodeOrder: [],
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

  sweep: (now = Date.now()) => {
    const state = get();
    let agents = state.agents;
    for (const agent of Object.values(state.agents)) {
      const gone = agent.doneAt !== null && now - agent.doneAt > AGENT_LINGER_MS;
      const stale = now - agent.lastEventAt > STALE_AGENT_MS;
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
    const captions = state.captions.filter((c) => now - c.at < CAPTION_TTL_MS + 1500);
    const patch: Partial<StreamState> = {};
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
      const order: string[] = [];
      for (const node of event.data.nodes) {
        nodes[node.id] = node;
        order.push(node.id);
      }
      return { ...base, nodes, nodeOrder: order };
    }
    case "node_status": {
      const node = state.nodes[event.data.node_id];
      if (!node || node.status === event.data.status) return base;
      return { ...base, nodes: { ...state.nodes, [node.id]: { ...node, status: event.data.status as NodeState } } };
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
      const agent = state.agents[event.data.agent_id];
      if (!agent) return base;
      return {
        ...base,
        agents: { ...state.agents, [agent.id]: { ...agent, thought: event.data.text, thoughtAt: now, lastEventAt: now } },
      };
    }
    case "tool_call": {
      const d = event.data;
      const agent = state.agents[d.agent_id];
      if (!agent) return base;
      const tool = { name: d.tool, ok: d.ok, args: d.args_summary, result: d.result_summary, at: now };
      return { ...base, agents: { ...state.agents, [agent.id]: { ...agent, tool, lastEventAt: now } } };
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
      const stamp: Stamp = {
        key: ++counter,
        node_id: d.node_id,
        incident_id: d.incident_id,
        verdict: d.verdict,
        quality: d.quality ?? null,
        failing: d.verdict === "rejected" ? firstFailing(d.probes) : "",
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
