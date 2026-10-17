"""Paths, endpoints, model registry and node layout shared across the project."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CRUSOE_BASE_URL = "https://api.inference.crusoecloud.com/v1"
API_KEY_ENV = "CRUSOE_API_KEY"

BUDGET_USD = float(os.environ.get("BUDGET_USD", "20"))
DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"

DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "understudy.db"
NODES_DIR = ROOT / "fleet" / "nodes"
MODELS_JSON = ROOT / "config" / "models.json"

SERVER_PORT = 8000
MOCK_PORT = 8765
NODE_PORT_BASE = 9200
SET_PORT_RANGE = (9200, 9299)

Rung = Literal["super", "lightning", "script"]
# Most expensive first; a promotion moves one step to the right.
RUNGS: tuple[Rung, ...] = ("super", "lightning", "script")


@dataclass(frozen=True)
class ModelSpec:
    key: str
    id: str
    input_per_m: float
    output_per_m: float
    cached_per_m: float


# IDs are case-sensitive and match GET /v1/models. Docs pages spell several of them differently.
# "ultra" is not enabled for every project; callers fall back to "super" when it is not listed.
MODELS: dict[str, ModelSpec] = {
    m.key: m
    for m in (
        ModelSpec("super", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B", 0.30, 2.40, 0.15),
        ModelSpec("lightning", "nvidia/Nemotron-3.5-Lightning-30B-A3B", 0.05, 0.20, 0.03),
        ModelSpec("nano", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", 0.05, 0.20, 0.03),
        ModelSpec("ultra", "nvidia/NVIDIA-Nemotron-3-Ultra-550B", 1.00, 3.20, 0.25),
        ModelSpec("omni", "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B", 0.30, 1.83, 0.30),
    )
}

TRAINING_FAULT_CLASSES = (
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
)
HELDOUT_FAULT_CLASSES = ("log_permission", "upstream_storm")
TRAINING_SEEDS = range(0, 10_000)
HELDOUT_SEED_START = 100_000


@dataclass(frozen=True)
class NodeSpec:
    id: str
    row: int
    col: int
    zone: Literal["live", "proving"]
    port: int


def node_layout() -> tuple[NodeSpec, ...]:
    """12 live nodes in 3 rows of 4 racks, then 4 proving-ground nodes."""
    specs: list[NodeSpec] = []
    for row in range(1, 4):
        for col in range(1, 5):
            specs.append(NodeSpec(f"r{row}-{col:02d}", row, col, "live", NODE_PORT_BASE + len(specs)))
    for col in range(1, 5):
        specs.append(NodeSpec(f"pg-{col:02d}", 1, col, "proving", NODE_PORT_BASE + len(specs)))
    return tuple(specs)


NODES = node_layout()
