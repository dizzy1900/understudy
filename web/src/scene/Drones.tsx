import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";

import {
  COMMANDER_ORB_POSITION,
  hoverPosition,
  LIBRARY_POSITION,
  PROVING_CENTER,
} from "../layout";
import { AGENT_LINGER_MS, useStream } from "../store";
import { rungColor } from "../theme";
import type { AgentRole, AgentRung } from "../types";
import Bubble from "./Bubble";
import { clamp, geo, glowTex, hash01, smooth, useOwned } from "./shared";

const SIZE: Record<AgentRung, number> = { super: 0.42, ultra: 0.34, lightning: 0.3, script: 0.26, nano: 0.2 };
const TRAIL_N = 14;
const LAUNCH_S = 2.4;
const RETURN_S = 1.5;
const FLASH_S = 0.65;
const SATELLITE_BUBBLE_MS = 4000;
/** White and gold saturate under bloom much sooner than the saturated rung colours. */
const BOOST: Record<AgentRung, number> = { super: 1.9, ultra: 2.3, lightning: 2.4, script: 1.25, nano: 2.3 };

const ORB = new THREE.Vector3(...COMMANDER_ORB_POSITION);
const p0 = new THREE.Vector3();
const p1 = new THREE.Vector3();
const p2 = new THREE.Vector3();
const p3 = new THREE.Vector3();
const tmpCol = new THREE.Color();
const WHITE = new THREE.Color("#ffffff");

function bezier(out: THREE.Vector3, t: number) {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  out.set(
    a * p0.x + b * p1.x + c * p2.x + d * p3.x,
    a * p0.y + b * p1.y + c * p2.y + d * p3.y,
    a * p0.z + b * p1.z + c * p2.z + d * p3.z,
  );
}

/** Control points that lift the drone over the racks before it drops onto its station. */
function arc(from: THREE.Vector3, to: THREE.Vector3, lift: number) {
  p0.copy(from);
  p3.copy(to);
  p1.set(from.x + (to.x - from.x) * 0.15, Math.max(from.y, to.y) + lift, from.z + (to.z - from.z) * 0.25);
  p2.set(to.x - (to.x - from.x) * 0.05, to.y + lift * 0.7, to.z + 2.6);
}

type Mode = "launch" | "hover" | "return";

interface DroneProps {
  id: string;
  role: AgentRole;
  rung: AgentRung;
  nodeId: string | null;
  slot: number;
  count: number;
}

function stationFor(role: AgentRole, nodeId: string | null, slot: number, count: number): THREE.Vector3 | null {
  if (role === "librarian") return new THREE.Vector3(...LIBRARY_POSITION);
  if (role === "promoter") return new THREE.Vector3(PROVING_CENTER[0], 3.9, PROVING_CENTER[2] + 0.3);
  if ((role === "responder" || role === "verifier" || role === "watchdog") && nodeId) {
    const [x, y, z] = hoverPosition(nodeId, 3.75);
    return new THREE.Vector3(x + (slot - (count - 1) / 2) * 1.05, y, z + 0.35);
  }
  return null;
}

