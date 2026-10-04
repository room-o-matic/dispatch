"""The run lifecycle against a stub of the roomomatic client: ordering, idempotent
resume, the max_duration stop, failure cleanup, archiving and overlap."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from dispatchd import db
from dispatchd.config import Definitions
from dispatchd.runner import Runner, worker_handle

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class Stub:
    """Records every call as (method, args). Sessions finish after `polls` status reads."""

    def __init__(self, polls=1, fail_summon=None):
        self.calls: list[tuple] = []
        self.rooms_made = 0
        self.status: dict[str, list[str]] = {}
        self.polls = polls
        self.fail_summon = fail_summon
        self.by_op: dict[str, str] = {}
        self.summon_kw: list[dict] = []
        self.lobby = SimpleNamespace(offer=self._offer)

    def _rec(self, *c):
        self.calls.append(c)

    def names(self):
        return [c[0] for c in self.calls]

    # client
    def create_room(self, name, **kw):
        self.rooms_made += 1
        self._rec("create_room", name, kw)
        return {"room_url": f"http://rooms/v1/rooms/room_{self.rooms_made}"}

    def room(self, url):
        rid = url.rsplit("/", 1)[1]
        stub = self
        return SimpleNamespace(
            update_room=lambda r, **kw: stub._rec("update_room", r, kw),
            put_note=lambda r, k, v: stub._rec("put_note", k, v),
            post=lambda r, body, type="message", **f: stub._rec("post", body, type, f),
            grant=lambda r, agent, rights: stub._rec("grant", agent, rights),
        ), rid

    def _offer(self, to, room_url, task, offer_id=None, **f):
        self._rec("offer", to, offer_id, f)
        return {"offer_id": offer_id}

    def summon(self, room_url, task, **kw):
        if self.fail_summon and kw["name"].startswith(self.fail_summon):
            raise RuntimeError("no agentd with capacity")
        op = kw["operation_id"]
        sid = self.by_op.setdefault(op, f"agt_{len(self.by_op) + 1}")
        self.status.setdefault(sid, ["running"] * self.polls + ["completed"])
        self._rec("summon", kw["name"], op, task)
        self.summon_kw.append(kw)
        return SimpleNamespace(session_url=f"http://agentd/v1/sessions/{sid}")

    def session(self, url):
        sid = url.rsplit("/", 1)[1]
        stub = self

        def status(s):
            seq = stub.status[s]
            return {"status": seq.pop(0) if len(seq) > 1 else seq[0]}

        def stop(s, reason):
            stub._rec("stop", s, reason)
            stub.status[s] = ["stopped"]

        return SimpleNamespace(session=status, stop=stop), sid

    def finalize_session(self, url):
        self._rec("finalize", url.rsplit("/", 1)[1])
        return "done"


TEMPLATE = {
    "room": {"max_hops": 3, "archive_after": 0},
    "rules": ["Read-only."],
    "workers": [
        {"name": "claude", "worker_type": "claude", "profile": "ro", "role": "reviewer"},
        {"name": "ollama", "worker_type": "ollama", "profile": "ro"},
    ],
    "peers": [{"agent": "odin@local", "role": "reviewer", "respond_within": "30m"}],
    "run": {"max_duration": "1h"},
}


@pytest.fixture
def make(tmp_path):
    path = tmp_path / "d.sqlite"
    db.init_db(path)
    clock = {"now": T0}

    def _make(stub=None, template=None, **run):
        tmpl = {**TEMPLATE, **(template or {})}
        if run:
            tmpl["run"] = {**tmpl["run"], **run}
        defs = Definitions.model_validate({"templates": {"review": tmpl}})
        r = Runner(
            stub or Stub(),
            path,
            lambda: defs,
            poll_seconds=60,
            clock=lambda: clock["now"],
            sleep=lambda s: clock.update(now=clock["now"] + timedelta(seconds=s)),
        )
        r.clock_state = clock
        return r

    return _make


def test_full_lifecycle_in_order(make):
    r = make()
    run, created = r.create("schedule", "triage", "review", "Triage new issues.")
    assert created and run["state"] == "pending"
    run = r.execute(run["id"])
    assert run["state"] == "done" and run["error"] is None
    s = r.client
    order = s.names()
    assert order[:2] == ["create_room", "update_room"]
    assert order.index("grant") < order.index("offer") < order.index("summon")
    name, kw = s.calls[0][1], s.calls[0][2]
    assert name.startswith("schedule-triage-20261004-1200")
    assert kw["admission"] == "closed" and kw["listed"] is False
    assert s.calls[1][2] == {"max_hops": 3}
    notes = {c[1]: c[2] for c in s.calls if c[0] == "put_note"}
    assert notes["goal"] == "Triage new issues."
    handle = worker_handle("claude", run["id"])
    assert notes["restrictions"]["rules"] == ["Read-only."]
    assert notes["restrictions"]["workers"][0]["handle"] == handle
    opening = next(c for c in s.calls if c[0] == "post")
    assert f"@{handle}" in opening[1] and "odin@local" in opening[1]
    assert opening[3] == {"topic": "goal"}
    offer = next(c for c in s.calls if c[0] == "offer")
    assert offer[1] == "odin@local" and offer[3] == {"role": "reviewer", "deadline_seconds": 1800}
    summons = [c for c in s.calls if c[0] == "summon"]
    assert [c[1] for c in summons] == [handle, worker_handle("ollama", run["id"])]
    assert summons[0][2] == f"{run['id']}.claude"  # idempotent operation id
    assert "Rules for this room:\n- Read-only." in summons[0][3]
    assert sorted(c[1] for c in s.calls if c[0] == "finalize") == ["agt_1", "agt_2"]
    assert ("update_room", "room_1", {"archived": True}) in s.calls  # archive_after 0
    assert run["archived_at"]


def test_resume_does_not_duplicate(make):
    stub = Stub()
    r = make(stub)
    run, _ = r.create("schedule", "triage", "review", "Goal")
    # simulate a crash after the room, the notes, the offer and the first worker
    r._room(r.get(run["id"]), r.definitions().templates["review"])
    t = r.definitions().templates["review"]
    r._seed(r.get(run["id"]), t)
    r._peers(r.get(run["id"]), t)
    stub.fail_summon = "ollama"
    with pytest.raises(RuntimeError):
        r._workers(r.get(run["id"]), t)
    stub.fail_summon = None
    run = r.execute(run["id"])
    assert run["state"] == "done"
    assert stub.names().count("create_room") == 1
    assert stub.names().count("post") == 1 and stub.names().count("offer") == 1
    assert [c[1].split("-")[0] for c in stub.calls if c[0] == "summon"] == ["claude", "ollama"]


def test_max_duration_stops_the_workers(make):
    r = make(Stub(polls=10_000))
    run, _ = r.create("schedule", "triage", "review", "Goal")
    run = r.execute(run["id"])
    assert run["state"] == "done" and run["error"] == "stopped 2 worker(s) at max_duration"
    stops = [c for c in r.client.calls if c[0] == "stop"]
    assert {c[2] for c in stops} == {"dispatch_max_duration"} and len(stops) == 2
    assert r.clock_state["now"] >= T0 + timedelta(hours=1)


def test_failure_stops_started_workers_and_records_the_error(make):
    r = make(Stub(polls=100, fail_summon="ollama"))
    run, _ = r.create("webhook", "ask", "review", "Goal")
    run = r.execute(run["id"])
    assert run["state"] == "failed" and "no agentd with capacity" in run["error"]
    assert [c[1:] for c in r.client.calls if c[0] == "stop"] == [("agt_1", "dispatch_run_failed")]
    assert run["archived_at"]  # the room doesn't linger


def test_archive_later(make):
    r = make(template={"room": {"archive_after": "2h"}})
    run, _ = r.create("schedule", "triage", "review", "Goal")
    run = r.execute(run["id"])
    assert run["state"] == "done" and run["archived_at"] is None
    r.clock_state["now"] += timedelta(hours=3)
    assert r.archive_due() == 1
    assert r.get(run["id"])["archived_at"]


def test_overlap_policy(make):
    r = make()
    first, _ = r.create("schedule", "triage", "review", "Goal")
    second, _ = r.create("schedule", "triage", "review", "Goal")
    assert second["state"] == "skipped" and "still active" in second["error"]
    r2 = make(on_overlap="allow", max_concurrent=2)
    assert r2.create("schedule", "triage", "review", "Goal")[0]["state"] == "pending"
    assert r2.create("schedule", "triage", "review", "Goal")[0]["state"] == "skipped"


def test_webhook_delivery_is_deduplicated(make):
    r = make()
    a, created_a = r.create("webhook", "ask", "review", "Goal", delivery_id="d-1")
    b, created_b = r.create("webhook", "ask", "review", "Goal", delivery_id="d-1")
    assert created_a and not created_b and a["id"] == b["id"]


def test_a_removed_template_fails_the_run(make, tmp_path):
    r = make()
    run, _ = r.create("schedule", "triage", "review", "Goal")
    r.definitions = lambda: Definitions.model_validate({"templates": {}})
    assert r.execute(run["id"])["state"] == "failed"


class SeqStub(Stub):
    """Records, at each summon, which earlier sessions had already finished."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.finished_at_summon: list[list[str]] = []

    def summon(self, room_url, task, **kw):
        done = [sid for sid, seq in self.status.items() if seq == ["completed"]]
        self.finished_at_summon.append(done)
        return super().summon(room_url, task, **kw)


