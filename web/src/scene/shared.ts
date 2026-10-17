import { useEffect, useMemo } from "react";
import * as THREE from "three";

import { COLORS } from "../theme";

type Disposable = { dispose(): void };

/** Module level assets are shared by every rack, drone and particle. three re-uploads a disposed
 * resource the next time it is drawn, so disposing on unmount is safe across StrictMode remounts. */
const registry: Disposable[] = [];

export function keep<T extends Disposable>(item: T): T {
  registry.push(item);
  return item;
}

export function disposeShared(): void {
  for (const item of registry) item.dispose();
}

export function useOwned<T extends Disposable>(factory: () => T): T {
  const item = useMemo(factory, []);
  useEffect(() => () => item.dispose(), [item]);
  return item;
}

export const BG_COLOR = new THREE.Color(COLORS.bg);

const colorCache = new Map<string, THREE.Color>();
export function colorOf(hex: string): THREE.Color {
  let c = colorCache.get(hex);
  if (!c) {
    c = new THREE.Color(hex);
    colorCache.set(hex, c);
  }
  return c;
}

let unitBox: THREE.BoxGeometry | null = null;
let unitPlane: THREE.PlaneGeometry | null = null;
let unitSphere: THREE.SphereGeometry | null = null;
let unitRing: THREE.TorusGeometry | null = null;

export const geo = {
  box: () => (unitBox ??= keep(new THREE.BoxGeometry(1, 1, 1))),
  plane: () => (unitPlane ??= keep(new THREE.PlaneGeometry(1, 1))),
  sphere: () => (unitSphere ??= keep(new THREE.SphereGeometry(1, 20, 14))),
  torus: () => (unitRing ??= keep(new THREE.TorusGeometry(1, 0.045, 8, 48))),
};

function makeCanvas(w: number, h: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  return [canvas, canvas.getContext("2d")!];
}

let glowTexture: THREE.CanvasTexture | null = null;
/** Radial falloff, white in the centre and transparent at the edge. */
export function glowTex(): THREE.CanvasTexture {
  if (glowTexture) return glowTexture;
  const [canvas, ctx] = makeCanvas(128, 128);
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.25, "rgba(255,255,255,0.45)");
  g.addColorStop(0.6, "rgba(255,255,255,0.12)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 128, 128);
  glowTexture = keep(new THREE.CanvasTexture(canvas));
  glowTexture.colorSpace = THREE.SRGBColorSpace;
  return glowTexture;
}

let beamTexture: THREE.CanvasTexture | null = null;
/** Vertical profile that is bright in the middle and fades to both ends; also fades sideways. */
export function beamTex(): THREE.CanvasTexture {
  if (beamTexture) return beamTexture;
  const [canvas, ctx] = makeCanvas(64, 128);
  const g = ctx.createLinearGradient(0, 0, 0, 128);
  g.addColorStop(0, "rgba(255,255,255,0)");
  g.addColorStop(0.5, "rgba(255,255,255,1)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 64, 128);
  ctx.globalCompositeOperation = "destination-in";
  const h = ctx.createLinearGradient(0, 0, 64, 0);
  h.addColorStop(0, "rgba(0,0,0,0)");
  h.addColorStop(0.5, "rgba(0,0,0,1)");
  h.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = h;
  ctx.fillRect(0, 0, 64, 128);
  beamTexture = keep(new THREE.CanvasTexture(canvas));
  return beamTexture;
}

let columnTexture: THREE.CanvasTexture | null = null;
/** Alpha is 1 at the bottom and 0 at the top, for light shafts rising from a rack. */
export function columnTex(): THREE.CanvasTexture {
  if (columnTexture) return columnTexture;
  const [canvas, ctx] = makeCanvas(64, 128);
  const g = ctx.createLinearGradient(0, 128, 0, 0);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 64, 128);
  ctx.globalCompositeOperation = "destination-in";
  const h = ctx.createLinearGradient(0, 0, 64, 0);
  h.addColorStop(0, "rgba(0,0,0,0)");
  h.addColorStop(0.5, "rgba(0,0,0,1)");
  h.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = h;
  ctx.fillRect(0, 0, 64, 128);
  columnTexture = keep(new THREE.CanvasTexture(canvas));
  return columnTexture;
}

