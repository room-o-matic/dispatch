import json
import sqlite3
from pathlib import Path

from dispatchd import ops

SCHEMA = """
-- One row per run: a schedule fire, a webhook delivery or a manual trigger. Progress is
-- recorded step by step so a run interrupted by a restart resumes without duplicating
-- rooms, offers or workers.
create table if not exists runs (
  id text primary key,
  source text not null,              -- schedule | webhook | manual
  name text not null,                -- the schedule or webhook name
  template text not null,
  goal text not null,
  task text not null,
  delivery_id text,                  -- webhook delivery id (dedupe)
  state text not null,               -- pending | running | done | failed | skipped
  room_url text,
  seeded integer not null default 0, -- notes and the opening message are in the room
  offers_json text not null default '{}',    -- peer -> offer_id
  sessions_json text not null default '{}',  -- worker name -> session URL
  error text,
  created_at text not null,
  updated_at text not null,
  deadline_at text,                  -- stop the workers after this
  finished_at text,
  archive_at text,
  archived_at text
);

create index if not exists runs_name on runs(source, name, created_at);
create index if not exists runs_state on runs(state);
create unique index if not exists runs_delivery
  on runs(source, name, delivery_id) where delivery_id is not null;

create table if not exists schedule_state (
  name text primary key,
  spec text,          -- "cron|timezone" next_fire was computed from; a config edit recomputes
  next_fire text,
  last_fire text,
  last_run_id text
);
"""


def _v1_to_v2(conn: sqlite3.Connection) -> None:
    conn.execute("alter table schedule_state add column spec text")


SCHEMA_VERSION = 2
MIGRATIONS: dict[int, ops.Migration] = {1: _v1_to_v2}
# The baseline (v1) schema, for adopting unversioned databases.
BASELINE = SCHEMA.replace(
    '  spec text,          -- "cron|timezone" next_fire was computed from; a config edit'
    " recomputes\n",
    "",
)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout = 10000")
    return conn


def init_db(path: Path, backup_dir: Path | None = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = ops.apply_schema(
        path,
        service="dispatchd",
        schema=SCHEMA,
        version=SCHEMA_VERSION,
        migrations=MIGRATIONS,
        baseline_schema=BASELINE,
        backup_dir=backup_dir,
    )
    conn = connect(path)
    try:
        conn.execute("pragma journal_mode = wal")
    finally:
        conn.close()
    return result


def run_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["offers"] = json.loads(d.pop("offers_json"))
    d["sessions"] = json.loads(d.pop("sessions_json"))
    d["seeded"] = bool(d["seeded"])
    return d
