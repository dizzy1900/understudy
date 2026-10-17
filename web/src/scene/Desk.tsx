import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

import { COMMANDER_ORB_POSITION, DESK_POSITION } from "../layout";
import { useStream } from "../store";
import { COLORS, RUNG_COLORS } from "../theme";
import Bubble from "./Bubble";
import { columnTex, geo, glowTex, useOwned } from "./shared";

const GOLD = RUNG_COLORS.super;
const ORB_R = 0.4;
const PULSE_S = 1.3;

function additive(color: string, map: THREE.Texture | null, opacity: number, boost = 1) {
  return new THREE.MeshBasicMaterial({
    color: new THREE.Color(color).multiplyScalar(boost),
    map,
    transparent: true,
    opacity,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide,
    toneMapped: false,
  });
}

export default function Desk() {
  const orbGroup = useRef<THREE.Group>(null);
  const ringA = useRef<THREE.Mesh>(null);
  const ringB = useRef<THREE.Mesh>(null);
  const pulse = useRef({ at: -10, thoughtAt: 0, agentId: "" });

  const body = useOwned(() => new THREE.MeshStandardMaterial({ color: "#0e0e0e", metalness: 0.2, roughness: 0.85 }));
  const top = useOwned(() => new THREE.MeshStandardMaterial({ color: "#050505", metalness: 0.2, roughness: 0.8 }));
  const edge = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(GOLD).multiplyScalar(2.3), toneMapped: false }),
  );
  const accent = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(COLORS.accent).multiplyScalar(1.8), toneMapped: false }),
  );
  const screen = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(COLORS.healthy).multiplyScalar(0.9), toneMapped: false }),
  );
  const orb = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(GOLD).multiplyScalar(2.2), toneMapped: false }),
  );
  const halo = useOwned(
    () =>
      new THREE.SpriteMaterial({
        map: glowTex(),
        color: new THREE.Color(GOLD).multiplyScalar(1.6),
        transparent: true,
        opacity: 0.18,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );
  const ringMat = useOwned(() => additive(GOLD, null, 0.7, 1.8));
  const shaft = useOwned(() => additive(GOLD, columnTex(), 0.22, 1.4));
  const floorGlow = useOwned(() => additive(GOLD, glowTex(), 0.07, 1));

  useFrame((state) => {
    const t = state.clock.elapsedTime;
    const p = pulse.current;
    const commander = Object.values(useStream.getState().agents).find(
      (a) => a.role === "commander" && a.doneAt === null,
    );
    if (commander && (commander.id !== p.agentId || commander.thoughtAt !== p.thoughtAt)) {
      const fresh = commander.id === p.agentId || Date.now() - commander.thoughtAt < 2000;
      p.agentId = commander.id;
      p.thoughtAt = commander.thoughtAt;
      if (fresh && commander.thoughtAt > 0) p.at = t;
    }
    const age = t - p.at;
    const k = age < PULSE_S ? Math.exp(-age * 2.6) : 0;
    const breathe = 1 + 0.04 * Math.sin(t * 1.3);
    if (orbGroup.current) orbGroup.current.scale.setScalar(breathe * (1 + 0.28 * k));
    halo.opacity = 0.5 + 0.12 * Math.sin(t * 1.3) + 0.5 * k;
    ringMat.opacity = 0.55 + 0.4 * k;
    if (ringA.current) {
      ringA.current.rotation.z = t * 0.35;
      ringA.current.rotation.x = Math.PI / 2 + 0.35 * Math.sin(t * 0.4);
    }
    if (ringB.current) {
      ringB.current.rotation.z = -t * 0.5;
      ringB.current.rotation.y = 0.9 + 0.3 * Math.cos(t * 0.33);
    }
  });

  const [dx, , dz] = DESK_POSITION;
  const orbY = COMMANDER_ORB_POSITION[1];
  const commanderId = useStream((s) => Object.values(s.agents).find((a) => a.role === "commander" && a.doneAt === null)?.id);

  return (
    <group position={[dx, 0, dz]}>
      <mesh geometry={geo.box()} material={body} position={[0, 0.3, 0]} scale={[4.6, 0.6, 1.5]} />
      <mesh geometry={geo.box()} material={top} position={[0, 0.63, 0]} scale={[4.8, 0.07, 1.7]} />
      <mesh geometry={geo.box()} material={edge} position={[0, 0.62, 0.86]} scale={[4.8, 0.04, 0.04]} />
      <mesh geometry={geo.box()} material={edge} position={[-2.4, 0.62, 0]} scale={[0.04, 0.04, 1.7]} />
      <mesh geometry={geo.box()} material={edge} position={[2.4, 0.62, 0]} scale={[0.04, 0.04, 1.7]} />
      <mesh geometry={geo.box()} material={accent} position={[0, 0.07, 0.76]} scale={[4.4, 0.03, 0.03]} />
      {[-1.5, 1.5].map((x) => (
        <mesh
          key={x}
          geometry={geo.plane()}
          material={screen}
          position={[x, 0.675, 0.25]}
          rotation={[-Math.PI / 2, 0, 0]}
          scale={[1.1, 0.5, 1]}
        />
      ))}
      <mesh geometry={geo.plane()} material={floorGlow} position={[0, 0.014, 0.2]} rotation={[-Math.PI / 2, 0, 0]} scale={[9, 5.5, 1]} />
      <mesh geometry={geo.plane()} material={shaft} position={[0, (orbY + 0.67) / 2 + 0.1, 0]} scale={[0.9, orbY - 0.6, 1]} />
      <mesh
        geometry={geo.plane()}
        material={shaft}
        position={[0, (orbY + 0.67) / 2 + 0.1, 0]}
        rotation={[0, Math.PI / 2, 0]}
        scale={[0.9, orbY - 0.6, 1]}
      />
      <group position={[0, orbY, 0]}>
        <group ref={orbGroup}>
          <mesh geometry={geo.sphere()} material={orb} scale={ORB_R} />
          <sprite material={halo} scale={[4.4, 4.4, 1]} />
          <mesh ref={ringA} geometry={geo.torus()} material={ringMat} scale={ORB_R * 1.9} rotation={[Math.PI / 2, 0, 0]} />
          <mesh ref={ringB} geometry={geo.torus()} material={ringMat} scale={ORB_R * 2.5} rotation={[0.3, 0.9, 0]} />
        </group>
        {commanderId && <Bubble agentId={commanderId} height={0.75} showForMs={16000} />}
      </group>
    </group>
  );
}
