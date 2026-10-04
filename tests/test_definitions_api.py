"""Templates, schedules and webhooks through the operator API, merged with the config
file's (which stay read-only here) and validated as one set."""

import json
import os
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from test_operator import BASE, DOMAIN, ISSUER, FakeLobby
from test_runner import TEMPLATE, Stub

from dispatchd import hooks
from dispatchd.app import create_app
from dispatchd.config import Settings
from dispatchd.verify import TokenVerifier


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_HOOK", "file-secret")
    cfg = tmp_path / "dispatch.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "templates": {"review": TEMPLATE},
                "schedules": {
                    "triage": {"cron": "0 7 * * 1-5", "use": "review", "goal": "Triage."}
                },
                "webhooks": {"file-hook": {"secret_env": "FILE_HOOK", "use": "review"}},
            }
        )
    )
    lobby = FakeLobby()
    settings = Settings(
        data_dir=tmp_path / "data",
        config_path=cfg,
        lobby_url=ISSUER,
        lobby_domain=DOMAIN,
        base_url=BASE,
        poll_seconds=0.01,
        tick_seconds=0.05,
        operators=(f"ops@{DOMAIN}",),
    )

    def make():
        verifier = TokenVerifier(issuer=ISSUER, domain=DOMAIN, audience=BASE, fetch_jwks=lobby.jwks)
        return TestClient(create_app(settings, client=Stub(), verifier=verifier))

    return {"make": make, "ops": lobby.headers("ops"), "lobby": lobby, "cfg": cfg}


@pytest.fixture
def c(env):
    with env["make"]() as client:
        yield client


def test_listing_marks_where_definitions_come_from(c, env):
    ops = env["ops"]
    assert [(t["name"], t["source"]) for t in c.get("/v1/templates", headers=ops).json()] == [
        ("review", "file")
    ]
    hook = c.get("/v1/webhooks/file-hook", headers=ops).json()
    assert hook["source"] == "file" and hook["url_path"] == "/v1/hooks/file-hook"
    assert "secret" not in json.dumps(hook).replace("secret_env", "")
    sched = c.get("/v1/schedules/triage", headers=ops).json()
    assert sched["source"] == "file" and sched["recent_runs"] == []


def test_create_a_schedule(c, env):
    ops = env["ops"]
    r = c.put(
        "/v1/schedules/nightly",
        headers=ops,
        json={"cron": "30 2 * * *", "timezone": "UTC", "use": "review", "goal": "Sweep."},
    )
    assert r.status_code == 200 and r.json()["source"] == "api"
    assert r.json()["created_by"] == f"ops@{DOMAIN}"
    names = [s["name"] for s in c.get("/v1/schedules", headers=ops).json()]
    assert names == ["triage", "nightly"]
    for _ in range(100):  # the scheduler loop picks it up
        if c.get("/v1/schedules/nightly", headers=ops).json()["next_fire"]:
            break
        time.sleep(0.02)
    assert c.get("/v1/schedules/nightly", headers=ops).json()["next_fire"].endswith("02:30:00.000Z")


@pytest.mark.parametrize(
    "body, detail",
    [
        ({"cron": "0 7 * *", "use": "review", "goal": "g"}, "5 fields"),
        ({"cron": "0 7 * * *", "use": "missing", "goal": "g"}, "unknown template"),
        ({"cron": "0 7 * * *", "use": "review", "goal": "g", "shell": True}, "Extra inputs"),
    ],
)
def test_invalid_schedules_are_refused(c, env, body, detail):
    r = c.put("/v1/schedules/bad", headers=env["ops"], json=body)
    assert r.status_code == 422 and detail in r.json()["detail"]


def test_file_definitions_are_read_only_here(c, env):
    ops = env["ops"]
    r = c.put(
        "/v1/schedules/triage",
        headers=ops,
        json={"cron": "0 8 * * *", "use": "review", "goal": "Changed."},
    )
    assert r.status_code == 409 and "config file" in r.json()["detail"]
    assert c.delete("/v1/templates/review", headers=ops).status_code == 409
    assert c.post("/v1/webhooks/file-hook/rotate-secret", headers=ops).status_code == 409


def sign_post(c, name, secret, prompt="Is SQLite enough?", delivery="d1"):
    body = json.dumps({"prompt": prompt}).encode()
    ts = int(time.time())
    return c.post(
        f"/v1/hooks/{name}",
        content=body,
        headers={
            "X-Rom-Timestamp": str(ts),
            "X-Rom-Signature": hooks.sign(secret, ts, body),
            "X-Rom-Delivery": delivery,
            "content-type": "application/json",
        },
    )


