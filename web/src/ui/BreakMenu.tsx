import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import { useStream } from "../store";
import { HELDOUT_CLASSES, STATUS_COLORS, TRAINING_CLASSES, prettyClass } from "../theme";
import { say, shortError } from "./actions";

const CLASSES: string[] = [...TRAINING_CLASSES, ...HELDOUT_CLASSES];
const HELDOUT = new Set<string>(HELDOUT_CLASSES);

function liveOrder(): string[] {
  const nodes = Object.values(useStream.getState().nodes).filter((n) => n.zone === "live");
  nodes.sort((a, b) => {
    const rank = (s: string) => (s === "healthy" ? 0 : 1);
    return rank(a.status) - rank(b.status) || a.id.localeCompare(b.id, "en", { numeric: true });
  });
  return nodes.map((n) => n.id);
}

export default function BreakMenu() {
  const nodes = useStream((s) => s.nodes);
  const [open, setOpen] = useState(false);
  // Order is fixed when the menu opens so chips do not shuffle as node status changes underneath the cursor.
  const [order, setOrder] = useState<string[]>([]);
  const [node, setNode] = useState<string | null>(null);
  const [cls, setCls] = useState<string>(CLASSES[0]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: PointerEvent) => {
      if (wrap.current && !wrap.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    const ids = liveOrder();
    setOrder(ids);
    setNode(ids[0] ?? null);
    setError(null);
    setOpen(true);
  };

  const inject = async () => {
    if (!node || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.inject(node, cls);
      setOpen(false);
      say(`Injected ${prettyClass(cls)} on ${node}`, "low");
    } catch (e) {
      setError(shortError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="break-wrap" ref={wrap}>
      <button type="button" className="btn" onClick={toggle} aria-expanded={open}>
        Break...
      </button>
      {open && (
        <div className="break-pop panel interactive" role="dialog" aria-label="Inject a fault">
          <div className="eyebrow">Node</div>
          {order.length === 0 ? (
            <p className="muted-line">No live nodes reported yet.</p>
          ) : (
            <div className="node-grid">
              {order.map((id) => {
                const status = nodes[id]?.status ?? "healthy";
                return (
                  <button
                    type="button"
                    key={id}
                    className="opt node-opt"
                    data-on={id === node || undefined}
                    onClick={() => setNode(id)}
                  >
                    <i style={{ background: STATUS_COLORS[status] }} />
                    {id}
                  </button>
                );
              })}
            </div>
          )}
          <div className="eyebrow">Fault class</div>
          <div className="class-grid">
            {CLASSES.map((name) => (
              <button
                type="button"
                key={name}
                className="opt"
                data-on={name === cls || undefined}
                onClick={() => setCls(name)}
              >
                {prettyClass(name)}
                {HELDOUT.has(name) && <em>held out</em>}
              </button>
            ))}
          </div>
          {error && <p className="form-error">{error}</p>}
          <button type="button" className="btn btn-go" disabled={!node || busy} onClick={inject}>
            {busy ? "Injecting" : `Inject ${prettyClass(cls)}${node ? ` on ${node}` : ""}`}
          </button>
        </div>
      )}
    </div>
  );
}