let faceTexture: THREE.CanvasTexture | null = null;
/** Front panel of a rack: drive bays, vent slits and a few indicator dots. */
export function rackFaceTex(): THREE.CanvasTexture {
  if (faceTexture) return faceTexture;
  const W = 256;
  const H = 560;
  const [canvas, ctx] = makeCanvas(W, H);
  ctx.fillStyle = "#12161d";
  ctx.fillRect(0, 0, W, H);
  const units = 11;
  const pad = 14;
  const uh = (H - pad * 2) / units;
  for (let i = 0; i < units; i++) {
    const y = pad + i * uh;
    ctx.fillStyle = "#1a2029";
    ctx.fillRect(12, y + 3, W - 24, uh - 6);
    ctx.strokeStyle = "#2a323f";
    ctx.lineWidth = 2;
    ctx.strokeRect(12, y + 3, W - 24, uh - 6);
    ctx.fillStyle = "#06080c";
    const slits = 9;
    for (let s = 0; s < slits; s++) {
      ctx.fillRect(38 + s * 13, y + uh * 0.32, 6, uh * 0.36);
    }
    ctx.fillStyle = i % 3 === 0 ? "#3a6a78" : "#2a3340";
    ctx.beginPath();
    ctx.arc(W - 34, y + uh / 2, 3.4, 0, Math.PI * 2);
    ctx.fill();
  }
  faceTexture = keep(new THREE.CanvasTexture(canvas));
  faceTexture.colorSpace = THREE.SRGBColorSpace;
  faceTexture.anisotropy = 4;
  return faceTexture;
}

