import { useEffect, useState } from "react";

import { api } from "../api";
import { useStream } from "../store";
import { RUNG_COLORS, prettyClass, rungColor } from "../theme";
import type { LadderEvidence, LadderLogEntry, SkillCard } from "../types";
import { say, shortError } from "./actions";

function toList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean);
  if (typeof value === "string") {
    return value
      .split("\n")
      .map((line) => line.replace(/^\s*(?:[-*]|\d+[.)])\s*/, "").trim())
      .filter(Boolean);
  }
  return [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function evidenceOf(entry: LadderLogEntry): LadderEvidence | null {
  const nested = entry.evidence;
  const passed = nested?.passed ?? entry.passed;
  const total = nested?.total ?? entry.total;
  const cost = nested?.mean_cost_usd ?? entry.mean_cost_usd;
  if (typeof passed !== "number" || typeof total !== "number") return null;
  return { passed, total, mean_cost_usd: typeof cost === "number" ? cost : 0 };
}

interface LatestMove {
  direction: "promote" | "demote";
  to: string;
  evidence: LadderEvidence;
}

export default function SkillCardPanel() {
  const selected = useStream((s) => s.selectedClass);
  if (!selected) return null;
  return <Card key={selected} name={selected} />;
}

function Card({ name }: { name: string }) {
  const selectClass = useStream((s) => s.selectClass);
  const rung = useStream((s) => s.ladder[name]);
  const note = useStream((s) => s.skillNotes[name]);
  const liveMove = useStream((s) => {
    for (let i = s.ladderMoves.length - 1; i >= 0; i--) if (s.ladderMoves[i].fault_class === name) return s.ladderMoves[i];
    return null;
  });
  const [card, setCard] = useState<SkillCard | null | undefined>(undefined);
  const [logged, setLogged] = useState<LatestMove | null>(null);

  const noteVersion = note?.version;
  useEffect(() => {
    let cancelled = false;
    api
      .skills()
      .then((cards) => {
        if (cancelled) return;
        const list = Array.isArray(cards) ? cards : [];
        setCard(list.find((c) => c && c.fault_class === name) ?? null);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setCard(null);
        say(`Skill card unavailable: ${shortError(error)}`, "low");
      });
    api
      .ladder()
      .then((payload) => {
        if (cancelled) return;
        const events = Array.isArray(payload?.events) ? payload.events : [];
        for (let i = events.length - 1; i >= 0; i--) {
          const entry = events[i];
          if (entry?.fault_class !== name) continue;
          const evidence = evidenceOf(entry);
          if (evidence) setLogged({ direction: entry.direction, to: entry.to_rung, evidence });
          return;
        }
      })
      .catch(() => {
        /* the live moves and the card still render without the ladder log */
      });
    return () => {
      cancelled = true;
    };
  }, [name, noteVersion]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") selectClass(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectClass]);

  const latest: LatestMove | null = liveMove
    ? {
        direction: liveMove.direction,
        to: liveMove.to_rung,
        evidence: { passed: liveMove.passed, total: liveMove.total, mean_cost_usd: liveMove.mean_cost_usd },
      }
    : logged;

  const color = rungColor(rung);
  const steps = toList(card?.fix_steps);
  const pitfalls = toList(card?.pitfalls);
  const evidenceLine = text(card?.evidence) || text(card?.summary);

  return (
    <>
      <div className="card-backdrop interactive" onClick={() => selectClass(null)} />
      <aside className="skill-card panel interactive" style={{ "--c": color } as React.CSSProperties} role="dialog">
        <header className="skill-head">
          <div>
            <div className="eyebrow">Skill card</div>
            <h2>{prettyClass(name)}</h2>
          </div>
          <div className="skill-meta">
            {rung && <span className="rung-chip">{rung}</span>}
            {typeof card?.version === "number" && <span className="version">v{card.version}</span>}
            <button type="button" className="icon-btn" onClick={() => selectClass(null)} aria-label="Close skill card">
              <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
                <path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        </header>

        <div className="skill-body">
          {card === undefined && <p className="muted-line">Loading.</p>}
          {card === null && <p className="muted-line">No skill card for this class yet.</p>}
          {card && (
            <>
              {text(card.signature) && (
                <section>
                  <h3>Signature</h3>
                  <p className="mono">{text(card.signature)}</p>
                </section>
              )}
              {text(card.diagnosis) && (
                <section>
                  <h3>Diagnosis</h3>
                  <p>{text(card.diagnosis)}</p>
                </section>
              )}
              {steps.length > 0 && (
                <section>
                  <h3>Fix steps</h3>
                  <ol className="mono steps">
                    {steps.map((step, i) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ol>
                </section>
              )}
              {pitfalls.length > 0 && (
                <section>
                  <h3>Pitfalls</h3>
                  <ul className="pitfalls">
                    {pitfalls.map((item, i) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                </section>
              )}
              {evidenceLine && (
                <section>
                  <h3>Evidence</h3>
                  <p>{evidenceLine}</p>
                </section>
              )}
            </>
          )}

          {(latest || note) && (
            <section className="skill-log">
              <h3>Latest</h3>
              {latest && (
                <p>
                  <span style={{ color: RUNG_COLORS[latest.to as keyof typeof RUNG_COLORS] ?? color }}>
                    {latest.direction === "promote" ? "Promoted" : "Demoted"} to {latest.to}
                  </span>
                  : {latest.evidence.passed}/{latest.evidence.total} passed, mean $
                  {latest.evidence.mean_cost_usd.toFixed(4)}
                </p>
              )}
              {note && (
                <p>
                  Card v{note.version}: {note.summary}
                </p>
              )}
            </section>
          )}
        </div>
      </aside>
    </>
  );
}
