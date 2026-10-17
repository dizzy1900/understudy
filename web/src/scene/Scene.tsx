import { Canvas } from "@react-three/fiber";
import { Bloom, EffectComposer, Vignette } from "@react-three/postprocessing";
import { useEffect } from "react";
import * as THREE from "three";

import BubbleLayout from "./BubbleLayout";
import CameraRig, { CAMERA_POSITION } from "./CameraRig";
import { FarWall, Floor, Lights } from "./Floor";
import Counter from "./Counter";
import Desk from "./Desk";
import Drones from "./Drones";
import { Lanes, SparkField, TokenField } from "./Particles";
import Proving from "./Proving";
import Racks from "./Racks";
import Stamps from "./Stamps";
import { BG_COLOR, disposeShared, installSceneStyles } from "./shared";

installSceneStyles();

export default function Scene() {
  useEffect(() => () => disposeShared(), []);

  return (
    <div style={{ position: "absolute", inset: 0, isolation: "isolate" }}>
      <Canvas
        dpr={[1, 1.5]}
        camera={{ position: CAMERA_POSITION, fov: 36, near: 0.5, far: 140 }}
        gl={{ antialias: true, powerPreference: "high-performance", toneMapping: THREE.NoToneMapping }}
      >
        <color attach="background" args={[BG_COLOR]} />
        <fog attach="fog" args={[BG_COLOR, 40, 85]} />
        <Lights />
        <Floor />
        <FarWall />
        <Racks />
        <Lanes />
        <TokenField />
        <SparkField />
        <Counter />
        <Proving />
        <Desk />
        <Drones />
        <Stamps />
        <BubbleLayout />
        <CameraRig />
        <EffectComposer multisampling={0}>
          <Bloom mipmapBlur intensity={0.25} luminanceThreshold={0.97} luminanceSmoothing={0.1} radius={0.4} />
          <Vignette eskil={false} offset={0.25} darkness={0.7} />
        </EffectComposer>
      </Canvas>
    </div>
  );
}
