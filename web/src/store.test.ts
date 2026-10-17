import { beforeEach, describe, expect, it } from "vitest";

import fixture from "./__fixtures__/real-events.json";
import { resetStream, tokensServed, useStream } from "./store";
import type { StreamEvent } from "./types";

const events = fixture as unknown as StreamEvent[];

function feed(list: StreamEvent[]) {
  for (const event of list) useStream.getState().handle(event);
}

describe("real event stream", () => {
  beforeEach(resetStream);

  it("builds the fleet and ledger from the first events", () => {
    feed(events);
    const state = useStream.getState();
    expect(Object.keys(state.nodes)).toHaveLength(16);
    expect(state.nodes["pg-01"].zone).toBe("proving");
    expect(state.ledger?.incidents_total).toBeGreaterThan(0);
    expect(Object.keys(state.ledger!.ladder)).toHaveLength(10);
    expect(tokensServed(state)).toBeGreaterThanOrEqual(state.ledger!.tokens_served);
  });

  it("applies node_status transitions to known nodes", () => {
    feed(events);
    const statuses = events.filter((e) => e.type === "node_status");
    const last = statuses[statuses.length - 1];
    if (last.type === "node_status") {
      expect(useStream.getState().nodes[last.data.node_id].status).toBe(last.data.status);
    }
  });

  it("keeps the commander even though its spawn happened before the page opened", () => {
    feed(events);
    const commander = Object.values(useStream.getState().agents).find((a) => a.role === "commander");
    expect(commander).toBeTruthy();
    expect(commander!.thought.length).toBeGreaterThan(0);
  });

  it("tracks agents through spawn, thought, tool call and done", () => {
    feed(events);
    const state = useStream.getState();
    const verifier = Object.values(state.agents).find((a) => a.role === "verifier");
    expect(verifier?.rung).toBe("ultra");
    const responders = Object.values(state.agents).filter((a) => a.role === "responder");
    expect(responders.length).toBeGreaterThan(0);
    expect(responders.some((a) => a.tool !== null)).toBe(true);
    expect(Object.values(state.agents).some((a) => a.doneAt !== null)).toBe(true);
    // Rules-based agents carry the script rung and still have to be tracked.
    expect(Object.values(state.agents).some((a) => a.role === "watchdog" && a.rung === "script")).toBe(true);
    expect(Object.values(state.agents).some((a) => a.role === "promoter")).toBe(true);
  });

  it("turns a verified verification into a stamp with its quality", () => {
    feed(events);
    const verified = events.find((e) => e.type === "verification" && e.data.verdict === "verified");
    expect(verified).toBeTruthy();
    if (verified?.type !== "verification") return;
    const stamp = useStream.getState().stamps[verified.data.node_id];
    expect(stamp.verdict).toBe("verified");
    expect(stamp.quality).toBe(verified.data.quality);
    expect(stamp.failing).toBe("");
  });

  it("names the failing probe on a rejected verification", () => {
    const rejected = events.find((e) => e.type === "verification" && e.data.verdict === "rejected")!;
    useStream.getState().handle(rejected);
    if (rejected.type !== "verification") return;
    const stamp = useStream.getState().stamps[rejected.data.node_id];
    expect(stamp.verdict).toBe("rejected");
    expect(stamp.failing).toBe("burst 0/4");
    expect(stamp.failDetail).toBe("connection refused");
  });

  it("marks an incident's responder as escalated", () => {
    const escalation = events.find((e) => e.type === "escalation");
    expect(escalation).toBeTruthy();
    if (escalation?.type !== "escalation") return;
    feed([
      {
        type: "agent_spawned",
        ts: 1,
        data: {
          agent_id: "responder-x",
          role: "responder",
          model: "m",
          rung: "lightning",
          incident_id: escalation.data.incident_id,
          node_id: "r1-01",
        },
      },
      escalation,
    ]);
    expect(useStream.getState().agents["responder-x"].escalatedAt).not.toBeNull();
  });

  it("moves a class on ladder_update and keeps the evidence", () => {
    const update = events.find((e) => e.type === "ladder_update");
    expect(update).toBeTruthy();
    if (update?.type !== "ladder_update") return;
    useStream.getState().handle(update);
    const state = useStream.getState();
    expect(state.ladder[update.data.fault_class]).toBe(update.data.to_rung);
    expect(state.ladderMoves[0].passed).toBe(update.data.evidence.passed);
    expect(state.ladderMoves[0].total).toBe(update.data.evidence.total);
  });

  it("keeps narration and skill notes", () => {
    feed(events);
    const state = useStream.getState();
    expect(state.captions.length).toBeGreaterThan(0);
    expect(Object.keys(state.skillNotes).length).toBeGreaterThan(0);
  });

  it("ignores duplicates, so replaying the stream changes nothing", () => {
    feed(events);
    const before = JSON.stringify({ ...useStream.getState(), lastEventAt: 0 });
    feed(events);
    expect(JSON.stringify({ ...useStream.getState(), lastEventAt: 0 })).toBe(before);
  });

  it("hydrates working agents quietly and drops the finished ones", () => {
    useStream.getState().hydrate(events);
    const agents = Object.values(useStream.getState().agents);
    expect(agents.every((a) => a.doneAt === null)).toBe(true);
    expect(useStream.getState().captions).toHaveLength(0);
    expect(useStream.getState().stamps).toEqual({});
  });
});
