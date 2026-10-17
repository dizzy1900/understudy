import pytest

from config import settings
from tests.fleet_support import FakeUpstream, make_supervisor
from understudy.db import Database
from understudy.fleet import faults, nodectl

pytestmark = pytest.mark.anyio


class StubSupervisor:
    """Just enough of Supervisor for pick_storm_nodes."""

    def __init__(self, down=()):
        self.specs = {s.id: s for s in settings.NODES}
        self.down = set(down)

    def status(self, node_id):
        return type("S", (), {"state": "exited" if node_id in self.down else "running"})()


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "t.db")
    yield database
    await database.close()


async def test_pick_storm_nodes_is_seeded_and_respects_rows_and_state(db):
    sup = StubSupervisor(down={"r1-01"})
    first = await faults.pick_storm_nodes(sup, 5)
    assert first == await faults.pick_storm_nodes(sup, 5)
    assert 3 <= len(first) <= 6
    assert all(n.startswith("r") for n in first) and "r1-01" not in first

    row2 = await faults.pick_storm_nodes(sup, 5, rows=[2], count=6)
    assert sorted(row2) == ["r2-01", "r2-02", "r2-03", "r2-04"]

    two_rows = await faults.pick_storm_nodes(sup, 5, rows=2, count=6)
    assert {n[:2] for n in two_rows} <= {"r1", "r2", "r3"} and len({n[:2] for n in two_rows}) <= 2

    await db.add_fault_truth("r2-01", "disk_full", 1, {})
    await db.add_fault_truth("r2-02", "disk_full", 1, {})
    remaining = await faults.pick_storm_nodes(sup, 5, rows=[2], count=6, db=db)
    assert sorted(remaining) == ["r2-03", "r2-04"]


async def test_storm_mixes_classes_and_records_truth(tmp_path, db, monkeypatch):
    monkeypatch.setenv("CRUSOE_API_KEY", "test-key")
    monkeypatch.setattr(
        faults, "STALE_ENDPOINTS", tuple(u for u in faults.STALE_ENDPOINTS if ".invalid" in u)
    )
    upstream = FakeUpstream().start()
    try:
        sup = make_supervisor(tmp_path, 5, upstream.base_url)
        await sup.start_all()
        try:
            nodes = sup.node_ids()
            injected = await faults.storm(sup, nodes, seed=11, db=db)
            assert [f.node_id for f in injected] == nodes
            classes = [f.fault_class for f in injected]
            assert len(set(classes)) == 5 and set(classes) <= set(faults.TRAIN_CLASSES)
            assert all(faults.seed_kind(f.seed) == "train" for f in injected)
            rows = await db.active_faults()
            assert sorted(r["fault_class"] for r in rows) == sorted(classes)
            assert all(r["zone"] == "live" and r["injected_at"] for r in rows)
            assert await faults.storm(sup, [], seed=11, db=db) == []
        finally:
            for pid in list(sup.squatters()):
                await sup.kill_squatter(pid)
            await sup.stop_all()
    finally:
        upstream.stop()


def test_storm_is_deterministic_for_a_seed():
    a = faults.seeded_rng("storm", 3).sample(faults.TRAIN_CLASSES, 4)
    b = faults.seeded_rng("storm", 3).sample(faults.TRAIN_CLASSES, 4)
    c = faults.seeded_rng("storm", 4).sample(faults.TRAIN_CLASSES, 4)
    assert a == b and a != c


def test_nodectl_parser_accepts_documented_forms():
    parser = nodectl.build_parser()
    assert parser.parse_args(["inject", "r1-01", "leaky_worker", "--seed", "7"]).seed == 7
    assert parser.parse_args(["inject", "r1-01", "10"]).fault_class == "10"
    assert parser.parse_args(["storm", "1", "2"]).rows == [1, 2]
    assert parser.parse_args(["logs", "r1-01", "-n", "5"]).n == 5
    assert parser.parse_args(["reset", "all"]).node == "all"
    assert parser.parse_args(["up"]).nodes == []