function Drone({ id, role, rung, nodeId, slot, count }: DroneProps) {
  const group = useRef<THREE.Group>(null);
  const spin = useRef<THREE.Group>(null);
  const station = useMemo(() => stationFor(role, nodeId, slot, count), [role, nodeId, slot, count]);
  const satellite = station === null;
  const phase = useMemo(() => hash01(id) * 6.28, [id]);
  const size = SIZE[rung] ?? 0.3;
  const color = rungColor(rung);
  const gate = useRef(false);
  const boost = BOOST[rung] ?? 2;

  const coreMat = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(boost), transparent: true, toneMapped: false }),
  );
  const ringMat = useOwned(
    () => new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(1.8), transparent: true, toneMapped: false }),
  );
  const haloMat = useOwned(
    () =>
      new THREE.SpriteMaterial({
        map: glowTex(),
        color: new THREE.Color(color).multiplyScalar(rung === "script" ? 0.8 : 1.3),
        transparent: true,
        opacity: 0.5,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );
  const trailGeo = useOwned(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(TRAIL_N * 3), 3));
    g.setAttribute("color", new THREE.BufferAttribute(new Float32Array(TRAIL_N * 3), 3));
    return g;
  });
  const trailMat = useOwned(
    () =>
      new THREE.LineBasicMaterial({
        vertexColors: true,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        toneMapped: false,
      }),
  );
  const trail = useMemo(() => {
    const line = new THREE.Line(trailGeo, trailMat);
    line.frustumCulled = false;
    return line;
  }, [trailGeo, trailMat]);

  const st = useRef({
    mode: "launch" as Mode,
    t0: 0,
    from: new THREE.Vector3(),
    pos: new THREE.Vector3(),
    born: 0,
    lastEsc: -1,
    flashAt: -10,
    trailTimer: 0,
    initialised: false,
    trailSeeded: false,
    returnFrom: new THREE.Vector3(),
    returnAt: 0,
  });

  useFrame((state, rawDt) => {
    const g = group.current;
    if (!g) return;
    const dt = Math.min(rawDt, 0.1);
    const t = state.clock.elapsedTime;
    const a = useStream.getState().agents[id];
    if (!a) {
      g.visible = false;
      return;
    }
    const s = st.current;
    const nowMs = Date.now();

    if (!s.initialised) {
      s.initialised = true;
      s.lastEsc = a.escalatedAt ?? -1;
      s.born = t;
      const ageS = (nowMs - a.spawnedAt) / 1000;
      if (satellite) {
        s.mode = "hover";
      } else if (ageS < LAUNCH_S - 0.3 && a.doneAt === null) {
        s.mode = "launch";
        s.t0 = t - Math.max(0, ageS);
      } else {
        s.mode = "hover";
        s.pos.copy(station!);
      }
      if (a.escalatedAt && nowMs - a.escalatedAt < FLASH_S * 1000) s.flashAt = t - (nowMs - a.escalatedAt) / 1000;
    }
    if (a.escalatedAt && a.escalatedAt !== s.lastEsc) {
      s.lastEsc = a.escalatedAt;
      s.flashAt = t;
    }

    if (a.doneAt !== null && s.mode !== "return") {
      s.mode = "return";
      s.returnFrom.copy(s.pos);
      s.returnAt = t;
    }

    // Position
    let fade = 1;
    if (satellite) {
      const ang = t * 0.7 + phase;
      const r = 1.55 + (phase % 1) * 0.5;
      s.pos.set(ORB.x + Math.cos(ang) * r, ORB.y + 0.15 + Math.sin(ang * 1.7) * 0.35, ORB.z + Math.sin(ang) * r * 0.8);
      gate.current = true;
      if (a.doneAt !== null) fade = 1 - smooth((nowMs - a.doneAt) / (AGENT_LINGER_MS * 0.6));
    } else if (s.mode === "launch") {
      const u = clamp((t - s.t0) / LAUNCH_S, 0, 1);
      arc(ORB, station!, 3.4);
      bezier(s.pos, smooth(u));
      gate.current = false;
      if (u >= 1) s.mode = "hover";
    } else if (s.mode === "hover") {
      const bob = Math.sin(t * 1.6 + phase) * 0.1;
      const k = 1 - Math.exp(-4 * dt);
      s.pos.x += (station!.x - s.pos.x) * k;
      s.pos.y += (station!.y + bob - s.pos.y) * k;
      s.pos.z += (station!.z - s.pos.z) * k;
      gate.current = true;
    } else {
      const u = clamp((t - s.returnAt) / RETURN_S, 0, 1);
      arc(s.returnFrom, ORB, 1.8);
      bezier(s.pos, smooth(u));
      gate.current = false;
      fade = 1 - smooth(((nowMs - a.doneAt!) - 600) / (AGENT_LINGER_MS - 700));
    }
    if (!s.trailSeeded) {
      s.trailSeeded = true;
      for (let i = 0; i < TRAIL_N; i++) trailGeo.attributes.position.setXYZ(i, s.pos.x, s.pos.y, s.pos.z);
    }
    const grow = smooth((t - s.born) / 0.5);

    // Escalation flash
    const fAge = t - s.flashAt;
    const flash = fAge >= 0 && fAge < FLASH_S ? Math.sin((fAge / FLASH_S) * Math.PI) : 0;
    const scale = grow * (0.45 + 0.55 * fade) * (1 + 0.6 * flash);

    g.visible = fade > 0.01;
    g.position.copy(s.pos);
    g.scale.setScalar(scale);
    if (spin.current) spin.current.rotation.set(Math.PI / 2 + Math.sin(t * 0.8 + phase) * 0.45, 0, t * 1.6 + phase);

    coreMat.opacity = fade;
    ringMat.opacity = fade * 0.9;
    haloMat.opacity = fade * (0.55 + 0.35 * flash);
    tmpCol.set(color).multiplyScalar(boost);
    coreMat.color.copy(tmpCol).lerp(WHITE, flash).multiplyScalar(1 + flash * 1.4);
    ringMat.color.set(color).multiplyScalar(boost * 0.75).lerp(WHITE, flash);

    // Trail
    s.trailTimer += dt;
    if (s.trailTimer > 0.045) {
      s.trailTimer = 0;
      const pos = trailGeo.attributes.position as THREE.BufferAttribute;
      const col = trailGeo.attributes.color as THREE.BufferAttribute;
      const arrP = pos.array as Float32Array;
      arrP.copyWithin(3, 0, (TRAIL_N - 1) * 3);
      arrP[0] = s.pos.x;
      arrP[1] = s.pos.y;
      arrP[2] = s.pos.z;
      const base = tmpCol.set(color);
      const arrC = col.array as Float32Array;
      for (let i = 0; i < TRAIL_N; i++) {
        const f = (1 - i / (TRAIL_N - 1)) * 0.4 * fade;
        arrC[i * 3] = base.r * f;
        arrC[i * 3 + 1] = base.g * f;
        arrC[i * 3 + 2] = base.b * f;
      }
      pos.needsUpdate = true;
      col.needsUpdate = true;
    }
  });

  const core = size * 0.95;

  return (
    <>
      <primitive object={trail} />
      <group ref={group}>
        <mesh geometry={geo.sphere()} material={coreMat} scale={core} />
        <sprite material={haloMat} scale={[core * 7, core * 7, 1]} />
        <group ref={spin}>
          <mesh geometry={geo.torus()} material={ringMat} scale={core * 1.7} />
        </group>
        <Bubble
          agentId={id}
          height={core + 0.35}
          gate={gate}
          showForMs={satellite ? SATELLITE_BUBBLE_MS : undefined}
        />
      </group>
    </>
  );
}

