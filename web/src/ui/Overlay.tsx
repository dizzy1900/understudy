import { useStream } from "../store";

export default function Overlay() {
  const connected = useStream((s) => s.connected);
  return (
    <div className="overlay-layer">
      <div style={{ position: "absolute", left: 24, top: 20, fontSize: 28, fontWeight: 700 }}>
        UNDERSTUDY {connected ? "" : "(offline)"}
      </div>
    </div>
  );
}
