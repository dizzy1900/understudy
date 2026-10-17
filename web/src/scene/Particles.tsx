import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import { on } from "../bus";
import { CEILING_Y, COUNTER_POSITION, isProving, nodeIdPosition } from "../layout";
import { useStream } from "../store";
import { COLORS } from "../theme";
import { clamp, geo, smooth, useOwned } from "./shared";

const POOL = 1700;
const RISE_T = 1.1;
const RACK_TOP = 2.68;
const Z_CONVERGE = -6.2;
const Z_END = COUNTER_POSITION[2] + 0.35;
const SPARK_POOL = 260;
const MAX_SPAWN_PER_FRAME = 40;

function identityMatrices(mesh: THREE.InstancedMesh | null, n: number) {
  if (!mesh) return;
  const a = mesh.instanceMatrix.array as Float32Array;
  for (let i = 0; i < n; i++) {
    a.fill(0, i * 16, i * 16 + 16);
    a[i * 16 + 15] = 1;
  }
  mesh.instanceMatrix.needsUpdate = true;
}

function writeMatrix(a: Float32Array, i: number, x: number, y: number, z: number, s: number) {
  const o = i * 16;
  a[o] = s;
  a[o + 5] = s;
  a[o + 10] = s;
  a[o + 12] = x;
  a[o + 13] = y;
  a[o + 14] = z;
}

export function TokenField() {
  const mesh = useRef<THREE.InstancedMesh>(null);
  const material = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(COLORS.healthy).multiplyScalar(2.6),
        toneMapped: false,
        fog: false,
      }),
  );

  const sim = useMemo(
    () => ({
      x0: new Float32Array(POOL),
      z0: new Float32Array(POOL),
      jx: new Float32Array(POOL),
      age: new Float32Array(POOL),
      vz: new Float32Array(POOL),
      size: new Float32Array(POOL),
      phase: new Float32Array(POOL),
      alive: new Uint8Array(POOL),
      cursor: 0,
      acc: new Map<string, number>(),
      burst: new Map<string, number>(),
      pos: new Map<string, [number, number]>(),
    }),
    [],
  );

  useEffect(() => {
    identityMatrices(mesh.current, POOL);
    return on("tokens", (e) => {
      const n = Math.min(8, Math.ceil(e.data.tokens / 30));
      sim.burst.set(e.data.node_id, Math.min(14, (sim.burst.get(e.data.node_id) ?? 0) + n));
    });
  }, [sim]);

  useFrame((_, rawDt) => {
    const m = mesh.current;
    if (!m) return;
    const dt = Math.min(rawDt, 0.1);
    const arr = m.instanceMatrix.array as Float32Array;
    const s = useStream.getState();

    const spawn = (id: string) => {
      let p = sim.pos.get(id);
      if (!p) {
        const v = nodeIdPosition(id);
        p = [v[0], v[2]];
        sim.pos.set(id, p);
      }
      let i = sim.cursor;
      for (let k = 0; k < POOL && sim.alive[i]; k++) i = (i + 1) % POOL;
      sim.cursor = (i + 1) % POOL;
      sim.alive[i] = 1;
      sim.age[i] = 0;
      sim.x0[i] = p[0];
      sim.z0[i] = p[1] + (Math.random() - 0.5) * 0.5;
      sim.jx[i] = (Math.random() - 0.5) * 0.7;
      sim.vz[i] = 2.1 + Math.random() * 0.7;
      sim.size[i] = 0.05 + Math.random() * 0.06;
      sim.phase[i] = Math.random() * 6.28;
    };

    let budget = MAX_SPAWN_PER_FRAME;
    const order = s.nodeOrder;
    for (let n = 0; n < order.length; n++) {
      const id = order[n];
      const node = s.nodes[id];
      if (!node || isProving(id)) continue;
      let b = sim.burst.get(id) ?? 0;
      if (b > 0) {
        const k = Math.min(b, 3);
        for (let j = 0; j < k && budget > 0; j++, budget--) spawn(id);
        b -= k;
        sim.burst.set(id, b);
      }
      if (node.status !== "healthy") continue;
      const rate = clamp(5 + node.tokens_per_min / 25, 5, 13);
      let acc = (sim.acc.get(id) ?? Math.random()) + rate * dt;
      while (acc >= 1 && budget > 0) {
        spawn(id);
        acc -= 1;
        budget--;
      }
      sim.acc.set(id, acc % 1);
    }

    const counterY = COUNTER_POSITION[1];
    for (let i = 0; i < POOL; i++) {
      if (!sim.alive[i]) continue;
      const a = (sim.age[i] += dt);
      let x: number;
      let y: number;
      let z: number;
      let scale = sim.size[i];
      if (a < RISE_T) {
        const u = a / RISE_T;
        const e = u * u * (3 - 2 * u) * 0.6 + u * 0.4;
        x = sim.x0[i] + sim.jx[i] * (1 - 0.6 * u) + Math.sin(a * 4 + sim.phase[i]) * 0.04;
        y = RACK_TOP + (CEILING_Y - RACK_TOP) * e;
        z = sim.z0[i] - u * 0.25;
        scale *= 0.5 + 0.5 * smooth(u * 3);
      } else {
        const d = (a - RISE_T) * sim.vz[i];
        z = sim.z0[i] - 0.25 - d;
        if (z < Z_END) {
          sim.alive[i] = 0;
          writeMatrix(arr, i, 0, -50, 0, 0);
          continue;
        }
        const conv = smooth((Z_CONVERGE - z) / (Z_CONVERGE - Z_END));
        x = (sim.x0[i] + sim.jx[i] * 0.22) * (1 - conv);
        y = CEILING_Y + (counterY - CEILING_Y) * conv + Math.sin(a * 3 + sim.phase[i]) * 0.025;
        const tail = z - Z_END;
        if (tail < 0.8) scale *= 0.35 + 0.65 * (tail / 0.8);
      }
      writeMatrix(arr, i, x, y, z, scale);
    }
    m.instanceMatrix.needsUpdate = true;
  });

  return <instancedMesh ref={mesh} args={[geo.sphere(), material, POOL]} frustumCulled={false} />;
}

