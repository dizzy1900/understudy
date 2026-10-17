import { Environment, Grid, Lightformer, MeshReflectorMaterial } from "@react-three/drei";
import * as THREE from "three";

import { COLORS } from "../theme";
import { COUNTER_POSITION, DESK_POSITION } from "../layout";
import { glowTex, useOwned } from "./shared";

const WALL_Z = -11.4;

export function Lights() {
  return (
    <>
      <ambientLight intensity={0.5} color="#b8bcc4" />
      <hemisphereLight args={["#6f8fb8", "#07080B", 0.55]} />
      <directionalLight position={[-6, 14, 16]} intensity={1.7} color="#f2f2f2" />
      <pointLight position={[0, 4.8, -9]} intensity={12} distance={22} color={COLORS.healthy} decay={2} />
      <pointLight position={[DESK_POSITION[0], 3.4, DESK_POSITION[2] - 0.5]} intensity={10} distance={12} color="#F5C451" decay={2} />
      <Environment resolution={64} frames={1} environmentIntensity={0.55}>
        <Lightformer form="rect" intensity={2.2} position={[0, 9, 4]} rotation-x={Math.PI / 2} scale={[24, 6, 1]} color="#ffffff" />
        <Lightformer form="rect" intensity={1.4} position={[-12, 4, 0]} rotation-y={Math.PI / 2} scale={[20, 5, 1]} color="#ffffff" />
        <Lightformer form="rect" intensity={1.8} position={[0, 4, -14]} scale={[26, 4, 1]} color={COLORS.healthy} />
      </Environment>
    </>
  );
}

export function Floor() {
  return (
    <>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[-4, 0, -2]}>
        <planeGeometry args={[110, 80]} />
        <MeshReflectorMaterial
          color="#0b0e14"
          metalness={0.6}
          roughness={0.75}
          resolution={512}
          blur={[260, 80]}
          mixBlur={1}
          mixStrength={14}
          mirror={0.7}
          depthScale={0}
        />
      </mesh>
      <Grid
        position={[-4, 0.02, -2]}
        args={[110, 80]}
        cellSize={1}
        cellThickness={0.6}
        cellColor="#16303d"
        sectionSize={5}
        sectionThickness={1.1}
        sectionColor="#1f5266"
        fadeDistance={48}
        fadeStrength={1.6}
        infiniteGrid
      />
    </>
  );
}

export function FarWall() {
  const glow = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        map: glowTex(),
        color: COLORS.healthy,
        transparent: true,
        opacity: 0.12,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );
  return (
    <group>
      <mesh position={[-4, 6, WALL_Z]}>
        <planeGeometry args={[70, 22]} />
        <meshStandardMaterial color="#0a0d13" metalness={0.5} roughness={0.7} />
      </mesh>
      <mesh position={[COUNTER_POSITION[0], COUNTER_POSITION[1], WALL_Z + 0.05]} material={glow}>
        <planeGeometry args={[22, 9]} />
      </mesh>
    </group>
  );
}
