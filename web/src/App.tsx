import { useEffect } from "react";

import { connect, useStream } from "./store";

export default function App() {
  const { connected, counts, recent } = useStream();

  useEffect(() => connect(), []);

  return (
    <main style={{ fontFamily: "monospace", padding: 16 }}>
      <h1>understudy</h1>
      <p>{connected ? "connected" : "disconnected"}</p>
      <ul>
        {Object.entries(counts).map(([type, count]) => (
          <li key={type}>
            {type}: {count}
          </li>
        ))}
      </ul>
      <ol>
        {recent.map((event, i) => (
          <li key={`${event.ts}-${i}`}>
            {event.type} {JSON.stringify(event.data)}
          </li>
        ))}
      </ol>
    </main>
  );
}