const CSS = `
.sc-label{font-family:var(--font-mono);color:#E8ECF4;text-align:center;white-space:nowrap;pointer-events:none;user-select:none;
  transform:translate(-50%,-50%);}
.sc-label .id{display:inline-block;font-weight:700;font-size:19px;letter-spacing:.04em;padding:2px 9px;border-radius:4px;
  background:rgba(7,8,11,.78);border:1px solid rgba(62,230,255,.55);text-shadow:0 0 8px rgba(62,230,255,.55);}
.sc-label .st{display:block;margin:3px auto 0;width:max-content;font-weight:700;font-size:13px;letter-spacing:.14em;padding:1px 7px;border-radius:3px;
  background:rgba(7,8,11,.85);opacity:0;transition:opacity .35s;}
.sc-label.small .id{font-size:15px;padding:1px 7px;}
.sc-bubble{position:absolute;left:0;top:0;pointer-events:none;user-select:none;width:max-content;max-width:300px;
  font-family:var(--font-mono);color:#E8ECF4;opacity:0;transition:opacity .45s ease;}
.sc-bubble .box{border:1px solid var(--rc,#F5C451);border-radius:7px;background:rgba(8,10,15,.84);padding:6px 10px 7px;
  box-shadow:0 0 18px -4px var(--rc,#F5C451);}
.sc-bubble .who{font-family:var(--font-ui);font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--rc,#F5C451);margin-bottom:2px;}
.sc-bubble .txt{font-size:16px;line-height:1.28;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;}
.sc-bubble .tool{display:inline-block;margin-top:5px;font-size:14px;font-weight:600;padding:1px 8px;border-radius:999px;
  background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);}
.sc-bubble .tool.ok b{color:#38E08C;}
.sc-bubble .tool.bad b{color:#FF3B3B;}
.sc-bubble .tool.none{display:none;}
.sc-bubble .tail{position:absolute;width:1px;height:14px;background:var(--rc,#F5C451);opacity:.7;}
.sc-stamp{position:absolute;left:0;top:0;pointer-events:none;user-select:none;}
.sc-stamp .anchor{transform:translate(-50%,-50%);}
.sc-stamp .face{font-family:var(--font-ui);text-align:center;padding:6px 18px 8px;border:4px solid var(--sc);border-radius:8px;
  color:var(--sc);background:rgba(6,8,12,.86);box-shadow:0 0 26px -2px var(--sc),inset 0 0 18px -6px var(--sc);
  text-shadow:0 0 14px var(--sc);animation:sc-stamp 7s cubic-bezier(.2,.9,.2,1) both;animation-delay:var(--delay,0s);}
.sc-stamp .face .v{font-size:38px;font-weight:900;letter-spacing:.1em;line-height:1.05;}
.sc-stamp .face .d{font-family:var(--font-mono);font-size:19px;font-weight:700;letter-spacing:.06em;}
.sc-stamp.rejected .face{animation:sc-stamp 7s cubic-bezier(.2,.9,.2,1) both,sc-shake .6s linear 0.3s both;
  animation-delay:var(--delay,0s),calc(var(--delay,0s) + .3s);}
@keyframes sc-stamp{
  0%{transform:scale(3) rotate(-14deg);opacity:0;}
  4%{transform:scale(.9) rotate(-5deg);opacity:1;}
  6%{transform:scale(1.04) rotate(-5deg);}
  8%{transform:scale(1) rotate(-5deg);}
  86%{transform:scale(1) rotate(-5deg);opacity:1;}
  100%{transform:scale(1.03) rotate(-5deg);opacity:0;}}
@keyframes sc-shake{
  0%,100%{translate:0 0;}15%{translate:-9px 2px;}30%{translate:8px -2px;}45%{translate:-6px 1px;}60%{translate:5px 0;}80%{translate:-2px 0;}}
.sc-counter{font-family:var(--font-mono);text-align:center;pointer-events:none;user-select:none;transform:translate(-50%,-50%);white-space:nowrap;}
.sc-counter .cap{font-family:var(--font-ui);font-weight:700;font-size:24px;letter-spacing:.34em;color:#9fe9f5;text-shadow:0 0 14px rgba(62,230,255,.7);}
.sc-counter .num{font-weight:800;font-size:84px;line-height:1.05;color:#EFFCFF;font-variant-numeric:tabular-nums;
  text-shadow:0 0 12px #3EE6FF,0 0 36px rgba(62,230,255,.75);}
.sc-bay{font-family:var(--font-ui);text-align:center;white-space:nowrap;pointer-events:none;user-select:none;transform:translate(-50%,-50%);}
.sc-bay .t{font-weight:800;font-size:19px;letter-spacing:.2em;padding:3px 12px;border-radius:4px;color:#CDEFF7;
  background:rgba(7,8,11,.8);border:1px solid rgba(62,230,255,.5);transition:color .4s,border-color .4s;}
.sc-bay .s{font-family:var(--font-mono);font-size:13px;margin-top:3px;letter-spacing:.12em;color:#FFB020;opacity:0;transition:opacity .4s;}
.sc-bay.on .t{color:#FFB020;border-color:rgba(255,176,32,.8);}
.sc-bay.on .s{opacity:1;}
`;

export function installSceneStyles(): void {
  if (typeof document === "undefined") return;
  let el = document.getElementById("scene-css");
  if (!el) {
    el = document.createElement("style");
    el.id = "scene-css";
    document.head.appendChild(el);
  }
  el.textContent = CSS;
}

export const smooth = (x: number) => {
  const t = Math.min(1, Math.max(0, x));
  return t * t * (3 - 2 * t);
};
export const clamp = (x: number, a: number, b: number) => Math.min(b, Math.max(a, x));

/** Cheap deterministic hash in 0..1 used for per-instance phase offsets. */
export function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10007) / 10007;
}
