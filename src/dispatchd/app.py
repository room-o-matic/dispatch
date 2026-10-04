"""The dispatchd service: the scheduler loop, run execution and (later) webhooks.

Runs execute in worker threads (the roomomatic client is synchronous), several at once.
On startup, runs a previous process left pending or running are resumed; each step is
recorded, so nothing is duplicated.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dispatchd import db, hooks, ops
from dispatchd.config import Definitions, Schedule, Settings, Template, Webhook
from dispatchd.runner import Runner, default_client
from dispatchd.scheduler import Scheduler
from dispatchd.store import KINDS, Store, load_file, merge, new_secret
from dispatchd.verify import InvalidToken, TokenVerifier

log = logging.getLogger("dispatchd")


class LiveDefinitions:
    """The operator's file plus the API's definitions, as one validated set. The file is
    re-read when it changes; API writes call refresh(). A broken file edit keeps the last
    good definitions running and is reported (readyz) until it's fixed."""

    def __init__(self, path: Path, store: Store):
        self.path, self.store = path, store
        self.error: str | None = None
        self._file = load_file(path)
        self.current, self.origin = merge(self._file, store.bodies())  # valid at startup
        self._mtime = path.stat().st_mtime

    def refresh(self) -> None:
        self.current, self.origin = merge(self._file, self.store.bodies())

    def __call__(self) -> Definitions:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._mtime = mtime
                raw = load_file(self.path)
                self.current, self.origin = merge(raw, self.store.bodies())
                self._file = raw
                self.error = None
                log.info("reloaded %s", self.path)
        except Exception as e:  # noqa: BLE001 - keep serving the last good definitions
            self.error = f"{type(e).__name__}: {e}"
            log.error("config %s is invalid, keeping the last good one: %s", self.path, e)
        return self.current

    def candidate(self, kind: str, name: str, body: dict | None) -> None:
        """Would the set still be valid with this API change (body None: deletion)?"""
        api = self.store.bodies()
        if body is None:
            api[kind].pop(name, None)
        else:
            api[kind][name] = body
        merge(self._file, api)