export function SparkField() {
  const mesh = useRef<THREE.InstancedMesh>(null);
  const material = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: new THREE.Color("#FF6A3A").multiplyScalar(3),
        toneMapped: false,
        fog: false,
      }),
  );
  const sim = useMemo(
    () => ({
      p: new Float32Array(SPARK_POOL * 3),
      v: new Float32Array(SPARK_POOL * 3),
      life: new Float32Array(SPARK_POOL),
      max: new Float32Array(SPARK_POOL),
      cursor: 0,
      acc: new Map<string, number>(),
    }),
    [],
  );

  useEffect(() => identityMatrices(mesh.current, SPARK_POOL), []);

  useFrame((_, rawDt) => {
    const m = mesh.current;
    if (!m) return;
    const dt = Math.min(rawDt, 0.1);
    const arr = m.instanceMatrix.array as Float32Array;
    const s = useStream.getState();
    const order = s.nodeOrder;
    for (let n = 0; n < order.length; n++) {
      const id = order[n];
      if (s.nodes[id]?.status !== "down") continue;
      const origin = nodeIdPosition(id);
      const scale = isProving(id) ? 0.82 : 1;
      let acc = (sim.acc.get(id) ?? 0) + 16 * dt;
      while (acc >= 1) {
        acc -= 1;
        const i = sim.cursor;
        sim.cursor = (i + 1) % SPARK_POOL;
        sim.p[i * 3] = origin[0] + (Math.random() - 0.5) * 1.1 * scale;
        sim.p[i * 3 + 1] = (0.5 + Math.random() * 2.0) * scale;
        sim.p[i * 3 + 2] = origin[2] + 0.6 * scale;
        sim.v[i * 3] = (Math.random() - 0.5) * 2.2;
        sim.v[i * 3 + 1] = 0.8 + Math.random() * 2.4;
        sim.v[i * 3 + 2] = 0.4 + Math.random() * 1.6;
        sim.max[i] = 0.5 + Math.random() * 0.6;
        sim.life[i] = sim.max[i];
      }
      sim.acc.set(id, acc);
    }
    for (let i = 0; i < SPARK_POOL; i++) {
      if (sim.life[i] <= 0) continue;
      sim.life[i] -= dt;
      if (sim.life[i] <= 0) {
        writeMatrix(arr, i, 0, -50, 0, 0);
        continue;
      }
      sim.v[i * 3 + 1] -= 7 * dt;
      sim.p[i * 3] += sim.v[i * 3] * dt;
      sim.p[i * 3 + 1] = Math.max(0.02, sim.p[i * 3 + 1] + sim.v[i * 3 + 1] * dt);
      sim.p[i * 3 + 2] += sim.v[i * 3 + 2] * dt;
      writeMatrix(arr, i, sim.p[i * 3], sim.p[i * 3 + 1], sim.p[i * 3 + 2], 0.055 * (sim.life[i] / sim.max[i]));
    }
    m.instanceMatrix.needsUpdate = true;
  });

  return <instancedMesh ref={mesh} args={[geo.sphere(), material, SPARK_POOL]} frustumCulled={false} />;
}

interface Segment {
  mid: [number, number, number];
  quat: THREE.Quaternion;
  length: number;
}

function segment(a: THREE.Vector3, b: THREE.Vector3): Segment {
  const mid = a.clone().add(b).multiplyScalar(0.5);
  const dummy = new THREE.Object3D();
  dummy.position.copy(mid);
  dummy.lookAt(b);
  return { mid: [mid.x, mid.y, mid.z], quat: dummy.quaternion.clone(), length: a.distanceTo(b) };
}

export function Lanes() {
  const key = useStream((s) =>
    s.nodeOrder
      .filter((id) => !isProving(id))
      .map((id) => nodeIdPosition(id)[0].toFixed(2))
      .filter((x, i, all) => all.indexOf(x) === i)
      .join("|"),
  );
  const lanes = useMemo(() => {
    const xs = key ? key.split("|").map(Number) : [];
    return xs.flatMap((x) => {
      const a = new THREE.Vector3(x, CEILING_Y, 3.0);
      const b = new THREE.Vector3(x, CEILING_Y, Z_CONVERGE);
      const c = new THREE.Vector3(0, COUNTER_POSITION[1], Z_END);
      return [segment(a, b), segment(b, c)];
    });
  }, [key]);

  const core = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(COLORS.healthy).multiplyScalar(1.5),
        transparent: true,
        opacity: 0.6,
        toneMapped: false,
        fog: false,
      }),
  );
  const haze = useOwned(
    () =>
      new THREE.MeshBasicMaterial({
        color: COLORS.healthy,
        transparent: true,
        opacity: 0.04,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
        fog: false,
      }),
  );

  return (
    <group>
      {lanes.map((l, i) => (
        <group key={i} position={l.mid} quaternion={l.quat}>
          <mesh geometry={geo.box()} material={core} scale={[0.05, 0.05, l.length]} />
          <mesh geometry={geo.box()} material={haze} scale={[0.4, 0.4, l.length]} />
        </group>
      ))}
    </group>
  );
}
