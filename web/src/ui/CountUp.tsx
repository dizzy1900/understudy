import { useLayoutEffect, useRef } from "react";

interface Props {
  value: number | null;
  format: (value: number) => string;
  duration?: number;
}

/** Tweens by writing text straight to the node, so a ledger arriving every few hundred ms never re-renders React. */
export default function CountUp({ value, format, duration = 700 }: Props) {
  const ref = useRef<HTMLSpanElement>(null);
  const shown = useRef<number | null>(null);
  const formatRef = useRef(format);
  formatRef.current = format;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (value === null || !Number.isFinite(value)) {
      shown.current = null;
      el.textContent = "\u2014";
      return;
    }
    const from = shown.current ?? 0;
    const started = performance.now();
    let raf = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const current = from + (value - from) * eased;
      shown.current = current;
      el.textContent = formatRef.current(current);
      if (t < 1) raf = requestAnimationFrame(step);
    };
    step(started);
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);

  return <span ref={ref} />;
}
