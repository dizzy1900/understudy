import { useStream } from "../store";
import CountUp from "./CountUp";
import { useReplay } from "./replayStore";

const whole = (v: number) => Math.round(v).toLocaleString("en-US");
const dollars = (v: number) => `$${v.toFixed(4)}`;
const percent = (v: number) => `${(v * 100).toFixed(1)}%`;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export default function Stats() {
  const ledger = useStream((s) => s.ledger);
  const replaying = useReplay((s) => s.active);
  const replay = useReplay((s) => s.stats);

  const incidents = replaying ? replay.incidents : finite(ledger?.incidents_total);
  const rejected = replaying ? replay.rejected : finite(ledger?.rejected_claims);
  const cost = replaying ? replay.costPerIncident : finite(ledger?.cost_per_incident_usd);
  const cache = replaying ? replay.cacheHit : finite(ledger?.cache_hit_rate);

  return (
    <div className="stats panel" data-replay={replaying || undefined}>
      <div className="stat" style={{ "--c": "var(--text)" } as React.CSSProperties}>
        <div className="num">
          <CountUp value={incidents} format={whole} />
        </div>
        <div className="label">Incidents handled</div>
      </div>
      <div className="stat" style={{ "--c": "var(--accent)" } as React.CSSProperties}>
        <div className="num">
          <CountUp value={rejected} format={whole} />
        </div>
        <div className="label">False fixes caught</div>
      </div>
      <div className="stat" style={{ "--c": "var(--verified)" } as React.CSSProperties}>
        <div className="num">
          <CountUp value={cost} format={dollars} />
        </div>
        <div className="label">Cost per incident</div>
      </div>
      <div className="stat" style={{ "--c": "var(--healthy)" } as React.CSSProperties}>
        <div className="num">
          <CountUp value={cache} format={percent} />
        </div>
        <div className="label">Cache hit rate</div>
      </div>
    </div>
  );
}
