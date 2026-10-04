import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_runner import TEMPLATE, Stub

from dispatchd import db
from dispatchd.app import create_app
from dispatchd.config import Definitions, Settings
from dispatchd.runner import Runner
from dispatchd.scheduler import Scheduler

T0 = datetime(2026, 10, 5, 6, 58, tzinfo=UTC)  # a Monday


def definitions(cron="0 7 * * 1-5", catch_up="5m", enabled=True, **run):
    tmpl = {**TEMPLATE, "run": {**TEMPLATE["run"], **run}}
    return Definitions.model_validate(
        {
            "templates": {"review": tmpl},
            "schedules": {
                "triage": {
                    "cron": cron,
                    "use": "review",
                    "goal": "Triage.",
                    "catch_up": catch_up,
                    "enabled": enabled,
                }
            },
        }
    )


@pytest.fixture
def sched(tmp_path):
    path = tmp_path / "d.sqlite"
    db.init_db(path)
    clock = {"now": T0}
    holder = {"defs": definitions()}
    runner = Runner(
        Stub(), path, lambda: holder["defs"], clock=lambda: clock["now"], sleep=lambda s: None
    )
    s = Scheduler(runner, lambda: holder["defs"])
    s.clock, s.holder = clock, holder
    return s


def advance(s, **delta):
    s.clock["now"] += timedelta(**delta)


def test_first_sight_schedules_but_does_not_fire(sched):
    assert sched.tick() == []
    assert sched.state()["triage"]["next_fire"] == "2026-10-05T07:00:00.000Z"


def test_fires_when_due_then_moves_to_the_next_day(sched):
    sched.tick()
    advance(sched, minutes=2, seconds=5)
    (run_id,) = sched.tick()
    run = sched.runner.get(run_id)
    assert run["source"] == "schedule" and run["state"] == "pending" and run["goal"] == "Triage."
    st = sched.state()["triage"]
    assert st["last_fire"] == "2026-10-05T07:00:00.000Z" and st["last_run_id"] == run_id
    assert st["next_fire"] == "2026-10-06T07:00:00.000Z"
    assert sched.tick() == []  # not again until tomorrow


def test_a_missed_fire_is_recorded_not_replayed(sched):
    sched.tick()
    advance(sched, hours=5)  # dispatchd was down at 07:00
    assert sched.tick() == []
    (run,) = sched.runner.recent()
    assert run["state"] == "skipped" and "missed by" in run["error"]
    assert sched.state()["triage"]["next_fire"] == "2026-10-06T07:00:00.000Z"


def test_a_cron_edit_recomputes_the_next_fire(sched):
    sched.tick()
    sched.holder["defs"] = definitions(cron="30 6 * * *")
    sched.tick()
    assert sched.state()["triage"]["next_fire"] == "2026-10-06T06:30:00.000Z"


def test_disabled_schedules_never_fire(sched):
    sched.holder["defs"] = definitions(enabled=False)
    sched.tick()
    advance(sched, days=2)
    assert sched.tick() == [] and sched.state() == {}


def test_overlap_skip_through_the_scheduler(sched):
    sched.holder["defs"] = definitions(cron="* * * * *")
    sched.tick()
    advance(sched, minutes=1)
    assert len(sched.tick()) == 1  # pending, never executed: still active
    advance(sched, minutes=1)
    assert sched.tick() == []
    assert sched.runner.recent()[0]["state"] == "skipped"


def test_v1_database_migrates_to_v2(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(db.BASELINE)  # what phase 1 created
    conn.execute("pragma user_version = 1")
    conn.execute("insert into schedule_state (name, next_fire) values ('triage', 'x')")
    conn.commit()
    conn.close()
    result = db.init_db(path, backup_dir=tmp_path / "bk")
    assert (result["from"], result["to"]) == (1, 2) and result["backup"]
    conn = sqlite3.connect(path)
    assert conn.execute("select name, spec from schedule_state").fetchall() == [("triage", None)]


# ----- the service ----------------------------------------------------------------------


def test_service_resumes_runs_and_reports_a_broken_config(tmp_path):
    cfg = tmp_path / "dispatch.yaml"
    import yaml

    cfg.write_text(yaml.safe_dump({"templates": {"review": TEMPLATE}}))
    settings = Settings(
        data_dir=tmp_path / "data", config_path=cfg, lobby_url="http://l", poll_seconds=0.01
    )
    stub = Stub()
    app = create_app(settings, client=stub, tick_seconds=0.05)
    run, _ = app.state.runner.create("manual", "x", "review", "Goal")  # left by a "crash"
    with TestClient(app) as c:
        for _ in range(200):
            if app.state.runner.get(run["id"])["state"] == "done":
                break
            import time

            time.sleep(0.02)
        assert app.state.runner.get(run["id"])["state"] == "done"  # resumed on startup
        assert c.get("/readyz").status_code == 200
        cfg.write_text("templates: {review: {}}\n")  # an invalid edit
        import os

        os.utime(cfg, (1, 1))
        time.sleep(0.2)
        r = c.get("/readyz")
        assert (
            r.status_code == 503 and "at least one worker" in r.json()["checks"]["config"]["error"]
        )
        assert "review" in app.state.definitions.current.templates  # last good kept
