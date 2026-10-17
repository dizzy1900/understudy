import pytest
from pydantic import ValidationError

from understudy import events as ev

CONTRACT_TYPES = {
    "fleet_snapshot",
    "node_status",
    "tokens",
    "incident_opened",
    "agent_spawned",
    "agent_thought",
    "tool_call",
    "fix_claimed",
    "verification",
    "escalation",
    "incident_closed",
    "skill_updated",
    "ladder_update",
    "narration",
    "ledger",
    "agent_done",
}


def test_every_contract_type_is_registered():
    assert set(ev.EVENT_TYPES) == CONTRACT_TYPES
    for name, cls in ev.EVENT_TYPES.items():
        assert cls.event_type == name


def test_envelope_round_trip_drops_unset_optionals():
    envelope = ev.Envelope.of(ev.NodeStatus(node_id="r1-01", status="down"), ts=12.5)
    assert envelope.model_dump() == {
        "type": "node_status",
        "ts": 12.5,
        "data": {"node_id": "r1-01", "status": "down"},
    }
    again = ev.Envelope.model_validate_json(envelope.to_json())
    assert again.payload() == ev.NodeStatus(node_id="r1-01", status="down")


def test_thought_is_clipped_to_contract_length():
    thought = ev.AgentThought(agent_id="a", text="x" * 500)
    assert len(thought.text) == ev.THOUGHT_MAX
    assert thought.text.endswith("...")


def test_contract_literals_are_enforced():
    with pytest.raises(ValidationError):
        ev.NodeStatus(node_id="r1-01", status="asleep")
    with pytest.raises(ValidationError):
        ev.IncidentOpened(incident_id=1, node_id="r1-01", symptom="x", severity="medium")
    with pytest.raises(ValidationError):
        ev.Verification(incident_id=1, node_id="r1-01", verdict="verified", probes=[], quality=6)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        ev.Tokens(node_id="r1-01", tokens=3, extra=1)


def test_ledger_shape():
    ledger = ev.Ledger(
        since=0,
        incidents_total=1,
        verified=1,
        rejected_claims=0,
        promotions=0,
        demotions=0,
        cost_total_usd=0.01,
        cost_per_incident_usd=0.01,
        tokens_served=10,
        cache_hit_rate=0.5,
        by_rung={"super": ev.RungStat(incidents=1, cost_usd=0.01), "lightning": ev.RungStat(), "script": ev.RungStat()},
        ladder={"model_id_case": "super"},
    )
    data = ev.Envelope.of(ledger).data
    assert set(data["by_rung"]) == {"super", "lightning", "script"}
    assert data["by_rung"]["super"] == {"incidents": 1, "cost_usd": 0.01}


@pytest.mark.anyio
async def test_bus_fans_out_to_every_subscriber():
    bus = ev.EventBus()
    with bus.subscribe() as one, bus.subscribe() as two:
        bus.publish(ev.Tokens(node_id="r1-01", tokens=5))
        first, second = await one.get(), await two.get()
    assert first.type == second.type == "tokens"
    assert first.data == {"node_id": "r1-01", "tokens": 5}


@pytest.mark.anyio
async def test_slow_subscriber_drops_oldest():
    bus = ev.EventBus()
    with bus.subscribe(maxsize=2) as sub:
        for n in range(5):
            bus.publish(ev.Tokens(node_id="r1-01", tokens=n))
        assert sub.dropped == 3
        assert [(await sub.get()).data["tokens"] for _ in range(2)] == [3, 4]


def test_closing_a_subscription_unregisters_it():
    bus = ev.EventBus()
    with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0
    bus.publish(ev.Tokens(node_id="r1-01", tokens=1))
