"""Command-line control of nodes."""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import httpx

from config import settings
from understudy.db import Database
from understudy.fleet import faults
from understudy.fleet.referee import for_supervisor
from understudy.fleet.supervisor import NodeStatus, Supervisor


def _nodes(sup: Supervisor, names: list[str]) -> list[str]:
    if not names or names == ["all"]:
        return sup.node_ids()
    unknown = [n for n in names if n not in sup.specs]
    if unknown:
        raise SystemExit(f"unknown node(s): {', '.join(unknown)}; known: {', '.join(sup.node_ids())}")
    return names


def _stats(port: int) -> dict[str, Any]:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/local/stats", timeout=0.7).json()
    except (httpx.HTTPError, ValueError):
        return {}


def print_table(sup: Supervisor, statuses: list[NodeStatus]) -> None:
    header = f"{'node':<7} {'zone':<7} {'state':<8} {'pid':>7} {'port':>5} {'exit':>5} {'up':>7} {'reqs':>5} {'tokens':>7}"
    print(header)
    for st in statuses:
        stats = _stats(st.port) if st.state == "running" else {}
        up = f"{st.uptime_s:.0f}s" if st.uptime_s is not None else "-"
        code = "-" if st.exit_code is None or st.state == "running" else str(st.exit_code)
        print(
            f"{st.node_id:<7} {sup.specs[st.node_id].zone:<7} {st.state:<8} {st.pid or '-':>7} {st.port:>5} {code:>5} "
            f"{up:>7} {stats.get('requests_total', '-'):>5} {stats.get('tokens_served', '-'):>7}"
        )


async def cmd_up(sup: Supervisor, args: argparse.Namespace) -> int:
    sup.ensure_all()
    nodes = _nodes(sup, args.nodes)
    statuses = await asyncio.gather(*(sup.start(n) for n in nodes))
    print_table(sup, list(statuses))
    return 0 if all(s.state == "running" for s in statuses) else 1


async def cmd_down(sup: Supervisor, args: argparse.Namespace) -> int:
    statuses = await asyncio.gather(*(sup.stop(n) for n in _nodes(sup, args.nodes)))
    print_table(sup, list(statuses))
    return 0


async def cmd_status(sup: Supervisor, args: argparse.Namespace) -> int:
    print_table(sup, sup.statuses())
    squatters = sup.squatters()
    for pid, info in squatters.items():
        print(f"squatter pid={pid} port={info['port']} (spawned for {info['node_id']})")
    return 0


async def cmd_inject(sup: Supervisor, args: argparse.Namespace) -> int:
    db = await Database.open()
    try:
        fault = await faults.inject(sup, args.node, args.fault_class, args.seed, db)
    finally:
        await db.close()
    print(f"injected {fault.fault_class} (#{fault.number}) on {fault.node_id} seed={fault.seed}")
    print(f"node: {sup.status(args.node).describe()}")
    return 0


async def cmd_storm(sup: Supervisor, args: argparse.Namespace) -> int:
    seed = args.seed if args.seed is not None else faults.random_seed("heldout" if args.heldout else "train")
    db = await Database.open()
    try:
        nodes = await faults.pick_storm_nodes(sup, seed, args.rows or None, args.count, db)
        if not nodes:
            print("no eligible nodes: every live node in those rows is down or already faulted")
            return 1
        injected = await faults.storm(sup, nodes, seed, db, heldout=args.heldout)
    finally:
        await db.close()
    print(f"storm seed={seed}: {len(injected)} faults")
    for fault in injected:
        print(f"  {fault.node_id}: {fault.fault_class} (seed {fault.seed})")
    return 0


async def cmd_verify(sup: Supervisor, args: argparse.Namespace) -> int:
    verdict = await for_supervisor(sup).verify(args.node)
    print(f"{args.node}: {verdict.verdict.upper()} - {verdict.note}")
    print(verdict.evidence())
    return 0 if verdict.verified else 1


async def cmd_reset(sup: Supervisor, args: argparse.Namespace) -> int:
    sup.ensure_all()
    if args.node == "all":
        statuses = await sup.reset_all()
        db = await Database.open()
        try:
            for node_id in sup.node_ids():
                await db.clear_fault_truth(node_id)
        finally:
            await db.close()
    else:
        _nodes(sup, [args.node])
        statuses = [await sup.reset(args.node)]
        db = await Database.open()
        try:
            await db.clear_fault_truth(args.node)
        finally:
            await db.close()
    print_table(sup, list(statuses))
    return 0


async def cmd_logs(sup: Supervisor, args: argparse.Namespace) -> int:
    _nodes(sup, [args.node])
    for line in sup.tail_log(args.node, args.n):
        print(line)
    if args.stderr:
        print("--- stderr")
        for line in sup.stderr_tail(args.node, args.n):
            print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nodectl", description="Control fleet nodes.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start nodes (all by default)")
    up.add_argument("nodes", nargs="*")
    down = sub.add_parser("down", help="stop nodes (all by default)")
    down.add_argument("nodes", nargs="*")
    sub.add_parser("status", help="show every node")

    inject = sub.add_parser("inject", help="inject a fault class (name or number 1-12)")
    inject.add_argument("node")
    inject.add_argument("fault_class")
    inject.add_argument("--seed", type=int)

    storm = sub.add_parser("storm", help="inject 3-6 mixed faults on live nodes in the given rack rows")
    storm.add_argument("rows", nargs="*", type=int)
    storm.add_argument("--seed", type=int)
    storm.add_argument("--count", type=int)
    storm.add_argument("--heldout", action="store_true", help="draw from all 12 classes and held-out seeds")

    verify = sub.add_parser("verify", help="run the referee on a node")
    verify.add_argument("node")

    reset = sub.add_parser("reset", help="restore a node (or all) to the canonical state")
    reset.add_argument("node", help="node id or 'all'")

    logs = sub.add_parser("logs", help="tail logs/node.log")
    logs.add_argument("node")
    logs.add_argument("-n", type=int, default=40)
    logs.add_argument("--stderr", action="store_true", help="also show the process stderr")
    return parser


COMMANDS = {
    "up": cmd_up,
    "down": cmd_down,
    "status": cmd_status,
    "inject": cmd_inject,
    "storm": cmd_storm,
    "verify": cmd_verify,
    "reset": cmd_reset,
    "logs": cmd_logs,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sup = Supervisor()
    try:
        return asyncio.run(COMMANDS[args.cmd](sup, args))
    except faults.FaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
