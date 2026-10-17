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
| B | "I'm back": the spoken briefing, a slow camera pass across the hall, then the replay. Pressing it again, or Escape, skips the rest |
| / | focus the command box |
| R | replay the whole run as a 20 second time-lapse |
| F | fullscreen |
| M | mute the voice (captions stay) |
| hold Space | push to talk |

## Voice

Narration is spoken with `speechSynthesis` (Daniel, then Samantha, then Google UK English Male, rate 1.05) and captioned as each line starts, so the caption and the voice stay together. A high priority line cuts off a low one and goes first; low lines that wait too long are dropped. With the voice muted, blocked by the browser until the page has been clicked, or missing, the caption appears at once and the page paces itself by reading time.

Push to talk asks `GET /api/voice/status` which path to use. When the backend can transcribe audio the page records with `MediaRecorder`, converts the clip to 16 kHz mono WAV unless the status lists its container, and posts it to `/api/voice`. Otherwise it uses the browser's speech recognition. What was heard is routed on the page: a phrase matching `i'm back`, `what happened` or `welcome back` runs the briefing, anything else goes to `POST /api/chaos`. The typed command box takes the same phrases. `window.understudyVoice` exposes `start`, `stop`, `command(text)`, `imBack()` and `speak(text, priority)` for scripts and tests.

## Layout

- `src/store.ts`: zustand store. Events are applied idempotently (duplicates are dropped) and also published on `src/bus.ts` for one-shot reactions. A page opened mid-incident replays the last 150 seconds of `/api/history` to seat the agents already at work. The backend reports a node as down and repairing in the same instant, so the store keeps the red state on screen for 3.5 seconds before showing the next one.
- `src/ws.ts`: WebSocket client with exponential backoff.
- `src/scene/`: the 3D scene. Animation runs in `useFrame` against refs, not React state. Speech bubbles are placed in screen space by `BubbleLayout.tsx`: at most four at a time, kept clear of each other, the token counter and the overlay panels.
- `src/ui/`: HTML overlays, controls and the replay.
- `src/voice/`: the speech queue (`speech.ts`), push to talk (`listen.ts`), WAV conversion (`wav.ts`) and the command and briefing flow (`flow.ts`). The camera pass is `src/scene/sweep.ts`, read by `CameraRig.tsx`.
- `scripts/voice_check.py`: drives the typed box, push to talk with a fake microphone and the briefing flow in Chromium, logging the requests the page makes and every line it speaks.
- `scripts/shoot.py`: opens the dev server at 1920x1080 with Playwright and saves screenshots to `web/.screens/` (not tracked). It renders on the GPU by default; pass `--software` on a machine without one.

## Replay

Replay does not use the event stream, which `/api/history` caps at the last 2000 events. It rebuilds the four numbers and the ladder from the full-run series in the same response: cumulative incidents, cost per incident and the ladder log. Rejected claims are counted from the events in the window; the ones before it are taken from the ledger total and appear when the window starts, because their times are not in the response. The cache hit rate is not a series, so it holds the current value.
