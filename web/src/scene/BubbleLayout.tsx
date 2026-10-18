import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

import { COUNTER_FRAME, COUNTER_POSITION } from "../layout";
import { bubbleHandles } from "./Bubble";
import type { BubbleHandle } from "./Bubble";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Placement {
  choice: number;
  x: number;
  y: number;
  shown: boolean;
}

const MAX_VISIBLE = 3;
const GAP = 16;
const EDGE = 10;
const PAD = 8;
const PANEL_SELECTOR = ".overlay-layer .panel, .overlay-layer .controls, .overlay-layer .hdr, .overlay-layer .caption";

const v = new THREE.Vector3();

function hits(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w + PAD && a.x + a.w + PAD > b.x && a.y < b.y + b.h + PAD && a.y + a.h + PAD > b.y;
}

/** Candidate top-left corners for a box of size w x h around the anchor point, in order of preference. */
function candidates(px: number, py: number, w: number, h: number): [number, number][] {
  const above = py - GAP - h;
  const below = py + GAP + 12;
  return [
    [px - w / 2, above],
    [px - 30, above],
    [px - w + 30, above],
    [px + 28, py - h / 2],
    [px - w - 28, py - h / 2],
    [px - w / 2, below],
    [px - 30, below],
    [px - w + 30, below],
    [px - w / 2, above - h - 10],
    [px + 28, py - h - 20],
    [px - w - 28, py - h - 20],
    [px + 28, py + 20],
    [px - w - 28, py + 20],
  ];
}

/** Places every speech bubble in screen space so they never cover each other, the token counter or the panels. */
export default function BubbleLayout() {
  const placed = useRef(new Map<string, Placement>());
  const sizes = useRef(new Map<string, { w: number; h: number }>());
  const panels = useRef<Rect[]>([]);
  const panelsAt = useRef(0);

  useFrame((state) => {
    if (bubbleHandles.size === 0) return;
    const { camera, size } = state;
    const now = performance.now();

    if (now - panelsAt.current > 400) {
      panelsAt.current = now;
      panels.current = Array.from(document.querySelectorAll<HTMLElement>(PANEL_SELECTOR)).map((el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height };
      });
    }

    const avoid: Rect[] = [...panels.current, counterRect(camera, size.width, size.height)];
    const wanting = Array.from(bubbleHandles.values())
      .filter((h) => h.wants)
      .sort((a, b) => b.priority - a.priority);
    const taken: Rect[] = [];
    const shown = new Set<string>();

    for (const h of wanting) {
      if (shown.size >= MAX_VISIBLE) break;
      h.anchor.getWorldPosition(v);
      v.project(camera);
      if (v.z > 1) continue;
      const px = (v.x * 0.5 + 0.5) * size.width;
      const py = (-v.y * 0.5 + 0.5) * size.height;
      if (px < -40 || px > size.width + 40 || py < -40 || py > size.height + 40) continue;

      const dim = measure(h, sizes.current);
      const prev = placed.current.get(h.id);
      const options = candidates(px, py, dim.w, dim.h);
      const fits = (i: number): boolean => {
        const [x, y] = options[i];
        if (x < EDGE || y < EDGE || x + dim.w > size.width - EDGE || y + dim.h > size.height - EDGE) return false;
        const rect = { x, y, w: dim.w, h: dim.h };
        return !avoid.some((r) => hits(rect, r)) && !taken.some((r) => hits(rect, r));
      };

      let choice = -1;
      // A bubble that is already on screen keeps its spot while it still fits, so it does not hop around.
      if (prev && prev.shown && fits(prev.choice)) {
        choice = prev.choice;
      } else {
        for (let i = 0; i < options.length; i++) {
          if (fits(i)) {
            choice = i;
            break;
          }
        }
      }
      if (choice < 0) continue;

      const [x, y] = options[choice];
      taken.push({ x, y, w: dim.w, h: dim.h });
      shown.add(h.id);
      const next: Placement = prev ?? { choice, x: 0, y: 0, shown: false };
      next.choice = choice;
      next.x = x - px;
      next.y = y - py;
      placed.current.set(h.id, next);
    }

    for (const h of bubbleHandles.values()) {
      const p = placed.current.get(h.id);
      const on = shown.has(h.id) && !!p;
      if (p) p.shown = on;
      apply(h, on, p);
    }
    for (const id of placed.current.keys()) if (!bubbleHandles.has(id)) placed.current.delete(id);
  });

  return null;
}

function measure(h: BubbleHandle, cache: Map<string, { w: number; h: number }>): { w: number; h: number } {
  let dim = cache.get(h.id);
  if (!dim || h.dirty) {
    dim = { w: h.box.offsetWidth || 220, h: h.box.offsetHeight || 60 };
    cache.set(h.id, dim);
    h.dirty = false;
  }
  return dim;
}

const lastStyle = new WeakMap<HTMLElement, string>();

function apply(h: BubbleHandle, on: boolean, p: Placement | undefined): void {
  const transform = p ? `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px)` : "";
  const key = `${on ? 1 : 0}|${transform}`;
  const before = lastStyle.get(h.root);
  if (before === key) return;
  lastStyle.set(h.root, key);
  // A bubble that just appeared jumps to its spot and fades in; one already visible glides when it moves.
  const wasOn = before?.startsWith("1") ?? false;
  h.root.style.transition = on && wasOn ? "opacity .45s ease, transform .35s ease" : "opacity .45s ease";
  h.root.style.opacity = on ? "1" : "0";
  if (transform) h.root.style.transform = transform;
}

const corner = new THREE.Vector3();

function counterRect(camera: THREE.Camera, width: number, height: number): Rect {
  const [cx, cy, cz] = COUNTER_POSITION;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      corner.set(cx + (sx * COUNTER_FRAME.w) / 2, cy + (sy * COUNTER_FRAME.h) / 2, cz).project(camera);
      const x = (corner.x * 0.5 + 0.5) * width;
      const y = (-corner.y * 0.5 + 0.5) * height;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
  }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}
