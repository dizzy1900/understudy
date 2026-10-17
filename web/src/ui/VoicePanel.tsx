import { useEffect, useRef, useState } from "react";

import { runCommand } from "../voice/flow";
import { micLevel, useVoice } from "../voice/voiceStore";
import { useUi } from "./actions";

const HEARD_MS = 5200;

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el && !!el.tagName && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
}

/** A ring around the frame while the microphone is open. Its glow follows the level the recorder reports. */
export function ListeningRing() {
  const listen = useVoice((s) => s.listen);
  const ring = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (listen !== "listening") return;
    let frame = 0;
    let level = 0;
    const tick = () => {
      level += (micLevel.value - level) * 0.25;
      ring.current?.style.setProperty("--lvl", level.toFixed(3));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [listen]);

  return <div ref={ring} className="listen-ring" data-state={listen} aria-hidden="true" />;
}

/** What the microphone is doing, and then what it heard. */
export function HeardChip() {
  const listen = useVoice((s) => s.listen);
  const heard = useVoice((s) => s.heard);
  const heardAt = useVoice((s) => s.heardAt);
  const fetching = useUi((s) => s.backPhase === "fetching");
  const [fresh, setFresh] = useState(false);

  useEffect(() => {
    if (!heardAt) return;
    setFresh(true);
    const id = window.setTimeout(() => setFresh(false), HEARD_MS);
    return () => window.clearTimeout(id);
  }, [heardAt]);

  const label =
    listen === "listening"
      ? "Listening"
      : listen === "transcribing"
        ? "Transcribing"
        : fetching
          ? "Preparing the briefing"
          : "";
  if (!label && !(fresh && heard)) return null;

  return (
    <div className="heard" role="status" data-state={label ? (listen === "idle" ? "transcribing" : listen) : "heard"}>
      {label ? (
        <>
          <i className="heard-dot" />
          <span>{label}</span>
        </>
      ) : (
        <>
          <span className="heard-label">Command</span>
          <span className="heard-text">{heard}</span>
        </>
      )}
    </div>
  );
}

/** Typed commands, for rooms where the microphone is not an option. */
export function CommandBox() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/" || typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      event.preventDefault();
      input.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const command = text.trim();
    if (!command || busy) return;
    setBusy(true);
    setText("");
    try {
      await runCommand(command);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="command" onSubmit={(event) => void submit(event)}>
      <input
        ref={input}
        type="text"
        value={text}
        enterKeyHint="send"
        autoComplete="off"
        spellCheck={false}
        aria-label="Type a command"
        placeholder="Type a command, e.g. surprise me"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") event.currentTarget.blur();
        }}
      />
      <kbd>/</kbd>
    </form>
  );
}
