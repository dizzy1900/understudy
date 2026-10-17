export type Rung = "super" | "lightning" | "script";
export type AgentRung = Rung | "ultra" | "nano";
export type AgentRole =
  | "commander"
  | "responder"
  | "verifier"
  | "watchdog"
  | "librarian"
  | "promoter"
  | "narrator"
  | "chaos"
  | "triage";
export type NodeState = "healthy" | "degraded" | "down" | "repairing";
export type Zone = "live" | "proving";

export interface NodeInfo {
  id: string;
  row: number;
  col: number;
  zone: Zone;
  status: NodeState;
  tokens_per_min: number;
  last_probe_ms: number;
}

export interface Probe {
  name: string;
  ok: boolean;
  ms: number;
  detail: string;
}

export interface RungStat {
  incidents: number;
  cost_usd: number;
}

export interface Ledger {
  since: number;
  incidents_total: number;
  verified: number;
  rejected_claims: number;
  promotions: number;
  demotions: number;
  cost_total_usd: number;
  cost_per_incident_usd: number;
  tokens_served: number;
  cache_hit_rate: number;
  by_rung: Partial<Record<Rung, RungStat>>;
  ladder: Record<string, Rung>;
}

export interface LadderEvidence {
  passed: number;
  total: number;
  mean_cost_usd: number;
}

export type StreamEvent =
  | { type: "fleet_snapshot"; ts: number; data: { nodes: NodeInfo[] } }
  | { type: "node_status"; ts: number; data: { node_id: string; status: NodeState; reason?: string | null } }
  | { type: "tokens"; ts: number; data: { node_id: string; tokens: number } }
  | {
      type: "incident_opened";
      ts: number;
      data: { incident_id: number; node_id: string; symptom: string; severity: "low" | "high" };
    }
  | {
      type: "agent_spawned";
      ts: number;
      data: {
        agent_id: string;
        role: AgentRole;
        model: string;
        rung: AgentRung;
        incident_id?: number | null;
        node_id?: string | null;
      };
    }
  | { type: "agent_thought"; ts: number; data: { agent_id: string; text: string } }
  | {
      type: "tool_call";
      ts: number;
      data: { agent_id: string; tool: string; args_summary: string; ok: boolean; result_summary: string };
    }
  | { type: "fix_claimed"; ts: number; data: { incident_id: number; agent_id: string; summary: string } }
  | {
      type: "verification";
      ts: number;
      data: {
        incident_id: number;
        node_id: string;
        verdict: "verified" | "rejected";
        probes: Probe[];
        quality?: number | null;
        note?: string;
      };
    }
  | {
      type: "escalation";
      ts: number;
      data: { incident_id: number; from_rung: Rung; to_rung: Rung; reason: string };
    }
  | {
      type: "incident_closed";
      ts: number;
      data: {
        incident_id: number;
        node_id: string;
        fault_class: string;
        rung: Rung;
        steps: number;
        cost_usd: number;
        seconds: number;
      };
    }
  | { type: "skill_updated"; ts: number; data: { fault_class: string; version: number; summary: string } }
  | {
      type: "ladder_update";
      ts: number;
      data: {
        fault_class: string;
        from_rung: Rung;
        to_rung: Rung;
        direction: "promote" | "demote";
        evidence: LadderEvidence;
      };
    }
  | { type: "narration"; ts: number; data: { text: string; priority: "low" | "high" } }
  | ({ type: "ledger"; ts: number; data: Ledger })
  | { type: "agent_done"; ts: number; data: { agent_id: string } };

export type EventType = StreamEvent["type"];
export type EventOf<T extends EventType> = Extract<StreamEvent, { type: T }>;

export interface SkillCard {
  fault_class: string;
  version?: number;
  signature?: string;
  diagnosis?: string;
  fix_steps?: string[] | string;
  pitfalls?: string[] | string;
  evidence?: string;
  summary?: string;
  [key: string]: unknown;
}

export interface LadderLogEntry {
  ts: number;
  fault_class: string;
  from_rung: Rung;
  to_rung: Rung;
  direction: "promote" | "demote";
  evidence?: LadderEvidence;
  passed?: number;
  total?: number;
  mean_cost_usd?: number;
}