def test_create_a_webhook_with_a_generated_secret(c, env):
    ops = env["ops"]
    r = c.post(
        "/v1/webhooks/ask",
        headers=ops,
        json={"use": "review", "task_template": "Q: {prompt}", "rate_per_hour": 5},
    )
    assert r.status_code == 201
    created = r.json()
    secret = created["secret"]
    assert secret.startswith("whsec_") and "HMAC-SHA256" in created["signing"]
    assert "secret" not in c.get("/v1/webhooks/ask", headers=ops).json()  # shown once
    assert sign_post(c, "ask", secret).status_code == 202
    assert c.post("/v1/webhooks/ask", headers=ops, json={"use": "review"}).status_code == 409
    rotated = c.post("/v1/webhooks/ask/rotate-secret", headers=ops).json()["secret"]
    assert sign_post(c, "ask", secret, delivery="d2").status_code == 401  # old secret dead
    assert sign_post(c, "ask", rotated, delivery="d3").status_code == 202
    detail = c.get("/v1/webhooks/ask", headers=ops).json()
    assert len(detail["recent_runs"]) == 2


def test_api_webhooks_cannot_name_an_env_secret(c, env):
    r = c.post(
        "/v1/webhooks/x", headers=env["ops"], json={"use": "review", "secret_env": "FILE_HOOK"}
    )
    assert r.status_code == 422 and "API generates" in r.json()["detail"]


def test_templates_and_deletion_rules(c, env):
    ops = env["ops"]
    tmpl = {
        "workers": [{"name": "codex", "worker_type": "codex", "profile": "ro"}],
        "run": {"max_duration": "20m", "order": "sequential"},
    }
    assert c.put("/v1/templates/solo", headers=ops, json=tmpl).json()["run"]["max_duration"] == 1200
    c.put("/v1/schedules/s", headers=ops, json={"cron": "0 9 * * *", "use": "solo", "goal": "g"})
    r = c.delete("/v1/templates/solo", headers=ops)
    assert r.status_code == 422 and "unknown template" in r.json()["detail"]  # still in use
    assert c.delete("/v1/schedules/s", headers=ops).status_code == 204
    assert c.delete("/v1/templates/solo", headers=ops).status_code == 204
    assert c.get("/v1/templates/solo", headers=ops).status_code == 404


def test_api_definitions_survive_a_restart(env):
    with env["make"]() as c:
        c.put(
            "/v1/schedules/kept",
            headers=env["ops"],
            json={"cron": "0 9 * * *", "use": "review", "goal": "g"},
        )
    with env["make"]() as c:
        assert c.get("/v1/schedules/kept", headers=env["ops"]).json()["source"] == "api"


def test_a_file_edit_colliding_with_the_api_is_reported(c, env):
    c.put(
        "/v1/schedules/clash",
        headers=env["ops"],
        json={"cron": "0 9 * * *", "use": "review", "goal": "g"},
    )
    raw = yaml.safe_load(env["cfg"].read_text())
    raw["schedules"]["clash"] = {"cron": "0 10 * * *", "use": "review", "goal": "file"}
    env["cfg"].write_text(yaml.safe_dump(raw))
    os.utime(env["cfg"], (1, 1))
    r = c.get("/readyz")
    assert (
        r.status_code == 503
        and "both the config file and the API" in (r.json()["checks"]["config"]["error"])
    )


def test_only_operators(c, env):
    assert c.get("/v1/templates", headers=env["lobby"].headers("mallory")).status_code == 403
    assert c.put("/v1/schedules/x", json={}).status_code == 401


def test_a_restore_never_revives_a_retired_secret(c, env, tmp_path):
    """Rotated or deleted after the snapshot: the old secret must not work again."""
    from dispatchd import recovery

    ops = env["ops"]
    settings = c.app.state.settings
    old = c.post("/v1/webhooks/ask", headers=ops, json={"use": "review"}).json()["secret"]
    kept = c.post("/v1/webhooks/kept", headers=ops, json={"use": "review"}).json()["secret"]
    gone = c.post("/v1/webhooks/gone", headers=ops, json={"use": "review"}).json()["secret"]
    recovery.backup(settings, tmp_path / "bk")
    c.post("/v1/webhooks/ask/rotate-secret", headers=ops)
    c.delete("/v1/webhooks/gone", headers=ops)

    report = recovery.restore(settings, tmp_path / "bk", force=True)
    assert sorted(report["invalidated"]["webhook_secrets_cleared"]) == ["ask", "gone"]
    with env["make"]() as c2:
        assert sign_post(c2, "ask", old).status_code == 503  # until an operator rotates it
        assert sign_post(c2, "gone", gone).status_code == 503
        assert sign_post(c2, "kept", kept).status_code == 202
        fresh = c2.post("/v1/webhooks/ask/rotate-secret", headers=ops).json()["secret"]
        assert sign_post(c2, "ask", fresh, delivery="d9").status_code == 202
