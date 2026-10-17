import type { AgentRung, NodeState, Rung } from "./types";

export const COLORS = {
  bg: "#0b0b0c",
  healthy: "#6f8f93",
  incident: "#e5484d",
  repairing: "#f5a524",
  accent: "#5b8def",
  verified: "#46a758",
  text: "#ededef",
  dim: "#8b8b90",
} as const;

export const RUNG_COLORS: Record<AgentRung, string> = {
  super: "#d4a72c",
  lightning: "#5b8def",
  script: "#e6e6e6",
  ultra: "#9a8ccf",
  nano: "#5fb3a8",
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
