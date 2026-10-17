export {};

declare global {
  interface UnderstudyVoice {
    /** Starts listening, as holding the talk key does. */
    start(): void | Promise<void>;
    /** Stops listening and sends what was said. */
    stop?(): void | Promise<void>;
    /** Runs a command as if it had been spoken: the briefing request, or a fault to inject. */
    command?(text: string): void | Promise<void>;
    /** Runs the "I'm back" briefing, sweep and replay. */
    imBack?(): void | Promise<void>;
    /** Speaks a line through the narration queue. */
    speak?(text: string, priority?: "low" | "high"): void | Promise<void>;
  }

  interface Window {
    understudyVoice?: UnderstudyVoice;
    webkitAudioContext?: typeof AudioContext;
  }
}
