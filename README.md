# understudy

An autonomous on-call team for an inference fleet, built on Nemotron models served by Crusoe Managed Inference.

Work in progress.

## Learning and promotion

- After a verified fix graded 4 or higher, the librarian (SUPER) rewrites that class's skill card from the last three verified trajectories and the held-out results. Cards are capped at 900 characters, versioned in the `skills` table, and rendered without version numbers so an update that changes nothing leaves the cached prompt prefix untouched. The evidence line on each card is computed from the database.
- A class sitting at the lightning rung with five verified lightning fixes gets a compiled fix script (`data/scripts/<class>.py`). The script is checked statically (only `re`, `json` and `yaml` imports, no dynamic code), then runs in a separate `python -I` process with an empty environment, a 30 s limit and at most 40 tool calls, all through the same jailed tool layer as a model responder. The script rung is reachable only through promotion.
- The promoter runs every 6 minutes and on `POST /api/promote/run` (optionally `?fault_class=...`). It tries the next cheaper rung on 5 held-out instances (seeds from 100000) on the proving nodes pg-01 to pg-04, and promotes at 4 of 5 passes when the mean cost is below the live mean at the current rung. It demotes a class when fewer than 3 of its last 5 live incidents at the rung were fixed without escalation. A trial that fails because the model API failed or stalled is run again and not counted.
- Every 20 minutes it also runs 3 instances each of the held-out classes (`log_permission`, `upstream_storm`) at the lightning rung and stores the results, failures included.
- Proving incidents are stored with `zone = 'proving'` and are excluded from the ledger, the history and the report.
- `make report` writes a markdown report to stdout and `data/report.json`; `GET /api/history` returns the same time series.

## Deploying on Vercel

`vercel.json` defines one project with two services:

- `web`: the Vite app in `web/`, served at `/`.
- `api`: a read-only FastAPI app (`understudy/vercel_app.py`), served at `/api/*`, `/healthz` and `/ws`.

The API service serves `/api/ledger`, `/api/ladder`, `/api/skills` and `/api/history` from a SQLite file. It opens the file read-only, so it needs a snapshot of `data/understudy.db` bundled with the deploy or a path in `UNDERSTUDY_DB`. `data/` is listed in `.vercelignore`, so by default no snapshot is included and the endpoints return empty payloads. The POST endpoints (`/api/chaos`, `/api/inject`, `/api/storm`, `/api/away`, `/api/back`, `/api/promote/run`, `/api/demo/reset`) are not implemented there.

Not deployed: the orchestrator, the fleet nodes, the traffic generator and the agents. They need long-lived processes, subprocesses and a writable database, so they run as a separate process (`make autopilot`) on a host you control. Vercel functions do not support WebSockets, so `/ws` returns 501 there. For the live stream, build the web app with `VITE_WS_URL=wss://<backend-host>/ws` pointing at that host; the backend allows only the local dev origins for CORS, so add the deployed origin in `understudy/server.py` if the browser calls it directly.

Environment variables:

- `UNDERSTUDY_DB`: path to the SQLite file the `api` service reads (default `data/understudy.db`).
- `VITE_WS_URL`: WebSocket URL baked into the web build (default: same origin, `ws:` or `wss:` chosen from the page protocol).
- `VITE_BACKEND_URL`: backend the Vite dev server proxies `/api`, `/healthz` and `/ws` to (default `http://127.0.0.1:8000`).

Neither service calls the other from server-side code, so no service bindings are configured. `requirements-dev.txt` holds pytest and playwright, which the function build does not need.

Locally, `make web` runs the dev server on :5173 and proxies to the backend on :8000.
