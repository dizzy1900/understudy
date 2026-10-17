"""One inference node: a FastAPI proxy to Crusoe configured by fleet/nodes/<id>/config.yaml.

Run as: python -m understudy.fleet.node_app --node-dir fleet/nodes/<id>

Boot order: parse config (exit 2), check the data/ quota, open logs/node.log (exit 3), bind the port (exit 4).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import json
import logging
import os
import random
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask

from understudy.fleet.nodeconfig import ConfigError, NodeConfig, NodePaths, load_node_config

EXIT_RECYCLED = 1
EXIT_CONFIG = 2
EXIT_LOG = 3
EXIT_BIND = 4

HOST = "127.0.0.1"
STORM_COOLDOWN_S = 20
CHECKPOINT_BYTES = 200 * 1024
CHECKPOINT_INTERVAL_S = 60
CHECKPOINT_KEEP = 10
QUOTA_CACHE_S = 1.0
MB = 1024 * 1024


class NodeLog:
    """Append-only service log. Falls back to stderr only before the file is open."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: Any = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", buffering=1)

    def try_open(self) -> bool:
        try:
            self.open()
        except OSError:
            return False
        return True

    def write(self, level: str, message: str) -> None:
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int((now % 1) * 1000):03d}Z"
        line = f"{stamp} {level:<5} {message}"
        if self._fh is not None:
            self._fh.write(line + "\n")
        else:
            print(line, file=sys.stderr, flush=True)


class _LogHandler(logging.Handler):
    def __init__(self, log: NodeLog):
        super().__init__()
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        self.log.write(record.levelname, f"uvicorn: {record.getMessage()}")


def write_exit_info(paths: NodePaths, code: int, reason: str) -> None:
    with contextlib.suppress(OSError):
        paths.exit_info.write_text(json.dumps({"pid": os.getpid(), "code": code, "reason": reason, "ts": time.time()}))


def die(paths: NodePaths, log: NodeLog, code: int, message: str) -> NoReturn:
    log.write("ERROR", message)
    if log._fh is not None:
        print(message, file=sys.stderr, flush=True)
    write_exit_info(paths, code, message)
    raise SystemExit(code)


def data_bytes(data_dir: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, name))
    return total


class Quota:
    def __init__(self, data_dir: Path, quota_mb: int, log: NodeLog):
        self.data_dir = data_dir
        self.limit = quota_mb * MB
        self.log = log
        self._checked = 0.0
        self.used = 0
        self.over = False
        self._announced: bool | None = None

    def check(self, force: bool = False, quiet: bool = False) -> bool:
        now = time.monotonic()
        if force or now - self._checked >= QUOTA_CACHE_S:
            self._checked = now
            self.used = data_bytes(self.data_dir)
            self.over = self.used > self.limit
            if not quiet and self.over != self._announced:
                if self.over:
                    self.log.write(
                        "ERROR",
                        f"data dir over quota: {self.used / MB:.1f} MB used of {self.limit / MB:.0f} MB, "
                        "rejecting requests with 507",
                    )
                elif self._announced:
                    self.log.write("INFO", f"data dir back under quota: {self.used / MB:.1f} MB used")
                self._announced = self.over
        return self.over


def _crusoe_error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "error_id": uuid.uuid4().hex}


def _upstream_error_text(status: int, body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:160]
    if isinstance(data, dict):
        if isinstance(data.get("errors"), list) and data["errors"]:
            return str(data["errors"][0])[:200]
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
        if data.get("message"):
            return str(data["message"])[:200]
    return text.strip()[:160]


class NodeRuntime:
    def __init__(self, cfg: NodeConfig, paths: NodePaths, log: NodeLog, quota: Quota):
        self.cfg = cfg
        self.paths = paths
        self.log = log
        self.quota = quota
        self.started_at = time.time()
        self.seq = 0
        self.handled = 0
        self.in_flight = 0
        self.cooldown_until = 0.0
        self.tokens_served = 0
        self.by_status: dict[int, int] = {}
        self.last_checkpoint: str | None = None
        self.sem = asyncio.Semaphore(max(1, cfg.worker_concurrency))
        self.client: httpx.AsyncClient | None = None

    def recycle_due(self) -> bool:
        limit = self.cfg.worker_recycle_after
        return limit > 0 and self.handled >= limit

    async def recycle(self) -> None:
        self.log.write("ERROR", f"worker recycled after {self.handled} requests")
        write_exit_info(self.paths, EXIT_RECYCLED, "worker recycled")
        os._exit(EXIT_RECYCLED)

    def write_checkpoint(self) -> None:
        if self.quota.check(force=True):
            self.log.write("WARN", "checkpoint skipped: data dir over quota")
            return
        name = f"ckpt-{int(time.time() * 1000)}.bin"
        try:
            (self.paths.data / name).write_bytes(os.urandom(CHECKPOINT_BYTES))
        except OSError as exc:
            self.log.write("ERROR", f"checkpoint write failed: {exc}")
            return
        self.last_checkpoint = name
        self.log.write("INFO", f"checkpoint written {name} size={CHECKPOINT_BYTES}")
        old = sorted(self.paths.data.glob("ckpt-*.bin"))[:-CHECKPOINT_KEEP]
        for path in old:
            with contextlib.suppress(OSError):
                path.unlink()

    async def checkpoint_loop(self) -> None:
        while True:
            self.write_checkpoint()
            await asyncio.sleep(CHECKPOINT_INTERVAL_S)

    def upstream_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.cfg.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def build_payload(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in body.items() if k != "stream"}
        payload["model"] = self.cfg.model
        payload["max_tokens"] = self.cfg.max_tokens
        payload.update(self.cfg.extra_body)
        return payload


