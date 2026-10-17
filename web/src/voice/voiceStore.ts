import { create } from "zustand";

export type ListenState = "idle" | "listening" | "transcribing";

interface VoiceState {
  /** The browser refused to speak because the page has not been interacted with yet. */
  blocked: boolean;
  listen: ListenState;
  /** What the last spoken or typed command was taken to be. */
  heard: string;
  heardAt: number;
}

export const useVoice = create<VoiceState>(() => ({
  blocked: false,
  listen: "idle",
  heard: "",
  heardAt: 0,
}));

/** Microphone level, 0 to 1, written by the recorder and read by the listening ring on its own frame loop. */
export const micLevel = { value: 0 };
