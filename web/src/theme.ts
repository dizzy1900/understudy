import type { AgentRung, NodeState, Rung } from "./types";

export const COLORS = {
  bg: "#07080B",
  healthy: "#3EE6FF",
  incident: "#FF3B3B",
  repairing: "#FFB020",
  accent: "#FF7A1A",
  verified: "#38E08C",
  text: "#E8ECF4",
  dim: "#8791A3",
} as const;

export const RUNG_COLORS: Record<AgentRung, string> = {
  super: "#F5C451",
  lightning: "#4C8DFF",
  script: "#F4F6FA",
  ultra: "#A78BFA",
  nano: "#2DD4BF",
};

export const STATUS_COLORS: Record<NodeState, string> = {
  healthy: COLORS.healthy,
  degraded: COLORS.repairing,
  down: COLORS.incident,
  repairing: COLORS.repairing,
};

export const RUNGS: Rung[] = ["super", "lightning", "script"];

export const TRAINING_CLASSES = [
  "model_id_case",
  "stale_endpoint",
  "starved_tokens",
  "forbidden_param",
  "bad_key_env",
  "port_squatter",
  "disk_full",
  "crashed_worker",
  "config_syntax",
  "leaky_worker",
] as const;

export const HELDOUT_CLASSES = ["log_permission", "upstream_storm"] as const;

export const ALL_CLASSES: string[] = [...TRAINING_CLASSES, ...HELDOUT_CLASSES];

export const LIVE_NODE_IDS: string[] = [1, 2, 3].flatMap((r) => [1, 2, 3, 4].map((c) => `r${r}-0${c}`));
export const PROVING_NODE_IDS: string[] = [1, 2, 3, 4].map((c) => `pg-0${c}`);

export function prettyClass(name: string): string {
  return name.replace(/_/g, " ");
}

export function rungColor(rung: string | undefined): string {
  return RUNG_COLORS[(rung ?? "super") as AgentRung] ?? RUNG_COLORS.super;
}
