import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";

import { COUNTER_POSITION } from "../layout";
import { tokensServed, useStream } from "../store";
import { COLORS } from "../theme";
import { geo, useOwned } from "./shared";

const FRAME_W = 11;
const FRAME_H = 2.5;
const BAR = 0.09;

function group(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

export default function Counter() {
  const numEl = useRef<HTMLDivElement>(null);
  const shown = useRef(0);
  const target = useRef(0);
  const text = useRef("");
  const primed = useRef(false);

  const frame = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(COLORS.healthy).multiplyScalar(2.2),
        toneMapped: false,
        fog: false,
      }),
  );
  const panel = useOwned(
    () => new THREE.MeshBasicMaterial({ color: "#04080c", transparent: true, opacity: 0.92, fog: false }),
  );

  useEffect(() => {
    target.current = tokensServed(useStream.getState());
    return useStream.subscribe((s) => {
      target.current = tokensServed(s);
    });
  }, []);

  useFrame((_, dt) => {
    const el = numEl.current;
    if (!el) return;
    if (!primed.current && target.current > 0) {
      shown.current = target.current;
      primed.current = true;
    }
    const diff = target.current - shown.current;
    if (Math.abs(diff) < 0.5) shown.current = target.current;
    else shown.current += diff * (1 - Math.exp(-2.6 * Math.min(dt, 0.1)));
    const next = group(shown.current);
    if (next !== text.current) {
      text.current = next;
      el.textContent = next;
    }
  });

  const [x, y, z] = COUNTER_POSITION;
  return (
    <group position={[x, y, z]}>
      <mesh geometry={geo.plane()} material={panel} scale={[FRAME_W, FRAME_H, 1]} />
      <mesh geometry={geo.box()} material={frame} position={[0, FRAME_H / 2, 0]} scale={[FRAME_W + BAR, BAR, BAR]} />
      <mesh geometry={geo.box()} material={frame} position={[0, -FRAME_H / 2, 0]} scale={[FRAME_W + BAR, BAR, BAR]} />
      <mesh geometry={geo.box()} material={frame} position={[-FRAME_W / 2, 0, 0]} scale={[BAR, FRAME_H, BAR]} />
      <mesh geometry={geo.box()} material={frame} position={[FRAME_W / 2, 0, 0]} scale={[BAR, FRAME_H, BAR]} />
      <Html
        distanceFactor={22}
        pointerEvents="none"
        zIndexRange={[40, 0]}
        style={{ pointerEvents: "none" }}
      >
        <div className="sc-counter">
          <div className="cap">TOKENS SERVED</div>
          <div ref={numEl} className="num">
            0
          </div>
        </div>
      </Html>
    </group>
  );
}
