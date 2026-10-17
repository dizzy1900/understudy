import { on } from "../bus";
import { CAPTION_TTL_MS, useStream } from "../store";
import { useVoice } from "./voiceStore";

export type SpeechPriority = "low" | "high";

export interface SpeakOptions {
  priority?: SpeechPriority;
  /** Show a caption while the line is spoken. Defaults to true. */
  caption?: boolean;
  /** Speak even if the same line was spoken a moment ago. */
  fresh?: boolean;
  /** Cut off whatever is being said and go first, and never be dropped to make room. For lines the user asked for. */
  interrupt?: boolean;
}

/** Reading time for a line, used when speech is muted or unavailable so callers can still pace themselves. */
export function estimateMs(text: string): number {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1200, Math.round((words / 2.6) * 1000));
}

export interface Engine {
  available(): boolean;
  /** Speaks one line. Each callback is called at most once. */
  say(
    text: string,
    callbacks: { onStart: () => void; onEnd: () => void; onError: (kind: "blocked" | "failed") => void },
  ): void;
  cancel(): void;
}

export interface QueueHooks {
  caption(text: string, priority: SpeechPriority, ttlMs: number): void;
  muted(): boolean;
  now(): number;
  blockedChanged?(blocked: boolean): void;
}

interface Item {
  text: string;
  priority: SpeechPriority;
  caption: boolean;
  queuedAt: number;
  startedAt: number;
  captioned: boolean;
  keep: boolean;
  promise: Promise<void>;
  resolve: () => void;
  startTimer: ReturnType<typeof setTimeout> | null;
  endTimer: ReturnType<typeof setTimeout> | null;
}

type Timer = ReturnType<typeof setTimeout>;

const START_TIMEOUT_MS = 3000;
const GAP_MS = 350;
const INTERRUPT_GAP_MS = 120;
const DEDUPE_MS = 20_000;
const LOW_STALE_MS = 12_000;
const HIGH_STALE_MS = 25_000;
const MAX_QUEUED_LOW = 2;
const MAX_QUEUED_HIGH = 3;
/** After this many lines in a row that never started, the engine is treated as absent until the user interacts. */
const MAX_STALLS = 2;

/** Speaks one line at a time. A high line cuts off a low one and goes ahead of the low lines still waiting.
 * When there is no sound (muted, blocked, no engine) the caption is shown at once and the promise resolves after
 * the reading time, so callers pace the same either way. */
export class SpeechQueue {
  private queue: Item[] = [];
  private current: Item | null = null;
  private recent = new Map<string, number>();
  private blocked = false;
  private stalls = 0;
  private pumpTimer: Timer | null = null;

  constructor(
    private engine: Engine,
    private hooks: QueueHooks,
  ) {}

  speak(text: string, options: SpeakOptions = {}): Promise<void> {
    const line = text.trim();
    if (!line) return Promise.resolve();
    const priority = options.priority ?? "low";
    const caption = options.caption ?? true;
    const now = this.hooks.now();

    const existing = this.current?.text === line ? this.current : this.queue.find((i) => i.text === line);
    if (existing) {
      if (priority === "high") this.raise(existing, options.interrupt === true);
      if (!caption) existing.caption = false;
      return existing.promise;
    }
    const last = this.recent.get(line);
    if (!options.fresh && last !== undefined && now - last < DEDUPE_MS) return Promise.resolve();

    let resolve = () => {};
    const promise = new Promise<void>((done) => (resolve = done));
    const item: Item = {
      text: line,
      priority,
      caption,
      queuedAt: now,
      startedAt: 0,
      captioned: false,
      keep: options.interrupt === true,
      promise,
      resolve,
      startTimer: null,
      endTimer: null,
    };

    if (options.interrupt) {
      this.queue.unshift(item);
      if (this.current) this.interrupt(this.current);
    } else if (priority === "high") {
      const firstLow = this.queue.findIndex((i) => i.priority === "low");
      this.queue.splice(firstLow === -1 ? this.queue.length : firstLow, 0, item);
      const waiting = this.queue.filter((i) => i.priority === "high" && !i.keep);
      if (waiting.length > MAX_QUEUED_HIGH) this.drop(waiting[0]);
      if (this.current?.priority === "low") this.interrupt(this.current);
    } else {
      this.queue.push(item);
      const lows = this.queue.filter((i) => i.priority === "low");
      if (lows.length > MAX_QUEUED_LOW) this.drop(lows[0]);
    }
    this.pump();
    return promise;
  }

  cancelAll(): void {
    for (const item of this.queue.splice(0)) item.resolve();
    if (this.current) {
      const item = this.current;
      this.clearTimers(item);
      this.current = null;
      this.engine.cancel();
      item.resolve();
    }
    this.clearPump();
  }

