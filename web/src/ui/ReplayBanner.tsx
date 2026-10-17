import { useEffect, useRef } from "react";

import { replayClock, useReplay } from "./replayStore";

function clock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

export default function ReplayBanner() {
  const active = useReplay((s) => s.active);
  const clockRef = useRef<HTMLDivElement>(null);
  const wallRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active) return;
    let raf = 0;
    const tick = () => {
      const { first, ts, progress } = replayClock;
      if (clockRef.current) clockRef.current.textContent = `T+ ${clock(ts - first)}`;
      if (wallRef.current) {
        wallRef.current.textContent = new Date(ts * 1000).toLocaleString("en-GB", {
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        });
      }
      if (barRef.current) barRef.current.style.transform = `scaleX(${progress})`;
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [active]);

  if (!active) return null;

  return (
    <>
      <div className="replay-frame" />
      <div className="replay-banner panel">
        <div className="replay-tag">
          <i />
          REPLAY
        </div>
        <div className="replay-clock" ref={clockRef} />
        <div className="replay-wall" ref={wallRef} />
        <div className="replay-bar">
          <div ref={barRef} />
        </div>
      </div>
    </>
  );
}
