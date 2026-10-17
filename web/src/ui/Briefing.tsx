import { useEffect } from "react";

import { useStream } from "../store";

const AUTO_DISMISS_MS = 40_000;

export default function Briefing() {
  const briefing = useStream((s) => s.briefing);
  const setBriefing = useStream((s) => s.setBriefing);

  useEffect(() => {
    if (!briefing) return;
    const id = window.setTimeout(() => {
      if (useStream.getState().briefing === briefing) setBriefing(null);
    }, AUTO_DISMISS_MS);
    return () => window.clearTimeout(id);
  }, [briefing, setBriefing]);

  if (!briefing) return null;

  return (
    <aside className="briefing panel interactive" key={briefing} role="status">
      <div className="briefing-head">
        <span className="eyebrow">Briefing</span>
        <button type="button" className="icon-btn" onClick={() => setBriefing(null)} aria-label="Dismiss briefing">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <p className="briefing-text">{briefing}</p>
      <div className="briefing-timer" style={{ animationDuration: `${AUTO_DISMISS_MS}ms` }} />
    </aside>
  );
}
