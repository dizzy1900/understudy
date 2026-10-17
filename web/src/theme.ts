import type { AgentRung, NodeState, Rung } from "./types";

export const COLORS = {
  bg: "#000000",
  healthy: "#3a3a3a",
  incident: "#ff4d3d",
  repairing: "#f5a524",
  accent: "#CEEB13",
  verified: "#CEEB13",
  text: "#ffffff",
  dim: "#8c8c8c",
} as const;

export const RUNG_COLORS: Record<AgentRung, string> = {
  super: "#ffffff",
  lightning: "#CEEB13",
  script: "#8c8c8c",
  ultra: "#bdbdbd",
  nano: "#6e6e6e",
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