  /** Called when the user mutes. Whatever is being said stops, and waiting low lines are dropped. */
  muteChanged(): void {
    if (!this.hooks.muted()) return;
    for (const item of this.queue.filter((i) => i.priority === "low")) this.drop(item);
    const item = this.current;
    if (!item) return;
    this.clearTimers(item);
    this.current = null;
    this.engine.cancel();
    const left = item.startedAt ? Math.max(0, estimateMs(item.text) - (this.hooks.now() - item.startedAt)) : estimateMs(item.text);
    setTimeout(item.resolve, left);
    this.schedulePump(INTERRUPT_GAP_MS);
  }

  /** The browser only lets a page speak after the user has interacted with it. */
  unblock(): void {
    this.stalls = 0;
    if (!this.blocked) return;
    this.blocked = false;
    this.hooks.blockedChanged?.(false);
    this.pump();
  }

  isBlocked(): boolean {
    return this.blocked;
  }

  private raise(item: Item, front: boolean): void {
    const wasLow = item.priority === "low";
    item.priority = "high";
    if (front) item.keep = true;
    if (item === this.current) return;
    const at = this.queue.indexOf(item);
    if (at === -1 || (!front && !wasLow)) return;
    this.queue.splice(at, 1);
    if (front) {
      this.queue.unshift(item);
      if (this.current) this.interrupt(this.current);
      return;
    }
    const firstLow = this.queue.findIndex((i) => i.priority === "low");
    this.queue.splice(firstLow === -1 ? this.queue.length : firstLow, 0, item);
  }

  private silent(): boolean {
    return this.hooks.muted() || !this.engine.available() || this.blocked || this.stalls >= MAX_STALLS;
  }

  private pump(): void {
    if (this.current || this.pumpTimer) return;
    const now = this.hooks.now();
    for (const item of [...this.queue]) {
      const limit = item.priority === "high" ? HIGH_STALE_MS : LOW_STALE_MS;
      if (now - item.queuedAt > limit) this.drop(item);
    }
    for (;;) {
      const item = this.queue.shift();
      if (!item) return;
      if (this.silent()) {
        this.silently(item);
        continue;
      }
      this.begin(item);
      return;
    }
  }

  private begin(item: Item): void {
    this.current = item;
    item.startTimer = setTimeout(() => {
      if (this.current !== item || item.startedAt) return;
      this.stalls += 1;
      this.abandon(item);
    }, START_TIMEOUT_MS);

    this.engine.say(item.text, {
      onStart: () => {
        if (this.current !== item || item.startedAt) return;
        item.startedAt = this.hooks.now();
        this.stalls = 0;
        if (item.startTimer) clearTimeout(item.startTimer);
        item.startTimer = null;
        this.showCaption(item);
        item.endTimer = setTimeout(() => this.finish(item), estimateMs(item.text) * 2 + 3000);
      },
      onEnd: () => {
        if (this.current === item) this.finish(item);
      },
      onError: (kind) => {
        if (this.current !== item) return;
        if (kind === "blocked") {
          this.blocked = true;
          this.hooks.blockedChanged?.(true);
        }
        if (item.startedAt) this.finish(item);
        else this.abandon(item);
      },
    });
  }

  private finish(item: Item): void {
    this.clearTimers(item);
    if (this.current !== item) return;
    this.current = null;
    this.recent.set(item.text, this.hooks.now());
    item.resolve();
    this.schedulePump(GAP_MS);
  }

  /** The engine did not produce sound for this line. Show it and carry on. */
  private abandon(item: Item): void {
    this.clearTimers(item);
    if (this.current === item) this.current = null;
    this.engine.cancel();
    this.silently(item);
    this.schedulePump(INTERRUPT_GAP_MS);
  }

  private silently(item: Item): void {
    this.recent.set(item.text, this.hooks.now());
    this.showCaption(item);
    setTimeout(item.resolve, estimateMs(item.text));
  }

  private interrupt(item: Item): void {
    this.clearTimers(item);
    this.current = null;
    this.engine.cancel();
    item.resolve();
    this.schedulePump(INTERRUPT_GAP_MS);
  }

  private drop(item: Item): void {
    const at = this.queue.indexOf(item);
    if (at !== -1) this.queue.splice(at, 1);
    item.resolve();
  }

  private showCaption(item: Item): void {
    if (!item.caption || item.captioned) return;
    item.captioned = true;
    this.hooks.caption(item.text, item.priority, Math.max(CAPTION_TTL_MS, estimateMs(item.text) + 1000));
  }

  private clearTimers(item: Item): void {
    if (item.startTimer) clearTimeout(item.startTimer);
    if (item.endTimer) clearTimeout(item.endTimer);
    item.startTimer = null;
    item.endTimer = null;
  }