def create_app(runtime: NodeRuntime) -> FastAPI:
    cfg = runtime.cfg
    log = runtime.log

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.client = httpx.AsyncClient(timeout=httpx.Timeout(cfg.upstream_timeout_s, connect=10.0))
        task = asyncio.create_task(runtime.checkpoint_loop())
        try:
            yield
        finally:
            task.cancel()
            await runtime.client.aclose()
            log.write("INFO", "shutting down")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def respond(
        rid: str,
        started: float,
        status: int,
        content: bytes,
        media_type: str = "application/json",
        headers: dict[str, str] | None = None,
        *,
        error: str = "",
        usage: dict[str, Any] | None = None,
        finish: str | None = None,
    ) -> Response:
        latency_ms = int((time.monotonic() - started) * 1000)
        runtime.handled += 1
        runtime.by_status[status] = runtime.by_status.get(status, 0) + 1
        fields = [f"req id={rid} POST /v1/chat/completions", f"status={status}", f"latency_ms={latency_ms}"]
        if usage:
            fields += [
                f"prompt_tokens={usage.get('prompt_tokens', 0)}",
                f"completion_tokens={usage.get('completion_tokens', 0)}",
                f"total_tokens={usage.get('total_tokens', 0)}",
            ]
        if finish:
            fields.append(f"finish={finish}")
        if error:
            fields.append(f'upstream_error="{error}"')
        level = "INFO" if status < 400 else "WARN" if status < 500 else "ERROR"
        log.write(level, " ".join(fields))
        background = BackgroundTask(runtime.recycle) if runtime.recycle_due() else None
        return Response(content=content, status_code=status, media_type=media_type, headers=headers, background=background)

    def local_error(rid: str, started: float, status: int, body: dict[str, Any], message: str, headers=None) -> Response:
        return respond(rid, started, status, json.dumps(body).encode(), headers=headers, error=message)

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        runtime.seq += 1
        rid = f"{runtime.seq:06d}"
        started = time.monotonic()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except ValueError as exc:
            return local_error(rid, started, 400, _crusoe_error("invalid_request", f"invalid JSON body: {exc}"), "invalid JSON body")

        if runtime.quota.check():
            message = f"quota exceeded: data/ uses {runtime.quota.used / MB:.1f} MB of {runtime.quota.limit / MB:.0f} MB"
            return local_error(
                rid, started, 507, {"error": {"message": "quota exceeded", "type": "insufficient_storage", "code": 507}}, message
            )

        retry_headers = {"retry-after": str(STORM_COOLDOWN_S)}

        def rate_limited(message: str) -> Response:
            return local_error(rid, started, 429, _crusoe_error("rate_limit_exceeded", message), message, retry_headers)

        if time.monotonic() < runtime.cooldown_until:
            remaining = runtime.cooldown_until - time.monotonic()
            return rate_limited(
                f"Rate limit exceeded: concurrent request limit of {cfg.upstream_limit} was exceeded. "
                f"Retry after {remaining:.0f}s."
            )

        async with runtime.sem:
            if runtime.in_flight >= cfg.upstream_limit:
                if cfg.retry_backoff:
                    for attempt in range(5):
                        await asyncio.sleep(0.2 * 2**attempt * (0.5 + random.random()))
                        if runtime.in_flight < cfg.upstream_limit:
                            break
                    else:
                        return rate_limited("Rate limit exceeded: no upstream slot free after 5 retries")
                else:
                    runtime.cooldown_until = time.monotonic() + STORM_COOLDOWN_S
                    log.write(
                        "WARN",
                        f"upstream concurrent request limit exceeded in_flight={runtime.in_flight} "
                        f"limit={cfg.upstream_limit} worker_concurrency={cfg.worker_concurrency} retry_backoff=off",
                    )
                    return rate_limited(
                        f"Rate limit exceeded: concurrent request limit of {cfg.upstream_limit} reached "
                        f"({runtime.in_flight} in flight). Retry after {STORM_COOLDOWN_S}s."
                    )

            runtime.in_flight += 1
            try:
                url = cfg.upstream_base_url.rstrip("/") + "/chat/completions"
                assert runtime.client is not None
                upstream = await runtime.client.post(url, json=runtime.build_payload(body), headers=runtime.upstream_headers())
            except httpx.TimeoutException as exc:
                return local_error(
                    rid, started, 504,
                    {"error": {"message": "upstream timed out", "type": "gateway_timeout", "code": 504}},
                    f"upstream timeout: {type(exc).__name__} url={url}",
                )
            except httpx.HTTPError as exc:
                return local_error(
                    rid, started, 502,
                    {"error": {"message": "upstream unreachable", "type": "bad_gateway", "code": 502}},
                    f"upstream connection failed: {exc} url={url}",
                )
            finally:
                runtime.in_flight -= 1

        content = upstream.content
        media_type = upstream.headers.get("content-type", "application/json")
        if upstream.status_code != 200:
            return respond(
                rid, started, upstream.status_code, content, media_type,
                error=_upstream_error_text(upstream.status_code, content).replace("\n", " ").replace('"', "'"),
            )
        usage = finish = None
        message_content: Any = "unparsed"
        try:
            data = upstream.json()
            usage = data.get("usage") or {}
            choice = (data.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            message_content = (choice.get("message") or {}).get("content")
        except (ValueError, AttributeError):
            pass
        runtime.tokens_served += int((usage or {}).get("total_tokens") or 0)
        if not message_content:
            log.write(
                "WARN",
                f"req id={rid} upstream returned empty content finish={finish} "
                f"completion_tokens={(usage or {}).get('completion_tokens', 0)}",
            )
        return respond(rid, started, 200, content, media_type, usage=usage, finish=finish)

    @app.get("/local/stats")
    async def stats() -> JSONResponse:
        runtime.quota.check()
        return JSONResponse(
            {
                "node_id": cfg.node_id,
                "pid": os.getpid(),
                "started_at": runtime.started_at,
                "uptime_s": round(time.time() - runtime.started_at, 1),
                "tokens_served": runtime.tokens_served,
                "requests_total": runtime.handled,
                "requests_ok": runtime.by_status.get(200, 0),
                "requests_failed": sum(n for s, n in runtime.by_status.items() if s != 200),
                "by_status": {str(k): v for k, v in sorted(runtime.by_status.items())},
                "in_flight": runtime.in_flight,
                "worker_recycle_after": cfg.worker_recycle_after,
                "quota": {
                    "used_mb": round(runtime.quota.used / MB, 2),
                    "limit_mb": runtime.quota.limit / MB,
                    "over": runtime.quota.over,
                },
                "last_checkpoint": runtime.last_checkpoint,
            }
        )

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="understudy.fleet.node_app")
    parser.add_argument("--node-dir", required=True)
    args = parser.parse_args(argv)
    paths = NodePaths(Path(args.node_dir).resolve())
    log = NodeLog(paths.log)
    node_name = paths.root.name

    try:
        cfg = load_node_config(paths.config)
    except FileNotFoundError:
        log.try_open()
        die(paths, log, EXIT_CONFIG, f"config parse error line 0: {paths.config} not found")
    except ConfigError as exc:
        log.try_open()
        die(paths, log, EXIT_CONFIG, f"config parse error line {exc.line}: {exc.problem}")
    cfg.node_id = cfg.node_id or node_name

    paths.data.mkdir(parents=True, exist_ok=True)
    quota = Quota(paths.data, cfg.quota_mb, log)
    quota.check(force=True, quiet=True)

    try:
        log.open()
    except PermissionError as exc:
        message = f"cannot open {paths.log}: {exc.strerror}"
        print(message, file=sys.stderr, flush=True)
        write_exit_info(paths, EXIT_LOG, message)
        raise SystemExit(EXIT_LOG)

    quota.check(force=True)
    log.write("INFO", f"node {cfg.node_id} starting pid={os.getpid()} port={cfg.port} model={cfg.model}")
    log.write(
        "INFO",
        f"config loaded upstream={cfg.upstream_base_url} max_tokens={cfg.max_tokens} api_key_env={cfg.api_key_env} "
        f"worker_concurrency={cfg.worker_concurrency} upstream_limit={cfg.upstream_limit} "
        f"retry_backoff={'on' if cfg.retry_backoff else 'off'} worker_recycle_after={cfg.worker_recycle_after} "
        f"quota_mb={cfg.quota_mb}",
    )
    if not os.environ.get(cfg.api_key_env):
        log.write("WARN", f"environment variable {cfg.api_key_env} is not set, upstream requests carry no credentials")
    if quota.over:
        log.write("ERROR", "starting in degraded mode: every request will be answered with 507 quota exceeded")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, cfg.port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            die(paths, log, EXIT_BIND, f"bind failed: address in use ({HOST}:{cfg.port})")
        die(paths, log, EXIT_BIND, f"bind failed: {exc.strerror} ({HOST}:{cfg.port})")
    sock.set_inheritable(True)

    handler = _LogHandler(log)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

    runtime = NodeRuntime(cfg, paths, log, quota)
    app = create_app(runtime)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=cfg.port, log_config=None, access_log=False, lifespan="on"))
    log.write("INFO", f"listening on http://{HOST}:{cfg.port}")
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
