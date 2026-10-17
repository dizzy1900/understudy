import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { estimateMs, pickVoice, SpeechQueue, type Engine, type QueueHooks } from "./speech";

type Callbacks = Parameters<Engine["say"]>[1];

class FakeEngine implements Engine {
  present = true;
  said: string[] = [];
  cancels = 0;
  callbacks: Callbacks | null = null;
  /** When set, say() starts at once, as a working voice does. */
  autoStart = true;

  available() {
    return this.present;
  }
  say(text: string, callbacks: Callbacks) {
    this.said.push(text);
    this.callbacks = callbacks;
    if (this.autoStart) callbacks.onStart();
  }
  cancel() {
    this.cancels += 1;
  }
}

function setup() {
  const engine = new FakeEngine();
  const captions: Array<{ text: string; priority: string; ttl: number }> = [];
  const state = { muted: false, blocked: false };
  const hooks: QueueHooks = {
    caption: (text, priority, ttl) => captions.push({ text, priority, ttl }),
    muted: () => state.muted,
    now: () => Date.now(),
    blockedChanged: (blocked) => (state.blocked = blocked),
  };
  return { engine, captions, state, queue: new SpeechQueue(engine, hooks) };
}

const words = (n: number) => Array.from({ length: n }, (_, i) => `word${i}`).join(" ");