def test_sequential_workers_start_after_the_previous_one_ends(make):
    stub = SeqStub(polls=2)
    r = make(stub, order="sequential")
    run, _ = r.create("schedule", "triage", "review", "Goal")
    run = r.execute(run["id"])
    assert run["state"] == "done" and run["error"] is None
    # claude was summoned first; ollama only once claude's session had completed
    assert stub.finished_at_summon == [[], ["agt_1"]]


def test_parallel_is_the_default(make):
    stub = SeqStub(polls=2)
    r = make(stub)
    run, _ = r.create("schedule", "triage", "review", "Goal")
    r.execute(run["id"])
    assert stub.finished_at_summon == [[], []]  # both summoned before either finished


def test_sequential_resumes_mid_sequence(make):
    stub = SeqStub(polls=1)
    r = make(stub, order="sequential")
    run, _ = r.create("schedule", "triage", "review", "Goal")
    t = r.definitions().templates["review"]
    r._room(r.get(run["id"]), t)
    r._seed(r.get(run["id"]), t)
    r._peers(r.get(run["id"]), t)
    stub.fail_summon = "ollama"
    with pytest.raises(RuntimeError):  # crashed after the first worker finished
        r._workers(r.get(run["id"]), t)
    stub.fail_summon = None
    run = r.execute(run["id"])
    assert run["state"] == "done"
    assert [c[1].split("-")[0] for c in stub.calls if c[0] == "summon"] == ["claude", "ollama"]


