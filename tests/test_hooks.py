"""Webhook ingress through the app (stub roomomatic client): signing, replay protection,
delivery dedupe, strict bodies, size and rate limits, and what reaches the agents."""

import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from test_runner import TEMPLATE, Stub

from dispatchd import hooks
from dispatchd.app import create_app
from dispatchd.config import Settings

SECRET = "s3cret-for-tests"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK_ASK", SECRET)
    cfg = tmp_path / "dispatch.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "templates": {"review": TEMPLATE},
                "webhooks": {
                    "ask": {
                        "secret_env": "HOOK_ASK",
                        "use": "review",
                        "max_prompt_bytes": 200,
                        "rate_per_hour": 3,
                        "task_template": "External question (untrusted): {prompt}",
                    },
                    "off": {"secret_env": "HOOK_ASK", "use": "review", "enabled": False},
                    "nosecret": {"secret_env": "HOOK_UNSET", "use": "review"},
                },
            }
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        config_path=cfg,
        lobby_url="http://l",
        poll_seconds=0.01,
        tick_seconds=60,
    )
    application = create_app(settings, client=Stub())
    with TestClient(application) as c:
        c.app_ = application
        yield c


def post(c, body, *, name="ask", delivery="d-1", secret=SECRET, ts=None, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    ts = int(time.time()) if ts is None else ts
    headers = {
        "X-Rom-Timestamp": str(ts),
        "X-Rom-Signature": hooks.sign(secret, ts, data),
        "content-type": "application/json",
    }
    if delivery is not None:
        headers["X-Rom-Delivery"] = delivery
    return c.post(f"/v1/hooks/{name}", content=data, headers=headers)


def wait_done(c, run_id):
    for _ in range(300):
        run = c.app_.state.runner.get(run_id)
        if run["state"] in ("done", "failed"):
            return run
        time.sleep(0.01)
    raise AssertionError(run)


def test_a_signed_request_starts_a_run(app):
    r = post(app, {"prompt": "Is {SQLite} enough for v1?", "goal": "Storage review"})
    assert r.status_code == 202
    body = r.json()
    assert body["status_url"] == f"/v1/hooks/ask/runs/{body['id']}"
    run = wait_done(app, body["id"])
    assert run["state"] == "done" and run["source"] == "webhook" and run["goal"] == "Storage review"
    # the prompt only lands inside the operator's template, followed by the rules
    assert run["task"].startswith("External question (untrusted): Is {SQLite} enough for v1?")
    assert "Rules for this room:\n- Read-only." in run["task"]
    stub = app.app_.state.runner.client
    assert ("put_note", "goal", "Storage review") in stub.calls


def test_goal_defaults_to_the_prompt_s_first_line(app):
    run = wait_done(app, post(app, {"prompt": "First line\nmore"}).json()["id"])
    assert run["goal"] == "First line"


def test_a_repeated_delivery_returns_the_same_run(app):
    a = post(app, {"prompt": "hi"})
    b = post(app, {"prompt": "hi"})
    assert (a.status_code, b.status_code) == (202, 200) and a.json()["id"] == b.json()["id"]


@pytest.mark.parametrize(
    "kw, status, detail",
    [
        ({"secret": "wrong"}, 401, "bad signature"),
        ({"ts": int(time.time()) - 3600}, 401, "5 minutes"),
        ({"delivery": None}, 400, "X-Rom-Delivery"),
        ({"delivery": "has spaces"}, 400, "X-Rom-Delivery"),
        ({"name": "nope"}, 404, "no such webhook"),
        ({"name": "off"}, 404, "no such webhook"),
        ({"name": "nosecret"}, 503, "secret"),
    ],
)
def test_rejections(app, kw, status, detail):
    r = post(app, {"prompt": "hi"}, **kw)
    assert r.status_code == status and detail in r.json()["detail"]
    assert app.app_.state.runner.recent() == []  # nothing started


def test_unsigned_request_is_refused(app):
    r = app.post("/v1/hooks/ask", json={"prompt": "hi"}, headers={"X-Rom-Delivery": "d"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "body, status, detail",
    [
        ({"prompt": "hi", "template": "admin-everything"}, 422, "only prompt and goal"),
        ({"prompt": "hi", "workers": []}, 422, "unknown keys"),
        ({"goal": "no prompt"}, 422, "prompt is required"),
        ({"prompt": "   "}, 422, "prompt is required"),
        ({"prompt": "x" * 201}, 413, "over 200 bytes"),
        (["not", "an", "object"], 422, "JSON object"),
    ],
)
def test_strict_bodies(app, body, status, detail):
    r = post(app, body)
    assert r.status_code == status and detail in r.json()["detail"]


def test_oversized_body_is_refused_before_parsing(app):
    r = post(app, None, raw=b'{"prompt": "' + b"x" * 10_000 + b'"}')
    assert r.status_code == 413 and "body is over" in r.json()["detail"]


def test_rate_limit_but_retries_still_answered(app):
    for i in range(3):
        assert post(app, {"prompt": f"q{i}"}, delivery=f"d{i}").status_code == 202
    r = post(app, {"prompt": "q4"}, delivery="d4")
    assert r.status_code == 429 and r.headers["retry-after"] == "300"
    assert post(app, {"prompt": "q0"}, delivery="d0").status_code == 200  # a retry, not new


def test_status_endpoint_is_signed(app):
    run_id = post(app, {"prompt": "hi"}).json()["id"]
    wait_done(app, run_id)
    ts = int(time.time())
    url = f"/v1/hooks/ask/runs/{run_id}"
    ok = app.get(
        url, headers={"X-Rom-Timestamp": str(ts), "X-Rom-Signature": hooks.sign(SECRET, ts, b"")}
    )
    assert ok.status_code == 200 and ok.json()["state"] == "done"
    assert app.get(url).status_code == 401
    other = app.get(
        "/v1/hooks/ask/runs/run_NOPE",
        headers={"X-Rom-Timestamp": str(ts), "X-Rom-Signature": hooks.sign(SECRET, ts, b"")},
    )
    assert other.status_code == 404


def test_render_is_plain_substitution():
    assert hooks.render("Q: {prompt} {other}", " a {b} ") == "Q: a {b} {other}"
