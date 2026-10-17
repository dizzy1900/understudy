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

## Keys

| key | action |
|---|---|
| S | send a storm (2 random rows) |
| B | "I'm back" briefing |
| R | replay the run as a 20 second time-lapse |
| F | fullscreen |
| M | mute |
| hold Space | push to talk (calls `window.understudyVoice` when it is defined) |

## Layout

- `src/store.ts`: zustand store. Events are applied idempotently (duplicates are dropped) and also published on `src/bus.ts` for one-shot reactions.
- `src/ws.ts`: WebSocket client with exponential backoff.
- `src/scene/`: the 3D scene. Animation runs in `useFrame` against refs, not React state.
- `src/ui/`: HTML overlays, controls and the replay.
- `scripts/shoot.py`: opens the dev server at 1920x1080 with Playwright and saves screenshots to `web/.screens/` (not tracked).