def create_app(
    settings: Settings | None = None,
    *,
    client=None,
    tick_seconds: float | None = None,
    verifier: TokenVerifier | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    tick_seconds = tick_seconds or settings.tick_seconds
    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    store = Store(settings.db_path, ops.Journal(settings.journal_path))
    definitions = LiveDefinitions(settings.config_path, store)
    runner = Runner(
        client or default_client(settings),
        settings.db_path,
        definitions,
        poll_seconds=settings.poll_seconds,
    )
    scheduler = Scheduler(runner, definitions)
    running: dict[str, asyncio.Task] = {}
    health = ops.LoopHealth()

    def start(run_id: str) -> None:
        if run_id in running:
            return
        task = asyncio.create_task(asyncio.to_thread(runner.execute, run_id))
        running[run_id] = task
        task.add_done_callback(lambda _t, r=run_id: running.pop(r, None))

    async def loop() -> None:
        while True:
            try:
                for run_id in await asyncio.to_thread(scheduler.tick):
                    start(run_id)
                await asyncio.to_thread(runner.archive_due)
                health.ok()
            except Exception as e:  # noqa: BLE001 - keep the loop alive; reported in readyz
                health.failed(e)
                log.exception("scheduler pass failed")
            await asyncio.sleep(tick_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for run_id in runner.unfinished():  # a previous process stopped mid-run
            start(run_id)
        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="dispatchd", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.definitions = definitions
    app.state.running = running
    app.state.start_run = start

    # ----- webhooks ----------------------------------------------------------------------

    def run_status(run: dict) -> dict:
        keys = ("id", "state", "room_url", "error", "created_at", "finished_at")
        return {k: run[k] for k in keys}

    def webhook_secret(name: str) -> tuple:
        w = definitions().webhooks.get(name)
        if w is None or not w.enabled:
            raise hooks.HookError(404, "no such webhook")
        secret = os.environ.get(w.secret_env) if w.secret_env else store.secret(name)
        if not secret:
            raise hooks.HookError(503, "webhook is not configured with a secret")
        return w, secret

    async def read_capped(request: Request, cap: int) -> bytes:
        declared = request.headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) > cap):
            raise hooks.HookError(413, f"body is over {cap} bytes")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > cap:
                raise hooks.HookError(413, f"body is over {cap} bytes")
        return body

    @app.post("/v1/hooks/{name}")
    async def webhook(name: str, request: Request) -> JSONResponse:
        try:
            w, secret = webhook_secret(name)
            body = await read_capped(request, w.max_prompt_bytes + 4096)
            hooks.verify(secret, request.headers, body)
            delivery = request.headers.get("x-rom-delivery") or ""
            if not hooks.DELIVERY.match(delivery):
                raise hooks.HookError(400, "X-Rom-Delivery is required: [A-Za-z0-9_.:-]{1,100}")
            prompt, goal = hooks.parse(body, w.max_prompt_bytes)
            # a retried delivery is answered before the rate limit: retries stay safe
            if seen := runner.find_delivery("webhook", name, delivery):
                return JSONResponse(run_status(seen), status_code=200)
            hour_ago = runner.clock() - timedelta(hours=1)
            if runner.count_since("webhook", name, hour_ago) >= w.rate_per_hour:
                raise hooks.HookError(
                    429, f"over {w.rate_per_hour} runs per hour", {"Retry-After": "300"}
                )
            run, created = runner.create(
                "webhook",
                name,
                w.use,
                goal or prompt.strip().splitlines()[0][:200],
                task=hooks.render(w.task_template, prompt),
                delivery_id=delivery,
            )
        except hooks.HookError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status, headers=e.headers)
        if run["state"] == "pending":
            start(run["id"])
        out = {**run_status(run), "status_url": f"/v1/hooks/{name}/runs/{run['id']}"}
        return JSONResponse(out, status_code=202 if created else 200)

    @app.get("/v1/hooks/{name}/runs/{run_id}")
    async def webhook_run(name: str, run_id: str, request: Request) -> JSONResponse:
        """Status of a run this webhook started; signed like the POST (empty body)."""
        try:
            _, secret = webhook_secret(name)
            hooks.verify(secret, request.headers, b"")
        except hooks.HookError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
        run = runner.get(run_id)
        if not run or run["source"] != "webhook" or run["name"] != name:
            return JSONResponse({"detail": "no such run"}, status_code=404)
        return JSONResponse(run_status(run))

    # ----- operator API ----------------------------------------------------------------
    # lobbyd access tokens for this service's base_url, from identities listed in
    # DISPATCHD_OPERATORS. Default deny: with no operators configured, nobody gets in.

    verifier = verifier or TokenVerifier(
        issuer=settings.lobby_url,
        domain=settings.lobby_domain,
        audience=settings.base_url,
        jwks_url=settings.lobby_jwks_url,
    )
    bearer = HTTPBearer(auto_error=False)

    async def operator(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if creds is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        try:
            claims = await asyncio.to_thread(verifier.verify, creds.credentials)
        except InvalidToken as e:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from e
        if claims.scope != "agent" or claims.identity not in settings.operators:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{claims.identity} is not an operator")
        return claims.identity

    Operator = Annotated[str, Depends(operator)]

    @app.get("/v1/runs")
    async def list_runs(
        who: Operator, source: str | None = None, name: str | None = None, limit: int = 50
    ) -> list[dict]:
        return await asyncio.to_thread(runner.recent, source, name, min(limit, 500))

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, who: Operator) -> dict:
        run = await asyncio.to_thread(runner.get, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
        return run

    @app.get("/v1/schedules")
    async def list_schedules(who: Operator) -> list[dict]:
        state = await asyncio.to_thread(scheduler.state)
        return [
            {
                "name": n,
                "source": definitions.origin.get(("schedules", n)),
                "cron": s.cron,
                "timezone": s.timezone,
                "use": s.use,
                "enabled": s.enabled,
                "next_fire": state.get(n, {}).get("next_fire"),
                "last_fire": state.get(n, {}).get("last_fire"),
                "last_run_id": state.get(n, {}).get("last_run_id"),
            }
            for n, s in definitions().schedules.items()
        ]

    @app.post("/v1/schedules/{name}/run", status_code=status.HTTP_202_ACCEPTED)
    async def trigger(name: str, who: Operator) -> dict:
        s = definitions().schedules.get(name)
        if s is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such schedule")
        run, _ = await asyncio.to_thread(runner.create, "manual", name, s.use, s.goal)
        log.info("%s triggered schedule %s: run %s", who, name, run["id"])
        if run["state"] == "pending":
            start(run["id"])
        return run

    # ----- definitions through the API ----------------------------------------------------
    # The config file's definitions are read-only here; the API adds its own beside them.

    models = {"templates": Template, "schedules": Schedule, "webhooks": Webhook}
    singular = {v: k for k, v in KINDS.items()}

    def stamp() -> str:
        return runner.clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def describe(kind: str, name: str) -> dict:
        item = getattr(definitions(), kind).get(name)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no {kind[:-1]} {name!r}")
        out = {
            "name": name,
            "source": definitions.origin.get((kind, name)),
            **item.model_dump(mode="json", exclude_none=True),
        }
        if meta := store.meta(singular[kind], name):
            out |= {"created_by": meta["created_by"], "updated_at": meta["updated_at"]}
        if kind == "schedules":
            st = scheduler.state().get(name, {})
            out |= {"next_fire": st.get("next_fire"), "last_fire": st.get("last_fire")}
        if kind == "webhooks":
            out["url_path"] = f"/v1/hooks/{name}"
        if kind != "templates":
            keep = ("id", "source", "state", "room_url", "error", "created_at")
            out["recent_runs"] = [
                {k: r[k] for k in keep} for r in runner.recent(name=name, limit=5)
            ]
        return out

    def write(kind: str, name: str, body: dict | None, who: str, secret: str | None = None):
        """Validate an API change against the whole merged set, then store it."""
        if definitions.origin.get((kind, name)) == "file":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{kind[:-1]} {name!r} is defined in the config file; edit it there",
            )
        try:
            if body is not None:
                body = models[kind].model_validate(body).model_dump(mode="json", exclude_none=True)
            definitions.candidate(kind, name, body)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
        if body is None:
            store.delete(singular[kind], name)
        else:
            store.put(singular[kind], name, body, who, stamp(), secret=secret)
        definitions.refresh()
        log.info("%s %s %s %s", who, "deleted" if body is None else "wrote", kind[:-1], name)

    @app.get("/v1/templates")
    async def list_templates(who: Operator) -> list[dict]:
        return [describe("templates", n) for n in definitions().templates]

    @app.get("/v1/templates/{name}")
    async def get_template(name: str, who: Operator) -> dict:
        return describe("templates", name)

    @app.put("/v1/templates/{name}")
    async def put_template(name: str, body: dict, who: Operator) -> dict:
        write("templates", name, body, who)
        return describe("templates", name)

    @app.delete("/v1/templates/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_template(name: str, who: Operator) -> Response:
        describe("templates", name)
        write("templates", name, None, who)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/schedules/{name}")
    async def get_schedule(name: str, who: Operator) -> dict:
        return describe("schedules", name)

    @app.put("/v1/schedules/{name}")
    async def put_schedule(name: str, body: dict, who: Operator) -> dict:
        write("schedules", name, body, who)
        return describe("schedules", name)

    @app.delete("/v1/schedules/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_schedule(name: str, who: Operator) -> Response:
        describe("schedules", name)
        write("schedules", name, None, who)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/webhooks")
    async def list_webhooks(who: Operator) -> list[dict]:
        return [describe("webhooks", n) for n in definitions().webhooks]

    @app.get("/v1/webhooks/{name}")
    async def get_webhook(name: str, who: Operator) -> dict:
        return describe("webhooks", name)

    @app.post("/v1/webhooks/{name}", status_code=status.HTTP_201_CREATED)
    async def create_webhook(name: str, body: dict, who: Operator) -> dict:
        """Create a webhook. Its HMAC secret is generated here and returned only now."""
        if name in definitions().webhooks:
            raise HTTPException(status.HTTP_409_CONFLICT, f"webhook {name!r} already exists")
        if "secret_env" in body:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "secret_env is for config-file webhooks; the API generates the secret",
            )
        secret = new_secret()
        write("webhooks", name, body, who, secret=secret)
        signing = (
            "X-Rom-Signature: sha256=<hex HMAC-SHA256(secret, '<X-Rom-Timestamp>.<raw body>')>,"
            ' plus X-Rom-Timestamp and X-Rom-Delivery; body {"prompt": ..., "goal"?: ...}'
        )
        return {**describe("webhooks", name), "secret": secret, "signing": signing}

    @app.post("/v1/webhooks/{name}/rotate-secret")
    async def rotate_webhook_secret(name: str, who: Operator) -> dict:
        if definitions.origin.get(("webhooks", name)) != "api":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "only API-created webhooks have a secret dispatchd can rotate",
            )
        secret = new_secret()
        store.put("webhook", name, store.bodies()["webhooks"][name], who, stamp(), secret=secret)
        log.info("%s rotated the secret of webhook %s", who, name)
        return {"name": name, "secret": secret}

    @app.delete("/v1/webhooks/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_webhook(name: str, who: Operator) -> Response:
        describe("webhooks", name)
        write("webhooks", name, None, who)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/metrics")
    def metrics() -> Response:
        conn = db.connect(settings.db_path)
        try:
            by_state = dict(conn.execute("select state, count(*) from runs group by state"))
            hour_ago = runner.clock() - timedelta(hours=1)
            recent = conn.execute(
                "select count(*) from runs where source = 'webhook' and created_at > ?",
                (hour_ago.isoformat().replace("+00:00", "Z"),),
            ).fetchone()[0]
        finally:
            conn.close()
        defs = definitions()
        gauges = {
            f"runs_{s}": by_state.get(s, 0)
            for s in ("pending", "running", "done", "failed", "skipped")
        }
        gauges |= {
            "runs_executing": len(running),
            "schedules": len(defs.schedules),
            "webhooks": len(defs.webhooks),
            "webhook_runs_last_hour": recent,
            "config_ok": definitions.error is None,
            "scheduler_failures": health.failures,
            "scheduler_age_seconds": health.age(),
        }
        return Response(ops.prometheus("dispatchd", gauges), media_type="text/plain; version=0.0.4")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict:
        definitions()  # pick up a file edit now, not on the next scheduler pass
        checks = {
            "database": {"ok": ops.db_writable(settings.db_path) is None},
            "config": {"ok": definitions.error is None, "error": definitions.error},
            "scheduler": {
                "ok": health.failures < 3,
                "age_seconds": health.age(),
                "last_error": health.last_error,
            },
        }
        ready = all(c["ok"] for c in checks.values())
        response.status_code = 200 if ready else 503
        return {"ready": ready, "checks": checks, "running": len(running)}

    return app
