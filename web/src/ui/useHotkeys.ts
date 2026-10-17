import { useEffect } from "react";

import { useStream } from "../store";
import { cancelBack, imBack } from "../voice/flow";
import { sendStorm, toggleFullscreen, toggleReplay, useUi, voiceStart, voiceStop } from "./actions";

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  return el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable;
}

function takesSpace(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el && !!el.tagName && (el.tagName === "BUTTON" || el.tagName === "A" || isTyping(el));
}

export function useHotkeys(): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.code === "Space") {
        if (!takesSpace(event.target)) event.preventDefault();
        if (!event.repeat) voiceStart();
        return;
      }
      if (event.repeat) return;
      if (event.key === "Escape") {
        if (useUi.getState().backPhase !== "idle") cancelBack();
        return;
      }
      switch (event.key.toLowerCase()) {
        case "s":
          void sendStorm();
          break;
        case "b":
          void imBack();
          break;
        case "r":
          toggleReplay();
          break;
        case "f":
          toggleFullscreen();
          break;
        case "m":
          useStream.getState().toggleMuted();
          break;
        default:
          break;
      }
    };

    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code === "Space") voiceStop();
    };

    // A mouse click leaves the button focused, and the next Space would then press it again.
    const onClick = (event: MouseEvent) => {
      const el = event.target instanceof Element ? event.target.closest("button") : null;
      if (el && event.detail > 0) el.blur();
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", voiceStop);
    document.addEventListener("click", onClick);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", voiceStop);
      document.removeEventListener("click", onClick);
    };
  }, []);
}
