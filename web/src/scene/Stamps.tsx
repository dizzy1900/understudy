import { Html } from "@react-three/drei";
import { useMemo } from "react";
import type { CSSProperties } from "react";

import { isProving, nodeIdPosition, PROVING_RACK_SCALE } from "../layout";
import { STAMP_TTL_MS, useStream } from "../store";
import type { Stamp as StampData } from "../store";
import { COLORS } from "../theme";

function Stamp({ stamp }: { stamp: StampData }) {
  const [x, , z] = nodeIdPosition(stamp.node_id);
  const scale = isProving(stamp.node_id) ? PROVING_RACK_SCALE : 1;
  const ok = stamp.verdict === "verified";
  const color = ok ? COLORS.verified : COLORS.incident;
  const delay = -Math.max(0, Date.now() - stamp.at) / 1000;
  const style = { "--sc": color, "--delay": `${delay}s` } as CSSProperties;
  const detail = ok ? (stamp.quality !== null ? `Q ${stamp.quality}/5` : "") : stamp.failing;
  return (
    <Html position={[x, 1.75 * scale, z + 0.8 * scale]} pointerEvents="none" zIndexRange={[100, 30]} style={{ pointerEvents: "none" }}>
      <div className={`sc-stamp ${ok ? "verified" : "rejected"}`} style={style}>
        <div className="anchor">
          <div className="face">
            <div className="v">{ok ? "VERIFIED" : "REJECTED"}</div>
            {detail && <div className="d">{detail}</div>}
          </div>
        </div>
      </div>
    </Html>
  );
}

export default function Stamps() {
  const keys = useStream((s) =>
    Object.values(s.stamps)
      .map((st) => st.key)
      .join(","),
  );
  const stamps = useMemo(() => {
    const now = Date.now();
    return Object.values(useStream.getState().stamps).filter((s) => now - s.at < STAMP_TTL_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys]);
  return (
    <>
      {stamps.map((s) => (
        <Stamp key={s.key} stamp={s} />
      ))}
    </>
  );
}
