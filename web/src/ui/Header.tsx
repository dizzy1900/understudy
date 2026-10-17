import { useStream } from "../store";
import { useIntro } from "./Intro";
import { toggleFullscreen } from "./actions";

function Mark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="rgba(255,255,255,0.04)" stroke="rgba(255,255,255,0.14)" />
      <circle cx="16" cy="16" r="7" fill="none" stroke="#3ee6ff" strokeWidth="2.6" />
      <circle cx="16" cy="16" r="2.2" fill="#3ee6ff" />
    </svg>
  );
}

const LABEL: Record<string, string> = {
  live: "Live",
  demo: "Demo",
  reconnecting: "Reconnecting",
  offline: "Offline",
};

export default function Header() {
  const connected = useStream((s) => s.connected);
  const everConnected = useStream((s) => s.everConnected);
  const demo = useStream((s) => (s as { demo?: boolean }).demo ?? false);
  const muted = useStream((s) => s.muted);
  const toggleMuted = useStream((s) => s.toggleMuted);
  const openIntro = useIntro((s) => s.setOpen);

  const state = connected ? "live" : demo ? "demo" : everConnected ? "reconnecting" : "offline";

  return (
    <div className="hdr">
      <div className="brand">
        <Mark />
        <h1 className="wordmark">UNDERSTUDY</h1>
        <span className="badge" title="Nemotron models served on Crusoe Managed Inference">
          <i />
          Nemotron on Crusoe
        </span>
      </div>
      <p className="subtitle">Autonomous on-call agents that keep an inference fleet healthy, and get cheaper every shift.</p>
      <div className="hdr-row">
        <span className="conn" data-state={state} title={demo && !connected ? "No live backend reachable; running a simulated shift in the browser" : undefined}>
          <i className="conn-dot" />
          {LABEL[state]}
        </span>
        {muted && <span className="tag tag-warn">muted</span>}
        <button type="button" className="chip interactive" onClick={toggleMuted} aria-pressed={muted}>
          <kbd>M</kbd>
          {muted ? "Unmute" : "Mute"}
        </button>
        <button type="button" className="chip interactive" onClick={toggleFullscreen}>
          <kbd>F</kbd>
          Fullscreen
        </button>
        <button type="button" className="chip interactive" onClick={() => openIntro(true)} aria-label="What am I looking at?">
          <kbd>?</kbd>
          Guide
        </button>
      </div>
    </div>
  );
}