def test_sequential_deadline_stops_the_line(make):
    stub = SeqStub(polls=10_000)  # the first worker never finishes
    r = make(stub, order="sequential")
    run, _ = r.create("schedule", "triage", "review", "Goal")
    run = r.execute(run["id"])
    assert run["state"] == "done"
    assert run["error"] == "max_duration reached before 1 worker(s) were summoned"
    assert [c[0] for c in stub.calls].count("summon") == 1
    assert [c[2] for c in stub.calls if c[0] == "stop"] == ["dispatch_max_duration"]


def test_a_worker_can_mount_a_knowledge_base(make):
    workers = [
        {"name": "claude", "worker_type": "claude", "profile": "kb", "workspace": "/srv/kb/vpn"},
        {"name": "ollama", "worker_type": "ollama", "profile": "ro"},
    ]
    r = make(template={"workers": workers})
    run, _ = r.create("manual", "ask", "review", "How is a client added?")
    assert r.execute(run["id"])["state"] == "done"
    by_name = {kw["name"].split("-")[0]: kw for kw in r.client.summon_kw}
    assert by_name["claude"]["workspace_path"] == "/srv/kb/vpn"
    assert "workspace_path" not in by_name["ollama"]


def test_a_workspace_must_be_absolute():
    from pydantic import ValidationError

    from dispatchd.config import WorkerSpec

    with pytest.raises(ValidationError):
        WorkerSpec(name="c", worker_type="claude", profile="kb", workspace="kb/vpn")
