export {};

declare global {
  interface UnderstudyVoice {
    start(): void | Promise<void>;
    stop?(): void | Promise<void>;
  }

  interface Window {
    understudyVoice?: UnderstudyVoice;
  }
}