describe("speech queue", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("captions a line when it starts and speaks lines one at a time", async () => {
    const { engine, captions, queue } = setup();
    const first = queue.speak("Row two is down.", { priority: "low" });
    queue.speak("Row three is down.", { priority: "low" });
    await vi.advanceTimersByTimeAsync(10);
    expect(engine.said).toEqual(["Row two is down."]);
    expect(captions.map((c) => c.text)).toEqual(["Row two is down."]);

    engine.callbacks!.onEnd();
    await first;
    await vi.advanceTimersByTimeAsync(500);
    expect(engine.said).toEqual(["Row two is down.", "Row three is down."]);
    expect(captions).toHaveLength(2);
  });

  it("lets a high line cut off a low one and go first", async () => {
    const { engine, queue } = setup();
    const low = queue.speak("Quiet remark.", { priority: "low" });
    queue.speak("Another quiet remark.", { priority: "low" });
    await vi.advanceTimersByTimeAsync(10);
    queue.speak("Promoted to lightning.", { priority: "high" });
    await low;
    expect(engine.cancels).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(300);
    expect(engine.said[1]).toBe("Promoted to lightning.");
  });

  it("does not cut off another high line", async () => {
    const { engine, queue } = setup();
    queue.speak("First alert.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    queue.speak("Second alert.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(300);
    expect(engine.said).toEqual(["First alert."]);
  });

  it("keeps only a couple of low lines waiting", async () => {
    const { engine, queue } = setup();
    queue.speak("Now speaking.", { priority: "low" });
    await vi.advanceTimersByTimeAsync(10);
    for (const n of [1, 2, 3, 4]) queue.speak(`Waiting ${n}.`, { priority: "low" });
    for (let i = 0; i < 3; i++) {
      engine.callbacks!.onEnd();
      await vi.advanceTimersByTimeAsync(400);
    }
    expect(engine.said).toEqual(["Now speaking.", "Waiting 3.", "Waiting 4."]);
  });

  it("drops a low line that waited too long", async () => {
    const { engine, queue } = setup();
    queue.speak(words(70), { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    queue.speak("Stale.", { priority: "low" });
    await vi.advanceTimersByTimeAsync(20_000);
    engine.callbacks!.onEnd();
    await vi.advanceTimersByTimeAsync(500);
    expect(engine.said).toEqual([words(70)]);
  });

  it("joins a repeat of a line that is already queued and resolves both together", async () => {
    const { engine, captions, queue } = setup();
    engine.autoStart = false;
    const a = queue.speak("Welcome back. Three incidents.", { priority: "low" });
    const b = queue.speak("Welcome back. Three incidents.", { priority: "high", caption: false });
    expect(b).toBe(a);
    await vi.advanceTimersByTimeAsync(10);
    engine.callbacks!.onStart();
    expect(engine.said).toHaveLength(1);
    expect(captions).toHaveLength(0);
    engine.callbacks!.onEnd();
    await a;
  });

  it("skips a line that was just spoken unless asked for again", async () => {
    const { engine, queue } = setup();
    const first = queue.speak("Storm cleared.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    engine.callbacks!.onEnd();
    await first;
    await vi.advanceTimersByTimeAsync(500);
    await queue.speak("Storm cleared.", { priority: "high" });
    expect(engine.said).toHaveLength(1);
    queue.speak("Storm cleared.", { priority: "high", fresh: true });
    await vi.advanceTimersByTimeAsync(10);
    expect(engine.said).toHaveLength(2);
  });

  it("shows the caption at once and paces by reading time when muted", async () => {
    const { engine, captions, state, queue } = setup();
    state.muted = true;
    const text = words(13);
    let done = false;
    void queue.speak(text, { priority: "high" }).then(() => (done = true));
    expect(captions).toHaveLength(1);
    expect(engine.said).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(estimateMs(text) - 50);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(100);
    expect(done).toBe(true);
  });

  it("stops the voice when muted mid-line and drops waiting low lines", async () => {
    const { engine, state, queue } = setup();
    queue.speak("Speaking now.", { priority: "high" });
    queue.speak("Waiting low.", { priority: "low" });
    await vi.advanceTimersByTimeAsync(10);
    state.muted = true;
    queue.muteChanged();
    expect(engine.cancels).toBe(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(engine.said).toEqual(["Speaking now."]);
  });

  it("falls back to captions when the browser refuses to speak, and recovers after a click", async () => {
    const { engine, captions, state, queue } = setup();
    engine.autoStart = false;
    queue.speak("First line.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    engine.callbacks!.onError("blocked");
    expect(state.blocked).toBe(true);
    expect(captions.map((c) => c.text)).toEqual(["First line."]);

    queue.speak("Second line.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(500);
    expect(engine.said).toEqual(["First line."]);
    expect(captions).toHaveLength(2);

    queue.unblock();
    expect(state.blocked).toBe(false);
    queue.speak("Third line.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(500);
    expect(engine.said).toEqual(["First line.", "Third line."]);
  });

  it("gives up on a voice that never starts", async () => {
    const { engine, captions, queue } = setup();
    engine.autoStart = false;
    const done = queue.speak("Nothing happens.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(3100);
    expect(captions.map((c) => c.text)).toEqual(["Nothing happens."]);
    await vi.advanceTimersByTimeAsync(estimateMs("Nothing happens."));
    await done;
  });

  it("resolves everything on cancel", async () => {
    const { engine, queue } = setup();
    const a = queue.speak("One.", { priority: "high" });
    const b = queue.speak("Two.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    queue.cancelAll();
    await Promise.all([a, b]);
    expect(engine.cancels).toBe(1);
  });

  it("lets a requested line cut off a high line and jump the queue", async () => {
    const { engine, queue } = setup();
    const cut = queue.speak("Storm on row two.", { priority: "high" });
    queue.speak("Waiting alert.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    queue.speak("Welcome back. Briefing.", { priority: "high", interrupt: true });
    await cut;
    await vi.advanceTimersByTimeAsync(300);
    expect(engine.said).toEqual(["Storm on row two.", "Welcome back. Briefing."]);
  });

  it("moves a queued line to the front when the same text is requested", async () => {
    const { engine, queue } = setup();
    queue.speak(words(40), { priority: "high" });
    queue.speak("Welcome back. Briefing.", { priority: "high" });
    queue.speak("Another alert.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    queue.speak("Welcome back. Briefing.", { priority: "high", interrupt: true });
    await vi.advanceTimersByTimeAsync(300);
    expect(engine.said).toEqual([words(40), "Welcome back. Briefing."]);
  });

  it("keeps only the newest few high lines waiting", async () => {
    const { engine, queue } = setup();
    queue.speak("Speaking.", { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    for (const n of [1, 2, 3, 4, 5]) queue.speak(`Alert ${n}.`, { priority: "high" });
    for (let i = 0; i < 4; i++) {
      engine.callbacks!.onEnd();
      await vi.advanceTimersByTimeAsync(400);
    }
    expect(engine.said).toEqual(["Speaking.", "Alert 3.", "Alert 4.", "Alert 5."]);
  });

  it("keeps the caption up as long as a long line is spoken", async () => {
    const { captions, queue } = setup();
    queue.speak(words(70), { priority: "high" });
    await vi.advanceTimersByTimeAsync(10);
    expect(captions[0].ttl).toBeGreaterThan(estimateMs(words(70)));
  });
});

describe("voice choice", () => {
  const voice = (name: string, lang = "en-US") => ({ name, lang });

  it("prefers Daniel, then Samantha, then Google UK English Male", () => {
    expect(pickVoice([voice("Samantha"), voice("Daniel (English (United Kingdom))", "en-GB")])).toBe(1);
    expect(pickVoice([voice("Karen", "en-AU"), voice("Samantha")])).toBe(1);
    expect(pickVoice([voice("Google US English"), voice("Google UK English Male", "en-GB")])).toBe(1);
  });

  it("falls back to British, American, then any English voice", () => {
    expect(pickVoice([voice("Amelie", "fr-FR"), voice("Fiona", "en-US"), voice("Kate", "en-GB")])).toBe(2);
    expect(pickVoice([voice("Amelie", "fr-FR"), voice("Tessa", "en-ZA")])).toBe(1);
    expect(pickVoice([voice("Amelie", "fr-FR")])).toBe(-1);
  });
});
