"""The dispatchd service: the scheduler loop, run execution and (later) webhooks.

Runs execute in worker threads (the roomomatic client is synchronous), several at once.
On startup, runs a previous process left pending or running are resumed; each step is
recorded, so nothing is duplicated.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Response

from dispatchd import db, ops
from dispatchd.config import Definitions, Settings
from dispatchd.runner import Runner, default_client
from dispatchd.scheduler import Scheduler

log = logging.getLogger("dispatchd")


class LiveDefinitions:
    """The operator's definitions, re-read when the file changes. A broken edit keeps the
    last good definitions running and is reported (readyz) until it's fixed."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self.current = Definitions.load(path)  # must be valid at startup
        self._mtime = path.stat().st_mtime
        self.error: str | None = None

    def __call__(self) -> Definitions:
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._mtime = mtime
                self.current = Definitions.load(self.path)
                self.error = None
                log.info("reloaded %s", self.path)
        except Exception as e:  # noqa: BLE001 - keep serving the last good definitions
            self.error = f"{type(e).__name__}: {e}"
            log.error("config %s is invalid, keeping the last good one: %s", self.path, e)
        return self.current


def create_app(
    settings: Settings | None = None,
    *,
    client=None,
    tick_seconds: float | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    tick_seconds = tick_seconds or settings.tick_seconds
    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    definitions = LiveDefinitions(settings.config_path)
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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict:
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
