import { useEffect, useState } from "react";

import { CAPTION_TTL_MS, useStream } from "../store";

const FADE_OUT_MS = 600;
const VISIBLE = 2;

export default function Captions() {
  const captions = useStream((s) => s.captions);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (captions.length === 0) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 400);
    return () => window.clearInterval(id);
  }, [captions]);

  const visible = captions.filter((c) => now - c.at < CAPTION_TTL_MS + FADE_OUT_MS).slice(-VISIBLE);

  return (
    <div className="captions" aria-live="polite">
      {visible.map((caption) => (
        <p
          key={caption.key}
          className="caption"
          data-priority={caption.priority}
          style={{ "--ttl": `${CAPTION_TTL_MS}ms`, "--fade": `${FADE_OUT_MS}ms` } as React.CSSProperties}
        >
          <span>{caption.text}</span>
        </p>
      ))}
    </div>
  );
}
