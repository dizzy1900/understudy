import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import type { RefObject } from "react";

import { useStream } from "../store";
import { rungColor } from "../theme";

export type BubbleAlign = "center" | "left" | "right";

const ANCHOR: Record<BubbleAlign, string> = {
  center: "translate(-50%, -100%)",
  // Left/right bubbles sit beside the drone so two agents on one rack do not cover each other.
  left: "translate(calc(-100% + 34px), -100%)",
  right: "translate(-34px, -100%)",
};

interface Props {
  agentId: string;
  /** Pixels above the anchor point. */
  lift?: number;
  /** World height above the object origin. */
  height?: number;
  align?: BubbleAlign;
  /** When set, the bubble hides this long after the last thought. */
  showForMs?: number;
  /** The bubble stays hidden while this is false, for example during flight. */
  gate?: RefObject<boolean>;
}

export default function Bubble({ agentId, lift = 22, height = 0.5, align = "center", showForMs, gate }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const who = useRef<HTMLDivElement>(null);
  const txt = useRef<HTMLDivElement>(null);
  const tool = useRef<HTMLDivElement>(null);
  const last = useRef({ thought: "", toolAt: 0, opacity: -1, rung: "" });

  useFrame(() => {
    const el = root.current;
    if (!el || !who.current || !txt.current || !tool.current) return;
    const a = useStream.getState().agents[agentId];
    const st = last.current;
    if (!a) {
      if (st.opacity !== 0) {
        el.style.opacity = "0";
        st.opacity = 0;
      }
      return;
    }
    if (st.rung !== a.rung) {
      st.rung = a.rung;
      el.style.setProperty("--rc", rungColor(a.rung));
      who.current.textContent = a.role === "commander" ? "commander" : `${a.role} / ${a.rung}`;
    }
    if (st.thought !== a.thought) {
      st.thought = a.thought;
      txt.current.textContent = a.thought;
      txt.current.style.display = a.thought ? "-webkit-box" : "none";
    }
    const toolAt = a.tool?.at ?? 0;
    if (toolAt !== st.toolAt) {
      st.toolAt = toolAt;
      if (a.tool) {
        tool.current.className = `tool ${a.tool.ok ? "ok" : "bad"}`;
        tool.current.innerHTML = "";
        tool.current.append(`${a.tool.name} `);
        const mark = document.createElement("b");
        mark.textContent = a.tool.ok ? "\u2713" : "\u2717";
        tool.current.append(mark);
      } else {
        tool.current.className = "tool none";
      }
    }
    const now = Date.now();
    let show = (a.thought !== "" || a.tool !== null) && (gate ? gate.current !== false : true);
    if (show && showForMs !== undefined && now - a.thoughtAt > showForMs) show = false;
    if (show && a.doneAt !== null && now - a.doneAt > 500) show = false;
    const opacity = show ? 1 : 0;
    if (opacity !== st.opacity) {
      st.opacity = opacity;
      el.style.opacity = String(opacity);
    }
  });

  return (
    <Html
      position={[0, height, 0]}
      pointerEvents="none"
      zIndexRange={[90, 20]}
      style={{ pointerEvents: "none" }}
    >
      <div ref={root} className="sc-bubble" style={{ transform: `${ANCHOR[align]} translateY(-${lift}px)` }}>
        <div className="box">
          <div ref={who} className="who" />
          <div ref={txt} className="txt" />
          <div ref={tool} className="tool none" />
        </div>
      </div>
    </Html>
  );
}
