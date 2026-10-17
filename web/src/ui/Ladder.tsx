import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { LadderMove } from "../store";
import { useStream } from "../store";
import { HELDOUT_CLASSES, RUNGS, RUNG_COLORS, TRAINING_CLASSES, prettyClass } from "../theme";
import type { Rung } from "../types";
import { useReplay } from "./replayStore";

const SLIDE_MS = 950;
const SETTLE_MS = 600;
const TOAST_MS = 7000;
const PARTICLES = 34;

const TRAINING_INDEX = new Map<string, number>(TRAINING_CLASSES.map((name, i) => [name, i]));
const HELDOUT = new Set<string>(HELDOUT_CLASSES);

function classOrder(a: string, b: string): number {
  const rank = (n: string) => (TRAINING_INDEX.has(n) ? TRAINING_INDEX.get(n)! : HELDOUT.has(n) ? 100 : 200);
  return rank(a) - rank(b) || a.localeCompare(b);
}

function rungOf(ladder: Record<string, Rung>, name: string): Rung {
  const rung = ladder[name];
  return RUNGS.includes(rung) ? rung : "super";
}

interface Toast {
  key: number;
  fault_class: string;
  rung: Rung;
  direction: "promote" | "demote";
  text: string;
}

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  size: number;
  life: number;
  age: number;
  color: string;
}

function toastFor(move: LadderMove): Toast {
  return {
    key: move.key,
    fault_class: move.fault_class,
    rung: move.to_rung,
    direction: move.direction,
    text: `${move.passed}/${move.total} passed, $${move.mean_cost_usd.toFixed(4)}`,
  };
}

