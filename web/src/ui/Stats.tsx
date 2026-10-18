import { useStream } from "../store";
import CountUp from "./CountUp";
import { useReplay } from "./replayStore";

const whole = (v: number) => Math.round(v).toLocaleString("en-US");
const dollars = (v: number) => (v > 0 ? `$${v.toFixed(4)}` : "—");
const percent = (v: number) => `${(v * 100).toFixed(1)}%`;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

interface Tile {
  label: string;
  sub: string;
  color: string;
  value: number | null;
  format: (v: number) => string;
}

export default function Stats() {
  const ledger = useStream((s) => s.ledger);
  const replaying = useReplay((s) => s.active);
  const replay = useReplay((s) => s.stats);

  const incidents = replaying ? replay.incidents : finite(ledger?.incidents_total);
  const rejected = replaying ? replay.rejected : finite(ledger?.rejected_claims);
  const cost = replaying ? replay.costPerIncident : finite(ledger?.cost_per_incident_usd);
  const cache = replaying ? replay.cacheHit : finite(ledger?.cache_hit_rate);

  const tiles: Tile[] = [
    { label: "Incidents handled", sub: "verified by the referee", color: "var(--text)", value: incidents, format: whole },
    { label: "False fixes caught", sub: "claims the referee rejected", color: "var(--accent)", value: rejected, format: whole },
    { label: "Cost per incident", sub: "mean inference spend", color: "var(--verified)", value: cost, format: dollars },
    { label: "Cache hit rate", sub: "fixes served from skills", color: "var(--healthy)", value: cache, format: percent },
  ];

  return (
    <div className="stats panel" data-replay={replaying || undefined} role="group" aria-label="Fleet ledger">
      {tiles.map((tile) => (
        <div className="stat" key={tile.label} style={{ "--c": tile.color } as React.CSSProperties}>
          <div className="label">{tile.label}</div>
          <div className="num">
            <CountUp value={tile.value} format={tile.format} />
          </div>
          <div className="sub">{replaying ? "replay" : tile.sub}</div>
        </div>
      ))}
    </div>
  );
}
