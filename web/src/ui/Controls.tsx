import { useEffect, useState } from "react";

import BreakMenu from "./BreakMenu";
import { askBack, sendStorm, toggleReplay, useUi, voiceStart, voiceStop } from "./actions";
import { useReplay } from "./replayStore";

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
  const backBusy = useUi((s) => s.backBusy);
  const talking = useUi((s) => s.talking);
  const voiceHint = useUi((s) => s.voiceHint);
  const [sent, setSent] = useState(false);

  useEffect(() => {
    if (!stormSentAt) return;
    setSent(true);
    const id = window.setTimeout(() => setSent(false), 1700);
    return () => window.clearTimeout(id);
  }, [stormSentAt]);

  return (
    <div className="controls">
      <div className="voice-hint" data-show={voiceHint || undefined} role="status">
        voice not connected
      </div>
      <button
        type="button"
        className="btn btn-storm"
        data-sent={sent || undefined}
        disabled={stormBusy}
        onClick={() => void sendStorm()}
      >
        {sent && <span key={stormSentAt} className="storm-flash" />}
        <span>{sent ? "Storm sent" : "Storm"}</span>
        <kbd>S</kbd>
      </button>
      <div className="control-grid">
        <BreakMenu />
        <button
          type="button"
          className="btn btn-mic"
          data-on={talking || undefined}
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
          <span>{talking ? "Listening" : "Talk"}</span>
          <kbd>Space</kbd>
        </button>
        <button type="button" className="btn" disabled={backBusy} onClick={() => void askBack()}>
          <span>I'm back</span>
          <kbd>B</kbd>
        </button>
        <button type="button" className="btn" data-on={replayActive || undefined} onClick={toggleReplay}>
          <span>{replayActive ? "Stop" : replayLoading ? "Loading" : "Replay"}</span>
          <kbd>R</kbd>
        </button>
      </div>
    </div>
  );
}
