"""Fires schedules. `tick()` is called every few seconds by the service:

- a schedule seen for the first time (or whose cron/time zone changed in the config)
  gets its next fire time computed and stored; it does not fire on sight;
- when a fire is due it creates a run (source "schedule"), subject to the template's
  overlap policy, and advances to the next fire time;
- a fire missed by more than the schedule's `catch_up` (dispatchd was down) is not
  replayed: it is recorded as a skipped run, and the schedule moves on. At most one run is
  created per tick per schedule, however many fires were missed.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

from dispatchd import db
from dispatchd.config import Definitions
from dispatchd.cron import Cron
from dispatchd.runner import Runner, now_iso

log = logging.getLogger("dispatchd.scheduler")


class Scheduler:
    def __init__(self, runner: Runner, definitions: Callable[[], Definitions]):
        self.runner = runner
        self.definitions = definitions

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.runner.db_path)

    def state(self) -> dict[str, dict]:
        conn = self._conn()
        try:
            return {r["name"]: dict(r) for r in conn.execute("select * from schedule_state")}
        finally:
            conn.close()

    def _save(self, name: str, **fields) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("insert or ignore into schedule_state (name) values (?)", (name,))
                cols = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(
                    f"update schedule_state set {cols} where name = ?", (*fields.values(), name)
                )
        finally:
            conn.close()

    def tick(self) -> list[str]:
        """Returns the ids of runs created now (pending, ready to execute)."""
        now = self.runner.clock()
        defs = self.definitions()
        states = self.state()
        created = []
        for name, s in defs.schedules.items():
            if not s.enabled:
                continue
            cron = Cron.parse(s.cron)
            spec = f"{s.cron}|{s.timezone}"
            st = states.get(name) or {}
            if st.get("spec") != spec or not st.get("next_fire"):
                self._save(name, spec=spec, next_fire=now_iso(cron.next_after(now, s.timezone)))
                continue
            due = datetime.fromisoformat(st["next_fire"])
            if now < due:
                continue
            nxt = now_iso(cron.next_after(now, s.timezone))
            late = (now - due).total_seconds()
            if late > s.catch_up:
                run, _ = self.runner.create("schedule", name, s.use, s.goal)
                self.runner.mark_skipped(
                    run["id"], f"fire at {st['next_fire']} missed by {int(late)}s; not replayed"
                )
                log.warning("schedule %s: missed fire at %s skipped", name, st["next_fire"])
                self._save(name, next_fire=nxt, last_fire=st["next_fire"], last_run_id=run["id"])
                continue
            run, _ = self.runner.create("schedule", name, s.use, s.goal)
            self._save(name, next_fire=nxt, last_fire=st["next_fire"], last_run_id=run["id"])
            if run["state"] == "pending":
                created.append(run["id"])
            else:
                log.info("schedule %s: run %s %s (%s)", name, run["id"], run["state"], run["error"])
        return created
