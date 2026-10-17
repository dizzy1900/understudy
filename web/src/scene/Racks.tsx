import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import { isProving, nodePosition, PROVING_RACK_SCALE, RACK_SIZE } from "../layout";
import { useStream } from "../store";
import { COLORS, STATUS_COLORS } from "../theme";
import type { NodeState } from "../types";
import {
  beamTex,
  clamp,
  colorOf,
  columnTex,
  geo,
  glowTex,
  hash01,
  keep,
  rackFaceTex,
  useOwned,
} from "./shared";

const FLASH_S = 1.7;
const STATUS_TEXT: Record<NodeState, string> = {
  healthy: "",
  degraded: "DEGRADED",
  down: "DOWN",
  repairing: "REPAIRING",
};

let ringGeo: THREE.RingGeometry | null = null;
const ringGeometry = () => (ringGeo ??= keep(new THREE.RingGeometry(1.0, 1.14, 64)));

let bodyMat: THREE.MeshStandardMaterial | null = null;
let plinthMat: THREE.MeshStandardMaterial | null = null;
let faceMat: THREE.MeshStandardMaterial | null = null;
let beamMat: THREE.MeshBasicMaterial | null = null;

function sharedMaterials() {
  bodyMat ??= keep(new THREE.MeshStandardMaterial({ color: "#0e0e0e", metalness: 0.2, roughness: 0.85 }));
  plinthMat ??= keep(new THREE.MeshStandardMaterial({ color: "#050505", metalness: 0.1, roughness: 0.9 }));
  faceMat ??= keep(
    new THREE.MeshStandardMaterial({
      map: rackFaceTex(),
      color: "#ffffff",
      emissive: "#ffffff",
      emissiveMap: rackFaceTex(),
      emissiveIntensity: 0.35,
      metalness: 0.4,
      roughness: 0.6,
    }),
  );
  beamMat ??= keep(
    new THREE.MeshBasicMaterial({
      map: beamTex(),
      color: new THREE.Color(COLORS.repairing).multiplyScalar(2.2),
      transparent: true,
      opacity: 0.85,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
      toneMapped: false,
    }),
  );
  return { body: bodyMat, plinth: plinthMat, face: faceMat, beam: beamMat };
}

function level(status: NodeState, t: number, seed: number): number {
  switch (status) {
    case "healthy":
      return 1 + 0.12 * Math.sin(t * 1.7 + seed * 6.28);
    case "degraded": {
      const f = Math.sin(t * 21 + seed * 9) * Math.sin(t * 5.3 + seed * 3);
      return f > 0.15 ? 1.05 : 0.3;
    }
    case "down": {
      const pulse = 0.5 + 0.5 * Math.sin(t * 4.2 + seed);
      const drop = Math.sin(t * 37 + seed * 11) > 0.9 ? 0.2 : 1;
      return (0.35 + 0.8 * pulse) * drop;
    }
    case "repairing":
      return 0.95 + 0.15 * Math.sin(t * 6);
  }
}

