import { beforeEach, describe, expect, it } from "vitest";

import { on } from "../bus";
import { resetStream, useStream } from "../store";
import type { StreamEvent } from "../types";
import { DemoSim } from "./sim";

function run(seconds: number, seed = 7): { sim: DemoSim; events: StreamEvent[] } {
  const events: StreamEvent[] = [];
  const sim = new DemoSim((event) => {
    events.push(event);
    useStream.getState().handle(event);
  }, seed);
  sim.start();
  for (let t = 0; t < seconds; t += 0.25) sim.advance(0.25);
  return { sim, events };
}

describe("demo simulator", () => {
  beforeEach(resetStream);

  it("tells the whole story within about 90 story seconds", () => {
    const verdicts: string[] = [];
    const off = on("verification", (e) => verdicts.push(e.data.verdict));
    const { events } = run(90);
    off();
    const state = useStream.getState();

    expect(Object.keys(state.nodes)).toHaveLength(16);
    expect(Object.keys(state.incidents).length).toBeGreaterThan(0);
    expect(state.ledger?.incidents_total).toBeGreaterThan(0);
    expect(verdicts).toContain("verified");
    expect(verdicts).toContain("rejected");
    expect(state.ledger?.rejected_claims).toBeGreaterThan(0);

    const promotions = events.filter((e) => e.type === "ladder_update" && e.data.direction === "promote");
    expect(promotions.length).toBeGreaterThan(0);
    expect(state.ladderMoves.some((m) => m.from_rung === "super" && m.to_rung === "lightning")).toBe(true);
    expect(Object.values(state.ladder)).toContain("lightning");
    expect(events.some((e) => e.type === "incident_closed")).toBe(true);
    expect(events.some((e) => e.type === "narration")).toBe(true);
  });

  it("emits events in time order with unique timestamps", () => {
    const { events } = run(40);
    for (let i = 1; i < events.length; i++) expect(events[i].ts).toBeGreaterThan(events[i - 1].ts);
  });

  it("drives cost per incident down as classes climb the ladder", () => {
    const { sim } = run(600);
    const ledger = sim.ledger();
    expect(ledger.promotions).toBeGreaterThan(2);
    expect(ledger.by_rung.lightning?.incidents ?? 0).toBeGreaterThan(0);
    const superMean = ledger.by_rung.super!.cost_usd / Math.max(1, ledger.by_rung.super!.incidents);
    const lightningMean = ledger.by_rung.lightning!.cost_usd / ledger.by_rung.lightning!.incidents;
    expect(lightningMean).toBeLessThan(superMean);
  });

  it("answers the operator controls", () => {
    const { sim } = run(5);
    sim.goAway();
    const injected = sim.storm([1, 2, 3]);
    expect(injected.length).toBeGreaterThan(0);
    for (let t = 0; t < 60; t += 0.5) sim.advance(0.5);
    const back = sim.comeBack();
    expect(back.briefing).toMatch(/While you were away: \d+ incidents opened/);
    expect(sim.history.some((e) => e.type === "ledger")).toBe(true);
  });
});
