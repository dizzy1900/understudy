import { useStream } from "../store";
import { toggleFullscreen } from "./actions";

export default function Header() {
  const connected = useStream((s) => s.connected);
  const everConnected = useStream((s) => s.everConnected);
  const muted = useStream((s) => s.muted);
  const toggleMuted = useStream((s) => s.toggleMuted);

  const state = connected ? "live" : everConnected ? "reconnecting" : "offline";

  return (
    <div className="hdr">
      <h1 className="wordmark">UNDERSTUDY</h1>
      <p className="subtitle">on-call agents for an inference fleet</p>
      <div className="hdr-row">
        <span className="conn" data-state={state}>
          <i className="conn-dot" />
          {state}
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
      </div>
    </div>
  );
}