export default function Drones() {
  const sig = useStream((s) =>
    Object.values(s.agents)
      .filter((a) => a.role !== "commander")
      .map((a) => `${a.id}~${a.role}~${a.rung}~${a.node_id ?? ""}~${a.spawnedAt}~${a.doneAt === null ? 0 : 1}`)
      .join("|"),
  );

  const drones = useMemo(() => {
    if (!sig) return [];
    const rows = sig.split("|").map((row) => {
      const [id, role, rung, node, spawnedAt, done] = row.split("~");
      return { id, role: role as AgentRole, rung: rung as AgentRung, node: node || null, spawnedAt: Number(spawnedAt), done: done === "1" };
    });
    const out: DroneProps[] = [];
    for (const r of rows) {
      let slot = 0;
      let count = 1;
      if (r.node && (r.role === "responder" || r.role === "verifier" || r.role === "watchdog")) {
        const mates = rows
          .filter((o) => o.node === r.node && (o.role === "responder" || o.role === "verifier" || o.role === "watchdog") && (!o.done || o.id === r.id))
          .sort((x, y) => x.spawnedAt - y.spawnedAt || x.id.localeCompare(y.id));
        slot = Math.max(0, mates.findIndex((m) => m.id === r.id));
        count = mates.length;
      }
      out.push({ id: r.id, role: r.role, rung: r.rung, nodeId: r.node, slot, count });
    }
    return out;
  }, [sig]);

  return (
    <>
      {drones.map((d) => (
        <Drone key={d.id} {...d} />
      ))}
    </>
  );
}