  private schedulePump(ms: number): void {
    this.clearPump();
    this.pumpTimer = setTimeout(() => {
      this.pumpTimer = null;
      this.pump();
    }, ms);
  }

  private clearPump(): void {
    if (this.pumpTimer) clearTimeout(this.pumpTimer);
    this.pumpTimer = null;
  }
}

const PREFERRED_VOICES = ["Daniel", "Samantha", "Google UK English Male"];
export const SPEECH_RATE = 1.05;

export function pickVoice(voices: readonly { name: string; lang: string }[]): number {
  for (const wanted of PREFERRED_VOICES) {
    const at = voices.findIndex((v) => v.name === wanted || v.name.startsWith(`${wanted} `));
    if (at !== -1) return at;
  }
  for (const lang of ["en-GB", "en-US"]) {
    const at = voices.findIndex((v) => v.lang.replace("_", "-") === lang);
    if (at !== -1) return at;
  }
  return voices.findIndex((v) => v.lang.toLowerCase().startsWith("en"));
}

function browserEngine(): Engine {
  const synth = typeof speechSynthesis === "undefined" ? null : speechSynthesis;
  // Chrome can collect an utterance that nothing references before its events fire.
  let active: SpeechSynthesisUtterance | null = null;
  let voicesReady = false;
  let turn = 0;

  const speakNow = (target: SpeechSynthesis, text: string, callbacks: Parameters<Engine["say"]>[1]) => {
    const utterance = new SpeechSynthesisUtterance(text);
    const voices = target.getVoices();
    const at = pickVoice(voices);
    if (at !== -1) {
      utterance.voice = voices[at];
      utterance.lang = voices[at].lang;
    }
    utterance.rate = SPEECH_RATE;
    utterance.onstart = () => callbacks.onStart();
    utterance.onend = () => callbacks.onEnd();
    utterance.onerror = (event) => {
      if (event.error === "interrupted" || event.error === "canceled") callbacks.onEnd();
      else callbacks.onError(event.error === "not-allowed" ? "blocked" : "failed");
    };
    active = utterance;
    target.speak(utterance);
  };

  return {
    available: () => synth !== null && typeof SpeechSynthesisUtterance !== "undefined",
    say(text, callbacks) {
      if (!synth) {
        callbacks.onError("failed");
        return;
      }
      if (voicesReady || synth.getVoices().length > 0) {
        voicesReady = true;
        speakNow(synth, text, callbacks);
        return;
      }
      // The first line can arrive before the voice list has loaded and would be spoken in the default voice.
      const mine = turn;
      let timer = 0;
      const go = () => {
        synth.removeEventListener("voiceschanged", go);
        window.clearTimeout(timer);
        voicesReady = true;
        if (mine === turn) speakNow(synth, text, callbacks);
      };
      synth.addEventListener("voiceschanged", go);
      timer = window.setTimeout(go, 700);
    },
    cancel() {
      turn += 1;
      active = null;
      synth?.cancel();
    },
  };
}

let queue: SpeechQueue | null = null;

function shared(): SpeechQueue {
  queue ??= new SpeechQueue(browserEngine(), {
    caption: (text, priority, ttl) => useStream.getState().pushCaption(text, priority, ttl),
    muted: () => useStream.getState().muted,
    now: Date.now,
    blockedChanged: (blocked) => useVoice.setState({ blocked }),
  });
  return queue;
}

/** Resolves when the line has been spoken, or when its reading time has passed if there is no sound. */
export function speak(text: string, options: SpeakOptions = {}): Promise<void> {
  return shared().speak(text, options);
}

export function cancelSpeech(): void {
  shared().cancelAll();
}

/** Speaks the backend's narration and captions it in step with the voice. Call once at startup. */
export function installSpeech(): () => void {
  const speech = shared();
  const stops: Array<() => void> = [];

  stops.push(
    on("narration", (event) => {
      const { text, priority } = event.data;
      // The briefing card shows this line and the briefing flow speaks it.
      if (useStream.getState().briefing?.trim() === text.trim()) return;
      void speech.speak(text, { priority });
    }),
  );

  let muted = useStream.getState().muted;
  stops.push(
    useStream.subscribe((state) => {
      if (state.muted === muted) return;
      muted = state.muted;
      speech.muteChanged();
    }),
  );

  const unlock = () => speech.unblock();
  window.addEventListener("pointerdown", unlock, true);
  window.addEventListener("keydown", unlock, true);
  stops.push(() => {
    window.removeEventListener("pointerdown", unlock, true);
    window.removeEventListener("keydown", unlock, true);
  });

  // Voices load after the first call on some browsers; asking once now warms the list.
  if (typeof speechSynthesis !== "undefined") speechSynthesis.getVoices();

  return () => {
    for (const stop of stops) stop();
    speech.cancelAll();
  };
}