export default function Ladder() {
  const replaying = useReplay((s) => s.active);
  const liveLadder = useStream((s) => s.ladder);
  const liveMoves = useStream((s) => s.ladderMoves);
  const replayLadder = useReplay((s) => s.ladder);
  const replayMoves = useReplay((s) => s.moves);
  const selected = useStream((s) => s.selectedClass);
  const selectClass = useStream((s) => s.selectClass);

  const ladder = replaying ? replayLadder : liveLadder;
  const moves = replaying ? replayMoves : liveMoves;
  const names = useMemo(() => Object.keys(ladder).sort(classOrder), [ladder]);

  const panelRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const snapshot = useRef(new Map<string, { x: number; y: number }>());
  const lastSignature = useRef<string | null>(null);
  const seen = useRef<Set<number> | null>(null);
  const lastSource = useRef(replaying);
  const timers = useRef<number[]>([]);
  const particles = useRef<Particle[]>([]);
  const raf = useRef(0);
  const [toast, setToast] = useState<Toast | null>(null);

  const later = (fn: () => void, ms: number) => {
    timers.current.push(window.setTimeout(fn, ms));
  };

  const draw = () => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) {
      raf.current = 0;
      return;
    }
    let last = performance.now();
    const frame = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const dpr = canvas.width / Math.max(1, canvas.clientWidth);
      particles.current = particles.current.filter((p) => (p.age += dt) < p.life);
      for (const p of particles.current) {
        p.vx *= 1 - 2.4 * dt;
        p.vy = p.vy * (1 - 2.4 * dt) + 120 * dt;
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        const alpha = 1 - p.age / p.life;
        ctx.globalAlpha = alpha;
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(p.x * dpr, p.y * dpr, p.size * dpr * (0.6 + 0.4 * alpha), 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (particles.current.length > 0) raf.current = requestAnimationFrame(frame);
      else {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        raf.current = 0;
      }
    };
    raf.current = requestAnimationFrame(frame);
  };

  const burst = (name: string, color: string) => {
    const panel = panelRef.current;
    const canvas = canvasRef.current;
    const el = panel?.querySelector<HTMLElement>(`[data-pill="${name}"]`);
    if (!panel || !canvas || !el) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(panel.clientWidth * dpr);
    const h = Math.round(panel.clientHeight * dpr);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const cx = el.offsetLeft + el.offsetWidth / 2;
    const cy = el.offsetTop + el.offsetHeight / 2;
    for (let i = 0; i < PARTICLES; i++) {
      const angle = Math.random() * Math.PI * 2;
      const speed = 70 + Math.random() * 240;
      particles.current.push({
        x: cx + (Math.random() - 0.5) * el.offsetWidth * 0.6,
        y: cy + (Math.random() - 0.5) * el.offsetHeight * 0.4,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 40,
        size: 1.6 + Math.random() * 2.6,
        life: 0.55 + Math.random() * 0.5,
        age: 0,
        color,
      });
    }
    if (raf.current === 0) draw();
  };

  const celebrate = (move: LadderMove, el: HTMLElement | undefined) => {
    if (el) {
      const cls = move.direction === "promote" ? "fx-sweep" : "fx-flash";
      el.classList.remove("fx-sweep", "fx-flash");
      void el.offsetWidth;
      el.classList.add(cls);
      later(() => el.classList.remove(cls), 1800);
      if (move.direction === "promote") later(() => burst(move.fault_class, RUNG_COLORS[move.to_rung]), SLIDE_MS * 0.8);
    }
    setToast(toastFor(move));
    later(() => setToast((current) => (current && current.key === move.key ? null : current)), TOAST_MS);
  };

  // Pill positions are measured by offset (ignores in-flight transforms) and compared across renders; a pill
  // that changes rung is remounted under a new parent, so the animation is applied by name after the fact.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;

    const signature = names.map((n) => `${n}:${rungOf(ladder, n)}`).join("|");
    const sourceChanged = lastSource.current !== replaying;
    lastSource.current = replaying;

    let fresh: LadderMove[] = [];
    const known = seen.current;
    if (known === null || sourceChanged) {
      seen.current = new Set(moves.map((m) => m.key));
    } else {
      fresh = moves.filter((m) => !known.has(m.key));
      for (const m of fresh) known.add(m.key);
    }

    const els = new Map<string, HTMLElement>();
    const next = new Map<string, { x: number; y: number }>();
    panel.querySelectorAll<HTMLElement>("[data-pill]").forEach((el) => {
      const name = el.dataset.pill!;
      els.set(name, el);
      next.set(name, { x: el.offsetLeft, y: el.offsetTop });
    });

    if (lastSignature.current !== null && signature !== lastSignature.current) {
      const heroes = new Set(fresh.map((m) => m.fault_class));
      els.forEach((el, name) => {
        const before = snapshot.current.get(name);
        const after = next.get(name);
        if (!before || !after) return;
        const dx = before.x - after.x;
        const dy = before.y - after.y;
        if (dx === 0 && dy === 0) return;
        const hero = heroes.has(name);
        if (hero) el.style.zIndex = "3";
        const animation = el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], {
          duration: hero ? SLIDE_MS : SETTLE_MS,
          easing: "cubic-bezier(0.22, 0.8, 0.24, 1)",
        });
        if (hero) {
          const clear = () => {
            el.style.zIndex = "";
          };
          animation.onfinish = clear;
          animation.oncancel = clear;
        }
      });
    }

    snapshot.current = next;
    lastSignature.current = signature;
    for (const move of fresh) celebrate(move, els.get(move.fault_class));
  });

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const id of pending) window.clearTimeout(id);
      pending.length = 0;
      cancelAnimationFrame(raf.current);
      raf.current = 0;
    };
  }, []);

  // A pill is remounted when its rung changes, so a move landing between press and release would swallow a
  // click; selecting on pointer down keeps the card reliable.
  const toggle = (name: string) => selectClass(useStream.getState().selectedClass === name ? null : name);

  const counts = RUNGS.map((rung) => names.filter((n) => rungOf(ladder, n) === rung).length);

  return (
    <div className="ladder panel interactive" ref={panelRef} data-replay={replaying || undefined}>
      <canvas className="burst" ref={canvasRef} />
      <div className="eyebrow ladder-title">Skill ladder</div>
      {RUNGS.map((rung, i) => (
        <section className="rung" key={rung} data-rung={rung} style={{ "--c": RUNG_COLORS[rung] } as React.CSSProperties}>
          <header className="rung-head">
            <h2>{rung.toUpperCase()}</h2>
            <span className="rung-count">{counts[i]}</span>
            {toast && toast.rung === rung && (
              <div className="toast" key={toast.key} data-dir={toast.direction}>
                <span className="toast-top">
                  {prettyClass(toast.fault_class)} {toast.direction === "promote" ? "promoted" : "demoted"}
                </span>
                <span className="toast-evidence">{toast.text}</span>
              </div>
            )}
          </header>
          <div className="pills">
            {names
              .filter((n) => rungOf(ladder, n) === rung)
              .map((name) => (
                <button
                  type="button"
                  key={name}
                  className="pill"
                  data-pill={name}
                  data-held={HELDOUT.has(name) || undefined}
                  data-selected={selected === name || undefined}
                  onPointerDown={(event) => {
                    if (event.button === 0) toggle(name);
                  }}
                  onClick={(event) => {
                    if (event.detail === 0) toggle(name);
                  }}
                >
                  {prettyClass(name)}
                </button>
              ))}
          </div>
        </section>
      ))}
    </div>
  );
}
