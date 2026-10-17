import { useEffect, useState } from "react";

import BreakMenu from "./BreakMenu";
import { imBack } from "../voice/flow";
import { useVoice } from "../voice/voiceStore";
import { sendStorm, toggleReplay, useUi, voiceStart, voiceStop } from "./actions";
import { useReplay } from "./replayStore";
import { CommandBox, HeardChip } from "./VoicePanel";

function MicGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
      <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" />
      <path
        d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

export default function Controls() {
  const replayActive = useReplay((s) => s.active);
  const replayLoading = useReplay((s) => s.loading);
  const stormSentAt = useUi((s) => s.stormSentAt);
  const stormBusy = useUi((s) => s.stormBusy);
  const backPhase = useUi((s) => s.backPhase);
  const talking = useUi((s) => s.talking);
  const voiceHint = useUi((s) => s.voiceHint);
  const listen = useVoice((s) => s.listen);
  const speechBlocked = useVoice((s) => s.blocked);
  const hint = voiceHint || (speechBlocked ? "click once to turn on spoken narration" : "");
  const [sent, setSent] = useState(false);

  useEffect(() => {
    if (!stormSentAt) return;
    setSent(true);
    const id = window.setTimeout(() => setSent(false), 1700);
    return () => window.clearTimeout(id);
  }, [stormSentAt]);

  return (
    <div className="controls">
      <HeardChip />
      <div className="voice-hint" data-show={hint || undefined} role="status">
        {hint}
      </div>
      <button
        type="button"
        className="btn btn-storm"
        data-sent={sent || undefined}
        disabled={stormBusy}
        onClick={() => void sendStorm()}
      >
        {sent && <span key={stormSentAt} className="storm-flash" />}
        <span>{sent ? "Storm sent" : "Storm \u2192"}</span>
        <kbd>S</kbd>
      </button>
      <div className="control-grid">
        <BreakMenu />
        <button
          type="button"
          className="btn btn-mic"
          data-on={talking || listen !== "idle" || undefined}
          aria-pressed={talking}
          onPointerDown={(event) => {
            event.preventDefault();
            event.currentTarget.setPointerCapture(event.pointerId);
            voiceStart();
          }}
          onPointerUp={voiceStop}
          onPointerCancel={voiceStop}
          onContextMenu={(event) => event.preventDefault()}
        >
          <MicGlyph />
          <span>{listen === "transcribing" ? "Sending" : talking || listen === "listening" ? "Listening" : "Talk"}</span>
          <kbd>Space</kbd>
        </button>
        <button
          type="button"
          className="btn"
          data-on={backPhase === "briefing" || backPhase === "replay" || undefined}
          disabled={backPhase === "fetching"}
          onClick={() => void imBack()}
        >
          <span>{backPhase === "fetching" ? "Briefing" : backPhase === "idle" ? "I'm back" : "Skip"}</span>
          <kbd>B</kbd>
        </button>
        <button type="button" className="btn" data-on={replayActive || undefined} onClick={toggleReplay}>
          <span>{replayActive ? "Stop" : replayLoading ? "Loading" : "Replay"}</span>
          <kbd>R</kbd>
        </button>
      </div>
      <CommandBox />
    </div>
  );
}
