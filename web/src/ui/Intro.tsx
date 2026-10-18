import { useEffect, useState } from "react";
import { create } from "zustand";

import { RUNG_COLORS } from "../theme";

const KEY = "understudy.intro.dismissed.v1";

function readDismissed(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

function writeDismissed() {
  try {
    window.localStorage.setItem(KEY, "1");
  } catch {
    /* private mode or blocked storage: the card simply shows again next visit */
  }
}

interface IntroState {
  open: boolean;
  setOpen: (open: boolean) => void;
}

/** Shared so the header's help chip can reopen the card after it has been dismissed. */
export const useIntro = create<IntroState>((set) => ({
  open: !readDismissed(),
  setOpen: (open) => set({ open }),
}));

const RUNG_KEY: Array<{ rung: keyof typeof RUNG_COLORS; label: string; note: string }> = [
  { rung: "super", label: "SUPER", note: "big model, first contact" },
  { rung: "lightning", label: "LIGHTNING", note: "small model, learned fix" },
  { rung: "script", label: "SCRIPT", note: "compiled, near-zero cost" },
];

export default function Intro() {
  const open = useIntro((s) => s.open);
  const setOpen = useIntro((s) => s.setOpen);
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const dismiss = () => {
    writeDismissed();
    setOpen(false);
  };

  if (!mounted || !open) return null;

  return (
    <div className="intro-backdrop interactive" onClick={dismiss} role="presentation">
      <aside
        className="intro panel"
        role="dialog"
        aria-labelledby="intro-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="eyebrow">Welcome to the control room</div>
        <h2 id="intro-title">An on-call team that gets cheaper every shift.</h2>
        <p>
          <b>Understudy</b> is a crew of Nemotron agents (on Crusoe Managed Inference) keeping an inference fleet
          healthy. Each node in the scene is a server; when one fails, an agent triages it, applies a fix, and a referee
          verifies the repair before it counts.
        </p>
        <p>
          Every verified fix climbs the <b>skill ladder</b> on the right: from an expensive frontier model, to a cheap
          small model, to a compiled script. Watch the cost per incident fall as the team learns.
        </p>
        <div className="rung-key">
          {RUNG_KEY.map(({ rung, label, note }) => (
            <span key={rung} style={{ "--c": RUNG_COLORS[rung] } as React.CSSProperties}>
              <i />
              {label}
              <em>{note}</em>
            </span>
          ))}
        </div>
        <div className="legend">
          <div>
            <kbd>S</kbd> Storm: break several nodes at once
          </div>
          <div>
            <kbd>Space</kbd> Hold to talk to the on-call lead
          </div>
          <div>
            <kbd>B</kbd> Ask for a briefing on what happened
          </div>
          <div>
            <kbd>R</kbd> Replay a past shift
          </div>
          <div>
            <kbd>M</kbd> Mute voice
          </div>
          <div>
            <kbd>F</kbd> Fullscreen
          </div>
          <div>
            <kbd>P</kbd> Pause or resume the demo
          </div>
        </div>
        <div className="intro-actions">
          <span className="hint">Click any ladder pill to read the skill card behind it.</span>
          <button type="button" className="btn btn-primary" onClick={dismiss} autoFocus>
            Enter the control room {"\u2192"}
          </button>
        </div>
      </aside>
    </div>
  );
}
