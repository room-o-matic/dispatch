"""The run lifecycle, the same for schedules, webhooks and manual triggers:

1. record the run (pending);
2. create the room (closed and unlisted by default), apply the template's loop guards;
3. seed the `goal` and `restrictions` notes and post the goal as the opening message;
4. grant each named peer its rights, then send it an offer (idempotent offer_id);
5. summon each worker (idempotent operation_id, so a resumed run never doubles up);
6. wait until every worker session has ended, or stop them at the template's max_duration;
7. hand back any room cleanup agentd couldn't finish (finalize_session);
8. mark the run done (or failed), and archive the room after `archive_after`.

Every step is recorded as it completes, so `execute` can be called again on a run that
was interrupted and picks up where it stopped. Worker names get the run's short id as a
suffix: a guest identity has at most one live invite, and two runs of a template may
overlap. The client is synchronous; dispatchd runs this off its event loop.
"""

import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from roomomatic import TERMINAL_STATUSES

from dispatchd import db
from dispatchd.config import Definitions, Template

log = logging.getLogger("dispatchd.runner")
FINISHED = ("done", "failed", "skipped")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def now_iso(t: datetime | None = None) -> str:
    t = t or datetime.now(UTC)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    """run_<ULID>: sortable by creation time."""
    ms = int(time.time() * 1000)
    raw = (ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "run_" + "".join(_CROCKFORD[(raw >> (5 * i)) & 31] for i in reversed(range(26)))


def worker_handle(name: str, run_id: str) -> str:
    return f"{name}-{run_id[-5:].lower()}"


def restrictions_note(t: Template, run_id: str) -> dict:
    return {
        "rules": t.rules,
        "enforced": "agentd profiles and grants, room admission and rights, loop guards",
        "workers": [
            {
                "handle": worker_handle(w.name, run_id),
                "worker_type": w.worker_type,
                "profile": w.profile,
                "role": w.role,
            }
            for w in t.workers
        ],
        "peers": [{"agent": p.agent, "rights": p.rights, "role": p.role} for p in t.peers],
        "max_duration_seconds": t.run.max_duration,
    }


def compose_task(goal: str, t: Template, run: dict) -> str:
    lines = [goal.strip(), ""]
    if t.rules:
        lines += ["Rules for this room:", *(f"- {r}" for r in t.rules), ""]
    lines.append(
        f"This room was opened by dispatch ({run['source']} {run['name']!r}, run {run['id']}). "
        "Work in the room: post your findings there with typed messages, then end your turn."
    )
    return "\n".join(lines)


class Runner:
    def __init__(
        self,
        client,
        db_path: Path,
        definitions: Callable[[], Definitions],
        *,
        poll_seconds: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.db_path = db_path
        self.definitions = definitions
        self.poll = poll_seconds
        self.clock = clock
        self.sleep = sleep

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def get(self, run_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        finally:
            conn.close()
        return db.run_dict(row) if row else None

    def recent(self, source: str | None = None, name: str | None = None, limit: int = 50):
        sql, params = "select * from runs where 1=1", []
        if source:
            sql, params = sql + " and source = ?", [*params, source]
        if name:
            sql, params = sql + " and name = ?", [*params, name]
        conn = self._conn()
        try:
            rows = conn.execute(sql + " order by id desc limit ?", [*params, limit]).fetchall()
        finally:
            conn.close()
        return [db.run_dict(r) for r in rows]

    def _update(self, run_id: str, **fields) -> None:
        for k in ("offers", "sessions"):
            if k in fields:
                fields[f"{k}_json"] = json.dumps(fields.pop(k))
        fields["updated_at"] = now_iso(self.clock())
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn = self._conn()
        try:
            with conn:
                conn.execute(f"update runs set {cols} where id = ?", (*fields.values(), run_id))
        finally:
            conn.close()

    # ----- creating runs -------------------------------------------------------------------

    def active(self, source: str, name: str) -> int:
        conn = self._conn()
        try:
            return conn.execute(
                "select count(*) from runs where source = ? and name = ?"
                " and state in ('pending', 'running')",
                (source, name),
            ).fetchone()[0]
        finally:
            conn.close()

    def create(
        self,
        source: str,
        name: str,
        template: str,
        goal: str,
        *,
        task: str | None = None,
        delivery_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Record a run. Returns (run, created); a repeated webhook delivery returns the
        original run with created=False. A run over the template's overlap limit is
        recorded as skipped."""
        defs = self.definitions()
        t = defs.templates[template]
        now = now_iso(self.clock())
        run_id = new_run_id()
        state = "pending"
        error = None
        busy = self.active(source, name)
        if busy and (t.run.on_overlap == "skip" or busy >= t.run.max_concurrent):
            state, error = "skipped", f"{busy} run(s) of {source} {name!r} still active"
        run = {"id": run_id, "source": source, "name": name}
        conn = self._conn()
        try:
            if delivery_id is not None:
                dup = conn.execute(
                    "select * from runs where source = ? and name = ? and delivery_id = ?",
                    (source, name, delivery_id),
                ).fetchone()
                if dup:
                    return db.run_dict(dup), False
            with conn:
                conn.execute(
                    "insert into runs (id, source, name, template, goal, task, delivery_id,"
                    " state, error, created_at, updated_at, finished_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        source,
                        name,
                        template,
                        goal,
                        task or compose_task(goal, t, run),
                        delivery_id,
                        state,
                        error,
                        now,
                        now,
                        now if state == "skipped" else None,
                    ),
                )
        except sqlite3.IntegrityError:  # a concurrent identical delivery won the race
            row = conn.execute(
                "select * from runs where source = ? and name = ? and delivery_id = ?",
                (source, name, delivery_id),
            ).fetchone()
            return db.run_dict(row), False
        finally:
            conn.close()
        return self.get(run_id), True

    # ----- executing -------------------------------------------------------------------------

    def unfinished(self) -> list[str]:
        conn = self._conn()
        try:
            return [
                r[0]
                for r in conn.execute(
                    "select id from runs where state in ('pending', 'running') order by id"
                )
            ]
        finally:
            conn.close()

    def execute(self, run_id: str) -> dict:
        run = self.get(run_id)
        if run is None or run["state"] in FINISHED:
            return run
        defs = self.definitions()
        t = defs.templates.get(run["template"])
        if t is None:
            self._finish(run_id, "failed", f"template {run['template']!r} no longer exists", 0)
            return self.get(run_id)
        self._update(run_id, state="running")
        try:
            self._room(run, t)
            run = self.get(run_id)
            self._seed(run, t)
            self._peers(run, t)
            self._workers(run, t)
            stopped = self._wait(self.get(run_id), t)
            self._finalize(self.get(run_id))
            self._finish(run_id, "done", stopped, t.room.archive_after)
        except Exception as e:  # noqa: BLE001 - recorded on the run; workers are stopped
            log.exception("run %s failed", run_id)
            self._stop_all(self.get(run_id), "dispatch_run_failed")
            self._finish(run_id, "failed", f"{type(e).__name__}: {e}", t.room.archive_after)
        self.archive_due()
        return self.get(run_id)

    def _room(self, run: dict, t: Template) -> None:
        if run["room_url"]:
            return
        stamp = self.clock().strftime("%Y%m%d-%H%M")
        room = self.client.create_room(
            f"{run['source']}-{run['name']}-{stamp}",
            server_tag=t.room.server_tag,
            purpose=run["goal"][:2000],
            listed=t.room.listed,
            admission=t.room.admission,
        )
        self._update(run["id"], room_url=room["room_url"])
        guards = {
            k: v
            for k, v in (
                ("max_hops", t.room.max_hops),
                ("message_rate_per_minute", t.room.message_rate_per_minute),
            )
            if v
        }
        if guards:
            rooms, rid = self.client.room(room["room_url"])
            rooms.update_room(rid, **guards)

    def _seed(self, run: dict, t: Template) -> None:
        if run["seeded"]:
            return
        rooms, rid = self.client.room(run["room_url"])
        rooms.put_note(rid, "goal", run["goal"])
        rooms.put_note(rid, "restrictions", restrictions_note(t, run["id"]))
        handles = ", ".join(f"@{worker_handle(w.name, run['id'])}" for w in t.workers)
        peers = ", ".join(p.agent for p in t.peers)
        who = " and ".join(x for x in (handles, peers) if x)
        rooms.post(
            rid,
            f"{run['goal'].strip()}\n\n(dispatch run {run['id']}; joining: {who})",
            type="message",
            topic="goal",
        )
        self._update(run["id"], seeded=1)

    def _peers(self, run: dict, t: Template) -> None:
        offers = dict(run["offers"])
        rooms, rid = self.client.room(run["room_url"])
        for p in t.peers:
            if p.agent in offers:
                continue
            rooms.grant(rid, p.agent, list(p.rights))
            offer_id = f"{run['id']}.{p.agent.replace('@', '.')}"[:80]
            fields = {"role": p.role} if p.role else {}
            if p.respond_within:
                fields["deadline_seconds"] = max(10, p.respond_within)
            self.client.lobby.offer(
                p.agent, run["room_url"], run["task"], offer_id=offer_id, **fields
            )
            offers[p.agent] = offer_id
            self._update(run["id"], offers=offers)

    def _workers(self, run: dict, t: Template) -> None:
        sessions = dict(run["sessions"])
        deadline = run["deadline_at"] or now_iso(
            self.clock() + timedelta(seconds=t.run.max_duration)
        )
        self._update(run["id"], deadline_at=deadline)
        for w in t.workers:
            if w.name in sessions:
                continue
            s = self.client.summon(
                run["room_url"],
                run["task"],
                worker_type=w.worker_type,
                profile=w.profile,
                name=worker_handle(w.name, run["id"]),
                role=w.role,
                instance_url=w.instance,
                operation_id=f"{run['id']}.{w.name}",
            )
            sessions[w.name] = s.session_url
            self._update(run["id"], sessions=sessions)

    def _status(self, session_url: str) -> str:
        agentd, sid = self.client.session(session_url)
        return agentd.session(sid)["status"]

    def _wait(self, run: dict, t: Template) -> str | None:
        """Until every worker session ends; at the deadline, stop the rest."""
        deadline = datetime.fromisoformat(run["deadline_at"])
        while True:
            live = [u for u in run["sessions"].values() if self._status(u) not in TERMINAL_STATUSES]
            if not live:
                return None
            if self.clock() >= deadline:
                self._stop_all(run, "dispatch_max_duration")
                return f"stopped {len(live)} worker(s) at max_duration"
            self.sleep(self.poll)

    def _stop_all(self, run: dict | None, reason: str) -> None:
        for url in (run or {}).get("sessions", {}).values():
            try:
                agentd, sid = self.client.session(url)
                if agentd.session(sid)["status"] not in TERMINAL_STATUSES:
                    agentd.stop(sid, reason)
            except Exception:  # noqa: BLE001 - best effort; reported on the run anyway
                log.warning("run %s: could not stop %s", run["id"], url, exc_info=True)

    def _finalize(self, run: dict) -> None:
        for url in run["sessions"].values():
            self.client.finalize_session(url)

    def _finish(self, run_id: str, state: str, error: str | None, archive_after: int) -> None:
        now = self.clock()
        self._update(
            run_id,
            state=state,
            error=error,
            finished_at=now_iso(now),
            archive_at=now_iso(now + timedelta(seconds=archive_after)),
        )

    def archive_due(self) -> int:
        """Archive the rooms of finished runs whose archive time has come."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "select id, room_url from runs where archive_at is not null"
                " and archived_at is null and room_url is not null and archive_at <= ?",
                (now_iso(self.clock()),),
            ).fetchall()
        finally:
            conn.close()
        for run_id, room_url in rows:
            try:
                rooms, rid = self.client.room(room_url)
                rooms.update_room(rid, archived=True)
                self._update(run_id, archived_at=now_iso(self.clock()))
            except Exception:  # noqa: BLE001 - retried on the next pass
                log.warning("run %s: archiving %s failed", run_id, room_url, exc_info=True)
        return len(rows)


def default_client(settings):
    from roomomatic import Client

    if not settings.api_key:
        raise SystemExit("DISPATCHD_LOBBYD_API_KEY is not set")
    return Client(
        settings.lobby_url,
        settings.api_key,
        cleanup_journal=settings.data_dir / "cleanup-journal.json",
    )
