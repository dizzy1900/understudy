/** Synthetic fault stories for the in-browser demo, ported from scripts/mock_events.py. Not measurements. */

export interface DemoFault {
  symptom: string;
  probe_error: string;
  pitfall: string;
  claim: string;
  /** [thought, tool, args_summary, result_summary] */
  steps: [string, string, string, string][];
  fails_at?: "probe_a" | "burst";
}

export const TRAINING_FAULT_CLASSES = [
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

export const HIGH_SEVERITY = new Set(["port_squatter", "crashed_worker", "config_syntax", "log_permission"]);

const restartProbe: [string, string, string, string][] = [
  ["Restart the node.", "restart_node", "", "node up"],
  ["Probe once.", "probe_once", "", "200 OK"],
];

export const FAULTS: Record<string, DemoFault> = {
  model_id_case: {
    symptom: "upstream 404 model_not_found",
    probe_error: "HTTP 404 model_not_found",
    pitfall: "model ids are case-sensitive; copy the id from list_models",
    claim: "Corrected the model id casing and restarted; probe returns 200",
    steps: [
      ["Probe returns 404 model_not_found. Check which model id goes upstream.", "tail_log", "n=40", "upstream 404: model 'nvidia/nemotron-3-nano-30b-a3b' not found"],
      ["Compare with the ids the API lists.", "list_models", "", "canonical id nvidia/Nemotron-3-Nano-30B-A3B"],
      ["Casing differs. Fix the model id in the config.", "write_config", "model: nvidia/Nemotron-3-Nano-30B-A3B", "config written"],
      ["Restart so the node reads the new config.", "restart_node", "", "node up"],
      ["One probe to confirm before claiming.", "probe_once", "", "200 OK, model matches"],
    ],
  },
  stale_endpoint: {
    symptom: "upstream host unreachable",
    probe_error: "502 upstream connect error",
    pitfall: "check the hostname, not only the path",
    claim: "Pointed upstream_base_url at the current hostname and restarted",
    steps: [
      ["Probe fails at connect. Look for the upstream error.", "tail_log", "n=40", "ConnectError: api.inference.crusoe.ai name not resolved"],
      ["That hostname looks outdated. Read the config.", "read_config", "", "upstream_base_url: https://api.inference.crusoe.ai/v1"],
      ["Set the current endpoint.", "write_config", "upstream_base_url: https://api.inference.crusoecloud.com/v1", "config written"],
      ...restartProbe,
    ],
  },
  starved_tokens: {
    symptom: "empty content, finish_reason=length",
    probe_error: "200 but empty content",
    pitfall: "thinking spends max_tokens; keep at least 2048 when it is on",
    claim: "Raised max_tokens so reasoning no longer starves the answer",
    steps: [
      ["200 responses with empty content usually mean the token budget ran out.", "tail_log", "n=40", "finish_reason=length completion_tokens=16 content=''"],
      ["Read the token settings.", "read_config", "", "max_tokens: 16, enable_thinking: true"],
      ["Raise max_tokens well above the reasoning cost.", "write_config", "max_tokens: 2048", "config written"],
      ["Restart the node.", "restart_node", "", "node up"],
      ["Probe once.", "probe_once", "", "200 OK, content 'OK'"],
    ],
  },
  forbidden_param: {
    symptom: "403 parameter not allowed",
    probe_error: "HTTP 403 parameter 'thinking' is not allowed",
    pitfall: "remove the whole thinking key, not just its value",
    claim: "Removed the top-level thinking parameter from extra_body",
    steps: [
      ["403 from upstream. Read the error body in the log.", "tail_log", "n=40", "403 Request blocked: parameter 'thinking' is not allowed"],
      ["Find where the parameter is set.", "read_config", "", "extra_body: {thinking: {type: enabled}}"],
      ["Drop it and use chat_template_kwargs only.", "write_config", "extra_body: chat_template_kwargs", "config written"],
      ...restartProbe,
    ],
  },
  bad_key_env: {
    symptom: "401 invalid api key",
    probe_error: "HTTP 401 Unauthorized",
    pitfall: "the variable has to exist in the node's environment, not only the shell",
    claim: "Pointed api_key_env at the variable that is set and restarted",
    steps: [
      ["401 means the key did not reach upstream. Check the log.", "tail_log", "n=40", "401 Unauthorized; env var CRUSOE_KEY is not set"],
      ["See which variable the config names.", "read_config", "", "api_key_env: CRUSOE_KEY"],
      ["Use the variable the fleet actually exports.", "write_config", "api_key_env: CRUSOE_API_KEY", "config written"],
      ...restartProbe,
    ],
  },
  port_squatter: {
    symptom: "node cannot bind its port",
    probe_error: "connection refused",
    pitfall: "kill only fleet-tracked pids; otherwise move the node to another port",
    claim: "Killed the squatter holding the port and started the node",
    steps: [
      ["Node is not answering. Look at the boot log.", "tail_log", "n=40", "OSError: address already in use :9204"],
      ["Who holds the port?", "port_owner", "", "pid 48213 (fleet dummy)"],
      ["It is fleet-tracked, so it is safe to kill.", "kill_squatter", "pid=48213", "killed"],
      ["Start the node.", "start_node", "", "node up on :9204"],
      ["Probe once.", "probe_once", "", "200 OK"],
    ],
  },
  disk_full: {
    symptom: "507 quota exceeded",
    probe_error: "HTTP 507 quota exceeded",
    pitfall: "keep the newest checkpoint when clearing data/",
    claim: "Cleared old checkpoints under data/ and kept the newest",
    steps: [
      ["507 points at storage. Check the log.", "tail_log", "n=40", "507 quota exceeded: data/ 1.2 GB of 1.0 GB"],
      ["Look at what fills data/.", "list_dir", "data", "412 checkpoint files"],
      ["Delete old checkpoints, keeping the newest.", "delete_files", "data/ckpt-*.bin", "deleted 411 files"],
      ["Probe once.", "probe_once", "", "200 OK"],
    ],
  },
  crashed_worker: {
    symptom: "node process not running",
    probe_error: "connection refused",
    pitfall: "a dead process leaves no error in the log; check node_status first",
    claim: "Started the node process again; probe returns 200",
    steps: [
      ["Connection refused. Is the process alive?", "node_status", "", "state=exited code=-9"],
      ["Killed by signal. Check for a config problem before restarting.", "tail_log", "n=40", "no errors before exit"],
      ["Config is fine. Start the node.", "start_node", "", "node up"],
      ["Probe once.", "probe_once", "", "200 OK"],
    ],
  },
  config_syntax: {
    symptom: "node fails to boot, YAML error",
    probe_error: "connection refused",
    pitfall: "read the line number in the log before editing",
    claim: "Fixed the YAML indentation on line 7 and started the node",
    steps: [
      ["Node does not boot. Read the error.", "tail_log", "n=40", "yaml.scanner.ScannerError: line 7, column 3"],
      ["Open the config around line 7.", "read_config", "", "line 7: mis-indented max_tokens"],
      ["Fix the indentation.", "write_config", "max_tokens: 2048 (indent fixed)", "config written"],
      ["Start the node.", "start_node", "", "node up"],
      ["Probe once.", "probe_once", "", "200 OK"],
    ],
  },
  leaky_worker: {
    symptom: "worker exits after a few requests",
    probe_error: "burst failed after 2 of 4 requests",
    pitfall: "a plain restart passes the first probe and fails again within a few requests",
    claim: "Disabled the worker recycle limit that made the worker exit",
    fails_at: "burst",
    steps: [
      ["Node answers, then drops. Look for worker exits.", "tail_log", "n=80", "worker recycled after 3 requests; process exited"],
      ["Check the worker settings.", "read_config", "", "worker_recycle_after: 3"],
      ["A restart alone will repeat this. Remove the recycle limit.", "write_config", "worker_recycle_after: 0", "config written"],
      ...restartProbe,
    ],
  },
  log_permission: {
    symptom: "node cannot start, log not writable",
    probe_error: "connection refused",
    pitfall: "fix the file mode instead of redirecting logs",
    claim: "Restored write permission on logs/node.log and started the node",
    steps: [
      ["Node will not start. Check the boot log.", "tail_log", "n=40", "PermissionError: logs/node.log"],
      ["Look at the log directory.", "list_dir", "logs", "node.log mode 000"],
      ["Restore the mode.", "fix_permissions", "logs/node.log", "mode 644"],
      ["Start the node.", "start_node", "", "node up"],
      ["Probe once.", "probe_once", "", "200 OK"],
    ],
  },
  upstream_storm: {
    symptom: "429 storm from upstream",
    probe_error: "HTTP 429 too many requests",
    pitfall: "lower concurrency and add backoff; retries alone make the storm worse",
    claim: "Lowered worker concurrency and enabled backoff",
    steps: [
      ["Many 429s in a short window. Check the rate.", "tail_log", "n=80", "429 x38 in 10s; concurrency=64"],
      ["Read the worker settings.", "read_config", "", "worker_concurrency: 64, backoff: false"],
      ["Reduce concurrency and turn backoff on.", "write_config", "worker_concurrency: 4, backoff: true", "config written"],
      ...restartProbe,
    ],
  },
};

export const MODEL_IDS: Record<string, string> = {
  super: "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
  lightning: "nvidia/Nemotron-3.5-Lightning-30B-A3B",
  nano: "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
  ultra: "nvidia/NVIDIA-Nemotron-3-Ultra-550B",
  script: "fix_script",
};
