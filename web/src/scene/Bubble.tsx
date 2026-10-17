import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import type { RefObject } from "react";
import type * as THREE from "three";

import { useStream } from "../store";
import { rungColor } from "../theme";

/** What the layout pass needs to place a bubble. The bubble itself only decides whether it has something to say. */
export interface BubbleHandle {
  id: string;
  root: HTMLDivElement;
  box: HTMLDivElement;
  anchor: THREE.Object3D;
  wants: boolean;
  priority: number;
  /** Set when the text changed, so the layout measures the box again. */
  dirty: boolean;
}

export const bubbleHandles = new Map<string, BubbleHandle>();

const ROLE_WEIGHT: Record<string, number> = {
  responder: 4,
  verifier: 3,
  watchdog: 2.5,
  librarian: 2,
  promoter: 2,
  commander: 1,
};

interface Props {
  agentId: string;
  /** World height above the object origin. */
  height?: number;
  /** When set, the bubble hides this long after the last thought. */
  showForMs?: number;
  /** The bubble stays hidden while this is false, for example during flight. */
  gate?: RefObject<boolean>;
}

export default function Bubble({ agentId, height = 0.5, showForMs, gate }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const anchor = useRef<THREE.Group>(null);
  const who = useRef<HTMLDivElement>(null);
  const txt = useRef<HTMLDivElement>(null);
  const tool = useRef<HTMLDivElement>(null);
  const handle = useRef<BubbleHandle | null>(null);
  const last = useRef({ thought: "", toolAt: 0, rung: "" });

  // Html renders its children in a separate React root a moment after mount, so the elements are looked up
  // from the frame loop instead of an effect.
  useEffect(
    () => () => {
      if (handle.current && bubbleHandles.get(agentId) === handle.current) bubbleHandles.delete(agentId);
      handle.current = null;
    },
    [agentId],
  );

  useFrame(() => {
    if (!handle.current) {
      const el = root.current;
      const boxEl = box.current;
      const obj = anchor.current;
      if (!el || !boxEl || !obj) return;
      handle.current = { id: agentId, root: el, box: boxEl, anchor: obj, wants: false, priority: 0, dirty: true };
      bubbleHandles.set(agentId, handle.current);
    }
    const h = handle.current;
    if (!who.current || !txt.current || !tool.current) return;
    const a = useStream.getState().agents[agentId];
    if (!a) {
      h.wants = false;
      return;
    }
    const st = last.current;
    if (st.rung !== a.rung) {
      st.rung = a.rung;
      h.root.style.setProperty("--rc", rungColor(a.rung));
      who.current.textContent = a.role === "commander" ? "commander" : `${a.role} / ${a.rung}`;
      h.dirty = true;
    }
    if (st.thought !== a.thought) {
      st.thought = a.thought;
      txt.current.textContent = a.thought;
      txt.current.style.display = a.thought ? "-webkit-box" : "none";
      h.dirty = true;
    }
    const toolAt = a.tool?.at ?? 0;
    if (toolAt !== st.toolAt) {
      st.toolAt = toolAt;
      h.dirty = true;
      if (a.tool) {
        tool.current.className = `tool ${a.tool.ok ? "ok" : "bad"}`;
        tool.current.innerHTML = "";
        // The responder protocol reports an unparsable turn as the tool "none".
        tool.current.append(`${a.tool.name && !/^none$/i.test(a.tool.name) ? a.tool.name : "no action"} `);
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
    h.wants = show;
    h.priority = (ROLE_WEIGHT[a.role] ?? 1) + Math.max(0, 1 - (now - a.thoughtAt) / 20_000);
  });

  return (
    <group ref={anchor} position={[0, height, 0]}>
      <Html pointerEvents="none" zIndexRange={[90, 20]} style={{ pointerEvents: "none" }}>
        <div ref={root} className="sc-bubble">
          <div ref={box} className="box">
            <div ref={who} className="who" />
            <div ref={txt} className="txt" />
            <div ref={tool} className="tool none" />
          </div>
        </div>
      </Html>
    </group>
  );
}
