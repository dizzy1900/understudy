import { beforeEach, describe, expect, it } from "vitest";

import fixture from "./__fixtures__/real-events.json";
import { CAPTION_TTL_MS, DOWN_HOLD_MS, resetStream, tokensServed, useStream } from "./store";
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
    useStream.getState().sweep(Date.now() + DOWN_HOLD_MS + 1);
    const statuses = events.filter((e) => e.type === "node_status");
    const last = statuses[statuses.length - 1];
    if (last.type === "node_status") {
      expect(useStream.getState().nodes[last.data.node_id].status).toBe(last.data.status);
    }
  });

  it("keeps a down node red for a moment when repairing follows in the same instant", () => {
    feed(events);
    const ts = events[events.length - 1].ts + 1;
    const node = "r1-01";
    const send = (status: string) =>
      useStream.getState().handle({ type: "node_status", ts: ts + Math.random(), data: { node_id: node, status } } as StreamEvent);
    send("down");
    send("repairing");
    expect(useStream.getState().nodes[node].status).toBe("down");

    useStream.getState().sweep(Date.now() + 1000);
    expect(useStream.getState().nodes[node].status).toBe("down");

    useStream.getState().sweep(Date.now() + DOWN_HOLD_MS + 1);
    expect(useStream.getState().nodes[node].status).toBe("repairing");
    expect(useStream.getState().nodeHolds[node]).toBeUndefined();
  });

  it("shows the latest status once the hold ends, not the first one that arrived", () => {
    feed(events);
    const ts = events[events.length - 1].ts + 1;
    const node = "r2-02";
    const send = (status: string, n: number) =>
      useStream.getState().handle({ type: "node_status", ts: ts + n, data: { node_id: node, status } } as StreamEvent);
    send("down", 0);
    send("repairing", 1);
    send("healthy", 2);
    useStream.getState().sweep(Date.now() + DOWN_HOLD_MS + 1);
    expect(useStream.getState().nodes[node].status).toBe("healthy");
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

  it("keeps skill notes and leaves narration captions to the speech queue", () => {
    feed(events);
    const state = useStream.getState();
    expect(events.some((e) => e.type === "narration")).toBe(true);
    expect(state.captions).toHaveLength(0);
    expect(Object.keys(state.skillNotes).length).toBeGreaterThan(0);
  });

  it("lets a caption set its own lifetime", () => {
    useStream.getState().pushCaption("A long line.", "high", 15_000);
    useStream.getState().pushCaption("A short line.");
    const [long, short] = useStream.getState().captions;
    expect([long.ttl, short.ttl]).toEqual([15_000, CAPTION_TTL_MS]);
    useStream.getState().sweep(Date.now() + CAPTION_TTL_MS + 2000);
    expect(useStream.getState().captions.map((c) => c.text)).toEqual(["A long line."]);
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

  it("removes the caption of a line the briefing card now shows", () => {
    const text = "Welcome back. 3 incidents opened.";
    useStream.getState().pushCaption(text, "high");
    useStream.getState().pushCaption("Another line.", "low");
    useStream.getState().setBriefing(text);
    expect(useStream.getState().captions.map((c) => c.text)).toEqual(["Another line."]);
  });
});
