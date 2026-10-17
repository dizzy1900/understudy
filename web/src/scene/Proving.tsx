import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

import { PROVING_CENTER } from "../layout";
import { useStream } from "../store";
import { COLORS } from "../theme";
import { geo, glowTex, smooth, useOwned } from "./shared";

const W = 5.2;
const D = 5.6;
const H = 3.5;
const BAR = 0.05;
const [CX, , CZ] = PROVING_CENTER;
const ACTIVE = new THREE.Color(COLORS.repairing);
const IDLE = new THREE.Color(COLORS.healthy);

export default function Proving() {
  const label = useRef<HTMLDivElement>(null);
  const sub = useRef<HTMLDivElement>(null);
  const light = useRef<THREE.PointLight>(null);
  const level = useRef(0);
  const wasActive = useRef<boolean | null>(null);

  const glass = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: "#3a3a3a",
        transparent: true,
        opacity: 0.055,
        depthWrite: false,
        side: THREE.DoubleSide,
        toneMapped: false,
      }),
  );
  const bar = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(COLORS.healthy).multiplyScalar(1.7), toneMapped: false }),
  );
  const floor = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        map: glowTex(),
        color: COLORS.healthy,
        transparent: true,
        opacity: 0.18,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );
  const slab = useOwned(() => new THREE.MeshStandardMaterial({ color: "#050505", metalness: 0.1, roughness: 0.9 }));

  useFrame((_, dt) => {
    const s = useStream.getState();
    let active = false;
    for (const id of s.nodeOrder) {
      if (s.nodes[id]?.zone === "proving" && s.nodes[id].status === "repairing") {
        active = true;
        break;
      }
    }
    level.current += ((active ? 1 : 0) - level.current) * (1 - Math.exp(-4 * Math.min(dt, 0.1)));
    const k = smooth(level.current);
    bar.color.copy(IDLE).lerp(ACTIVE, k).multiplyScalar(1.7 + 0.8 * k);
    floor.color.copy(IDLE).lerp(ACTIVE, k);
    floor.opacity = 0.16 + 0.5 * k;
    glass.opacity = 0.055 + 0.05 * k;
    if (light.current) light.current.intensity = 55 * k;
    if (wasActive.current !== active && label.current && sub.current) {
      wasActive.current = active;
      label.current.classList.toggle("on", active);
      sub.current.textContent = active ? "TRIAL RUNNING" : "";
    }
  });

  const hw = W / 2;
  const hd = D / 2;
  const edges: Array<[number, number, number, number, number, number]> = [
    // x, y, z, sx, sy, sz
    [0, H, hd, W, BAR, BAR],
    [0, H, -hd, W, BAR, BAR],
    [-hw, H, 0, BAR, BAR, D],
    [hw, H, 0, BAR, BAR, D],
    [0, 0.03, hd, W, BAR, BAR],
    [0, 0.03, -hd, W, BAR, BAR],
    [-hw, 0.03, 0, BAR, BAR, D],
    [hw, 0.03, 0, BAR, BAR, D],
    [-hw, H / 2, hd, BAR, H, BAR],
    [hw, H / 2, hd, BAR, H, BAR],
    [-hw, H / 2, -hd, BAR, H, BAR],
    [hw, H / 2, -hd, BAR, H, BAR],
  ];

  return (
    <group position={[CX, 0, CZ]}>
      <mesh geometry={geo.box()} material={slab} position={[0, 0.02, 0]} scale={[W, 0.04, D]} />
      <mesh geometry={geo.plane()} material={floor} position={[0, 0.05, 0]} rotation={[-Math.PI / 2, 0, 0]} scale={[W * 1.5, D * 1.4, 1]} />
      <mesh geometry={geo.plane()} material={glass} position={[0, H / 2, hd]} scale={[W, H, 1]} />
      <mesh geometry={geo.plane()} material={glass} position={[0, H / 2, -hd]} scale={[W, H, 1]} />
      <mesh geometry={geo.plane()} material={glass} position={[-hw, H / 2, 0]} rotation={[0, Math.PI / 2, 0]} scale={[D, H, 1]} />
      <mesh geometry={geo.plane()} material={glass} position={[hw, H / 2, 0]} rotation={[0, Math.PI / 2, 0]} scale={[D, H, 1]} />
      <mesh geometry={geo.plane()} material={glass} position={[0, H, 0]} rotation={[-Math.PI / 2, 0, 0]} scale={[W, D, 1]} />
      {edges.map((e, i) => (
        <mesh key={i} geometry={geo.box()} material={bar} position={[e[0], e[1], e[2]]} scale={[e[3], e[4], e[5]]} />
      ))}
      <pointLight ref={light} position={[0, 2.6, 0.6]} color={COLORS.repairing} intensity={0} distance={9} decay={2} />
      <Html position={[0, 0.05, hd + 0.75]} pointerEvents="none" zIndexRange={[60, 0]} style={{ pointerEvents: "none" }}>
        <div ref={label} className="sc-bay">
          <div className="t">Proving ground</div>
          <div ref={sub} className="s" />
        </div>
      </Html>
    </group>
  );
}