function Rack({ id }: { id: string }) {
  const row = useStream((s) => s.nodes[id]?.row ?? 1);
  const col = useStream((s) => s.nodes[id]?.col ?? 1);
  const position = useMemo(() => nodePosition({ id, row, col }), [id, row, col]);
  const proving = isProving(id);
  const scale = proving ? PROVING_RACK_SCALE : 1;
  const seed = useMemo(() => hash01(id), [id]);
  const mats = sharedMaterials();

  const led = useOwned(() => new THREE.MeshBasicMaterial({ color: COLORS.healthy, toneMapped: false }));
  const top = useOwned(() => new THREE.MeshBasicMaterial({ color: COLORS.healthy, toneMapped: false }));
  const shaft = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        map: columnTex(),
        color: COLORS.healthy,
        transparent: true,
        opacity: 0.2,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        side: THREE.DoubleSide,
        toneMapped: false,
      }),
  );
  const ring = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: COLORS.healthy,
        transparent: true,
        opacity: 0.3,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );
  const floorGlow = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        map: glowTex(),
        color: COLORS.healthy,
        transparent: true,
        opacity: 0.07,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );

  const cur = useRef(new THREE.Color(COLORS.healthy));
  const tmp = useRef(new THREE.Color());
  const scanV = useRef<THREE.Mesh>(null);
  const scanH = useRef<THREE.Mesh>(null);
  const labelRoot = useRef<HTMLDivElement>(null);
  const statusEl = useRef<HTMLSpanElement>(null);
  const prevStatus = useRef<NodeState | null>(null);

  useFrame((state, dt) => {
    const t = state.clock.elapsedTime;
    const s = useStream.getState();
    const status = s.nodes[id]?.status ?? "healthy";
    const stamp = s.stamps[id];
    const age = stamp ? (Date.now() - stamp.at) / 1000 : Infinity;

    cur.current.lerp(colorOf(STATUS_COLORS[status]), 1 - Math.exp(-9 * dt));
    let lvl = level(status, t, seed);
    const c = tmp.current.copy(cur.current);
    if (age < FLASH_S && stamp) {
      const f = 1 - age / FLASH_S;
      c.lerp(colorOf(stamp.verdict === "verified" ? COLORS.verified : COLORS.incident), clamp(f * 2.5, 0, 1));
      lvl += 1.4 * f;
    }

    led.color.copy(c).multiplyScalar(0.45 + 1.25 * lvl);
    top.color.copy(c).multiplyScalar(0.1 + 0.3 * lvl);
    shaft.color.copy(c);
    ring.color.copy(c);
    floorGlow.color.copy(c);

    const bad = status === "down";
    const warn = status === "degraded" || status === "repairing";
    shaft.opacity = (bad ? 0.1 + 0.28 * lvl : warn ? 0.1 + 0.2 * lvl : 0.17 + 0.05 * lvl) + (age < FLASH_S ? 0.3 : 0);
    ring.opacity = bad ? 0.35 + 0.65 * lvl : warn ? 0.35 + 0.3 * lvl : 0.3;
    floorGlow.opacity = bad ? 0.2 + 0.4 * lvl : warn ? 0.22 + 0.15 * lvl : 0.2;

    const scanning = status === "repairing";
    if (scanV.current && scanH.current) {
      scanV.current.visible = scanning;
      scanH.current.visible = scanning;
      if (scanning) {
        const p = 0.5 + 0.5 * Math.sin(t * 2.1 + seed * 6);
        const y = 0.2 + p * (RACK_SIZE[1] - 0.2);
        scanV.current.position.y = y;
        scanH.current.position.y = y;
      }
    }

    if (prevStatus.current !== status && labelRoot.current && statusEl.current) {
      prevStatus.current = status;
      const text = STATUS_TEXT[status];
      statusEl.current.textContent = text;
      statusEl.current.style.opacity = text ? "1" : "0";
      statusEl.current.style.color = STATUS_COLORS[status];
      statusEl.current.style.border = `1px solid ${STATUS_COLORS[status]}`;
      const idEl = labelRoot.current.firstElementChild as HTMLElement | null;
      if (idEl) {
        idEl.style.borderColor = STATUS_COLORS[status];
        idEl.style.textShadow = `0 0 8px ${STATUS_COLORS[status]}`;
      }
    }
  });

  useEffect(() => {
    prevStatus.current = null;
  }, []);

  return (
    <group position={position} scale={scale}>
      <mesh geometry={geo.box()} material={mats.plinth} position={[0, 0.05, 0]} scale={[1.44, 0.1, 1.24]} />
      <mesh geometry={geo.box()} material={mats.body} position={[0, RACK_SIZE[1] / 2 + 0.05, 0]} scale={RACK_SIZE} />
      <mesh
        geometry={geo.plane()}
        material={mats.face}
        position={[0, RACK_SIZE[1] / 2 + 0.05, RACK_SIZE[2] / 2 + 0.002]}
        scale={[1.18, 2.34, 1]}
      />
      <mesh geometry={geo.box()} material={led} position={[-0.6, 1.3, 0.57]} scale={[0.075, 2.2, 0.04]} />
      <mesh geometry={geo.box()} material={led} position={[0.6, 1.3, 0.57]} scale={[0.035, 2.2, 0.04]} />
      <mesh geometry={geo.box()} material={top} position={[0, RACK_SIZE[1] + 0.08, 0]} scale={[1.36, 0.06, 1.16]} />
      <mesh geometry={geo.box()} material={led} position={[0, RACK_SIZE[1] + 0.12, 0.58]} scale={[1.36, 0.035, 0.035]} />
      <mesh geometry={geo.box()} material={led} position={[0, RACK_SIZE[1] + 0.12, -0.58]} scale={[1.36, 0.035, 0.035]} />
      <mesh geometry={geo.box()} material={led} position={[-0.68, RACK_SIZE[1] + 0.12, 0]} scale={[0.035, 0.035, 1.16]} />
      <mesh geometry={geo.box()} material={led} position={[0.68, RACK_SIZE[1] + 0.12, 0]} scale={[0.035, 0.035, 1.16]} />
      <mesh geometry={geo.plane()} material={shaft} position={[0, RACK_SIZE[1] + 1.15, 0]} scale={[1.5, 2.2, 1]} />
      <mesh
        geometry={geo.plane()}
        material={shaft}
        position={[0, RACK_SIZE[1] + 1.15, 0]}
        rotation={[0, Math.PI / 2, 0]}
        scale={[1.3, 2.2, 1]}
      />
      <mesh geometry={ringGeometry()} material={ring} position={[0, 0.02, 0]} rotation={[-Math.PI / 2, 0, 0]} scale={[1.22, 1.1, 1]} />
      <mesh geometry={geo.plane()} material={floorGlow} position={[0, 0.012, 0.1]} rotation={[-Math.PI / 2, 0, 0]} scale={[4.6, 4.2, 1]} />
      <mesh ref={scanV} geometry={geo.plane()} material={mats.beam} position={[0, 1, 0.64]} scale={[1.7, 0.9, 1]} visible={false} />
      <mesh
        ref={scanH}
        geometry={geo.plane()}
        material={mats.beam}
        position={[0, 1, 0]}
        rotation={[-Math.PI / 2, 0, 0]}
        scale={[1.7, 1.5, 1]}
        visible={false}
      />
      <Html
        position={[0, RACK_SIZE[1] - 0.28, 0.62]}
        pointerEvents="none"
        zIndexRange={[60, 0]}
        style={{ pointerEvents: "none" }}
      >
        <div ref={labelRoot} className={`sc-label${proving ? " small" : ""}`}>
          <span className="id">{id}</span>
          <span ref={statusEl} className="st" />
        </div>
      </Html>
    </group>
  );
}

export default function Racks() {
  const order = useStream((s) => s.nodeOrder.join("|"));
  const ids = useMemo(() => (order ? order.split("|") : []), [order]);
  return (
    <>
      {ids.map((id) => (
        <Rack key={id} id={id} />
      ))}
    </>
  );
}
