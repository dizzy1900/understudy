import { create } from "zustand";

import { api } from "../api";
import { useStream } from "../store";
import { useReplay } from "./replayStore";

interface UiState {
  stormSentAt: number;
  stormBusy: boolean;
  backBusy: boolean;
  talking: boolean;
  voiceHint: boolean;
}

export const useUi = create<UiState>(() => ({
  stormSentAt: 0,
  stormBusy: false,
  backBusy: false,
  talking: false,
  voiceHint: false,
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

export async function askBack(): Promise<void> {
  if (useUi.getState().backBusy) return;
  useUi.setState({ backBusy: true });
  try {
    const payload = await api.back();
    const text = typeof payload?.briefing === "string" ? payload.briefing.trim() : "";
    if (text) useStream.getState().setBriefing(text);
    else say("Nothing to report since you left.", "low");
  } catch (error) {
    say(`Briefing unavailable: ${shortError(error)}`);
  } finally {
    useUi.setState({ backBusy: false });
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

export function voiceStart(): void {
  if (useUi.getState().talking) return;
  const voice = window.understudyVoice;
  if (!voice) {
    useUi.setState({ voiceHint: true });
    window.clearTimeout(hintTimer);
    hintTimer = window.setTimeout(() => useUi.setState({ voiceHint: false }), 2600);
    return;
  }
  useUi.setState({ talking: true });
  try {
    void Promise.resolve(voice.start()).catch((error: unknown) => say(`Voice failed: ${shortError(error)}`));
  } catch (error) {
    say(`Voice failed: ${shortError(error)}`);
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
