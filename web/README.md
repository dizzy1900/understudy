# web

The control room: a live 3D view of the data hall with the on-call agents as visible actors, plus overlays for the ledger, the promotion ladder, captions and controls. Vite, React, TypeScript, three, @react-three/fiber, drei, postprocessing and zustand.

## Run

```
make web          # dev server on :5173
make web-build    # type-check and build to web/dist
make mock-events  # synthetic event stream on :8765, for working on the UI without the fleet
```

The page connects to `VITE_API_BASE` (REST) and the same host's `/ws` (WebSocket). The default is `http://localhost:8000` in dev and the page's own origin in a production build. To work against the mock server:

```
VITE_API_BASE=http://localhost:8765 npm run dev
```

`VITE_WS_URL` overrides the WebSocket URL when the stream lives on a different host than the REST API.

## Demo mode

The hosted page cannot reach a live fleet (Vercel cannot hold the orchestrator or a WebSocket), so the control room falls back to an in-browser simulated incident stream. Numbers in demo mode are synthetic and the page says so.

- Demo mode starts automatically when no live backend answers within about 4 seconds.
- `?demo=1` forces demo mode even when a backend is reachable.
- `?live=1` disables the fallback and waits for the live backend.

## Keys

| key | action |
|---|---|
| S | send a storm (2 random rows) |
| B | "I'm back" briefing |
| R | replay the whole run as a 20 second time-lapse |
| F | fullscreen |
| M | mute |
| hold Space | push to talk (calls `window.understudyVoice` when it is defined) |

## Layout

- `src/store.ts`: zustand store. Events are applied idempotently (duplicates are dropped) and also published on `src/bus.ts` for one-shot reactions. A page opened mid-incident replays the last 150 seconds of `/api/history` to seat the agents already at work. The backend reports a node as down and repairing in the same instant, so the store keeps the red state on screen for 3.5 seconds before showing the next one.
- `src/ws.ts`: WebSocket client with exponential backoff.
- `src/scene/`: the 3D scene. Animation runs in `useFrame` against refs, not React state. Speech bubbles are placed in screen space by `BubbleLayout.tsx`: at most four at a time, kept clear of each other, the token counter and the overlay panels.
- `src/ui/`: HTML overlays, controls and the replay.
- `scripts/shoot.py`: opens the dev server at 1920x1080 with Playwright and saves screenshots to `web/.screens/` (not tracked). It renders on the GPU by default; pass `--software` on a machine without one.

## Replay

Replay does not use the event stream, which `/api/history` caps at the last 2000 events. It rebuilds the four numbers and the ladder from the full-run series in the same response: cumulative incidents, cost per incident and the ladder log. Rejected claims are counted from the events in the window; the ones before it are taken from the ledger total and appear when the window starts, because their times are not in the response. The cache hit rate is not a series, so it holds the current value.
