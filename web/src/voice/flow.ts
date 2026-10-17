import { api } from "../api";
import { startSweep, cancelSweep } from "../scene/sweep";
import { useStream } from "../store";
import { say, shortError, useUi } from "../ui/actions";
import { useReplay } from "../ui/replayStore";
import { abortListening, loadVoiceStatus, onTranscript, startListening, stopListening } from "./listen";
import { cancelSpeech, estimateMs, speak } from "./speech";
import { useVoice } from "./voiceStore";

export const BACK_PATTERN = /i'?m back|what happened|welcome back/i;

/** "422 no fault matches that" reads better to the operator as "no fault matches that". */
export function plainError(error: unknown): string {
  return shortError(error).replace(/^\d{3}\s+/, "");
}

let run = 0;

export function cancelBack(): void {
  run += 1;
  cancelSpeech();
  cancelSweep();
  useUi.setState({ backPhase: "idle" });
}

/** The operator is back: fetch the briefing, read it aloud while the camera crosses the hall, then replay the
 * ladder. Calling it again while it runs cancels it. */
export async function imBack(): Promise<void> {
  const phase = useUi.getState().backPhase;
  if (phase === "briefing" || phase === "replay") {
    cancelBack();
    return;
  }
  if (phase === "fetching") return;

  const mine = ++run;
  useUi.setState({ backPhase: "fetching" });
  try {
    const payload = await api.back();
    if (mine !== run) return;
    const text = typeof payload?.text === "string" ? payload.text.trim() : "";
    if (!text) {
      say("Nothing to report since you left.", "low");
      return;
    }

    useStream.getState().setBriefing(text);
    useUi.setState({ backPhase: "briefing" });
    await Promise.all([speak(text, { priority: "high", caption: false, fresh: true, interrupt: true }), startSweep(estimateMs(text))]);
    if (mine !== run) return;

    useUi.setState({ backPhase: "replay" });
    await useReplay.getState().start();
  } catch (error) {
    if (mine === run) say(`Briefing unavailable: ${plainError(error)}`);
  } finally {
    if (mine === run) useUi.setState({ backPhase: "idle" });
  }
}

async function chaos(command: string): Promise<void> {
  try {
    const result = await api.chaos(command);
    const text = typeof result?.text === "string" ? result.text.trim() : "";
    if (text) void speak(text, { priority: "high" });
  } catch (error) {
    say(plainError(error));
  }
}

/** Routes a spoken or typed command: a request for the briefing, or a fault to inject. */
export async function runCommand(text: string): Promise<void> {
  const command = text.trim();
  if (!command) return;
  useVoice.setState({ heard: command, heardAt: Date.now() });
  if (BACK_PATTERN.test(command)) await imBack();
  else await chaos(command);
}

export function installVoice(): () => void {
  onTranscript((transcript) => void runCommand(transcript));

  window.understudyVoice = {
    start: startListening,
    stop: stopListening,
    command: runCommand,
    imBack,
    speak: (text, priority = "high") => speak(text, { priority, fresh: true }),
  };
  void loadVoiceStatus();

  return () => {
    abortListening();
    cancelBack();
    delete window.understudyVoice;
  };
}
