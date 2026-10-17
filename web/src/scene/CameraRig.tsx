import { OrbitControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";

export const CAMERA_TARGET: [number, number, number] = [-4.6, 1.0, -1.6];
export const CAMERA_POSITION: [number, number, number] = [-4.6, 18.4, 25.2];

const DRIFT_LIMIT = (12 * Math.PI) / 180;
const DRIFT_SPEED = 0.09;
const IDLE_RESUME_MS = 8000;
/** Fraction of the frame width kept free on the right for the overlay panels. */
const RIGHT_MARGIN = 0.25;
/** Positive moves the scene up in the frame, as a fraction of the frame height. */
const LIFT = -0.03;

export default function CameraRig() {
  const controls = useRef<OrbitControlsImpl>(null);
  const camera = useThree((s) => s.camera);
  const size = useThree((s) => s.size);
  const drift = useRef({ angle: 0, dir: 1, resumeAt: 0 });

  useEffect(() => {
    camera.setViewOffset(size.width, size.height, (size.width * RIGHT_MARGIN) / 2, size.height * LIFT, size.width, size.height);
    return () => camera.clearViewOffset();
  }, [camera, size.width, size.height]);

  useFrame((_, dt) => {
    const c = controls.current;
    if (!c) return;
    const d = drift.current;
    const idle = performance.now() >= d.resumeAt;
    c.autoRotate = idle;
    if (!idle) return;
    const step = Math.min(dt, 0.1);
    d.angle += d.dir * DRIFT_SPEED * ((Math.PI * 2) / 60) * step;
    if (Math.abs(d.angle) > DRIFT_LIMIT) {
      d.angle = Math.sign(d.angle) * DRIFT_LIMIT;
      d.dir = -d.dir;
    }
    c.autoRotateSpeed = d.dir * DRIFT_SPEED;
  });

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      target={CAMERA_TARGET}
      enablePan={false}
      enableDamping
      dampingFactor={0.06}
      rotateSpeed={0.5}
      zoomSpeed={0.6}
      minDistance={12}
      maxDistance={42}
      minPolarAngle={0.55}
      maxPolarAngle={1.42}
      autoRotate
      autoRotateSpeed={DRIFT_SPEED}
      onStart={() => {
        drift.current.resumeAt = Infinity;
      }}
      onEnd={() => {
        drift.current.resumeAt = performance.now() + IDLE_RESUME_MS;
      }}
    />
  );
}
