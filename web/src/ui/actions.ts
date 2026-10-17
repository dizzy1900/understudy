import { create } from "zustand";

import { api } from "../api";
import { useStream } from "../store";
import { useReplay } from "./replayStore";

interface UiState {
  stormSentAt: number;
  stormBusy: boolean;
  /** Where the "I'm back" flow is: waiting on the briefing, reading it with the camera sweep, or replaying. */
  backPhase: "idle" | "fetching" | "briefing" | "replay";
  talking: boolean;
  voiceHint: string;
}

export const useUi = create<UiState>(() => ({
  stormSentAt: 0,
  stormBusy: false,
  backPhase: "idle",
  talking: false,
  voiceHint: "",
}));

export function shortError(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  return text.length > 140 ? `${text.slice(0, 137)}...` : text;
}

export function say(text: string, priority: "low" | "high" = "high"): void {
  useStream.getState().pushCaption(text, priority);
}

function pickRows(): number[] {
  const rows = [1, 2, 3];
  const skip = Math.floor(Math.random() * rows.length);
  return rows.filter((_, i) => i !== skip);
}

export async function sendStorm(): Promise<void> {
  if (useUi.getState().stormBusy) return;
  useUi.setState({ stormBusy: true });
  try {
    await api.storm(pickRows());
    useUi.setState({ stormSentAt: Date.now() });
  } catch (error) {
    say(`Storm not sent: ${shortError(error)}`);
  } finally {
    useUi.setState({ stormBusy: false });
  }
}

export function toggleReplay(): void {
  const replay = useReplay.getState();
  if (replay.active || replay.loading) replay.stop();
  else void replay.start();
}

export function toggleFullscreen(): void {
  const doc = document;
  const task = doc.fullscreenElement ? doc.exitFullscreen() : doc.documentElement.requestFullscreen();
  task.catch((error: unknown) => say(`Fullscreen refused: ${shortError(error)}`, "low"));
}

let hintTimer = 0;

export function showHint(text: string): void {
  useUi.setState({ voiceHint: text });
  window.clearTimeout(hintTimer);
  hintTimer = window.setTimeout(() => useUi.setState({ voiceHint: "" }), 2600);
}

export function voiceStart(): void {
  if (useUi.getState().talking) return;
  const voice = window.understudyVoice;
  if (!voice) {
    showHint("voice not connected");
    return;
  }
  useUi.setState({ talking: true });
  const failed = (error: unknown) => {
    useUi.setState({ talking: false });
    say(`Voice failed: ${shortError(error)}`);
  };
  try {
    void Promise.resolve(voice.start()).catch(failed);
  } catch (error) {
    failed(error);
  }
}

export function voiceStop(): void {
  if (!useUi.getState().talking) return;
  useUi.setState({ talking: false });
  try {
    void Promise.resolve(window.understudyVoice?.stop?.()).catch((error: unknown) =>
      say(`Voice failed: ${shortError(error)}`),
    );
  } catch (error) {
    say(`Voice failed: ${shortError(error)}`);
  }
}
