"""The operator API (lobbyd tokens from listed operators only) and /metrics."""

import time
import uuid

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from test_runner import TEMPLATE, Stub

from dispatchd.app import create_app
from dispatchd.config import Settings
from dispatchd.verify import TokenVerifier

ISSUER, DOMAIN, BASE = "http://lobby.test", "test", "http://dispatch.test"


class FakeLobby:
    kid = "k"

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()

    def jwks(self) -> dict:
        jwk = jwt.algorithms.OKPAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "alg": "EdDSA", "use": "sig"}]}

    def headers(self, name, *, scope="agent", aud=BASE) -> dict:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": f"{name}@{DOMAIN}",
            "aud": aud,
            "scope": scope,
            "iat": now,
            "nbf": now,
            "exp": now + 900,
            "jti": uuid.uuid4().hex,
        }
        token = jwt.encode(claims, self.key, algorithm="EdDSA", headers={"kid": self.kid})
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def lobby():
    return FakeLobby()


@pytest.fixture
def client(tmp_path, lobby):
    cfg = tmp_path / "dispatch.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "templates": {"review": TEMPLATE},
                "schedules": {
                    "triage": {"cron": "0 7 * * 1-5", "use": "review", "goal": "Triage."}
                },
            }
        )
    )
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
    verifier = TokenVerifier(issuer=ISSUER, domain=DOMAIN, audience=BASE, fetch_jwks=lobby.jwks)
    app = create_app(settings, client=Stub(), verifier=verifier)
    with TestClient(app) as c:
        yield c


def test_only_listed_operators_get_in(client, lobby):
    assert client.get("/v1/runs").status_code == 401
    assert client.get("/v1/runs", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/v1/runs", headers=lobby.headers("mallory")).status_code == 403
    assert (
        client.get("/v1/runs", headers=lobby.headers("ops", aud="http://other")).status_code == 401
    )  # token for another service
    assert client.get("/v1/runs", headers=lobby.headers("ops")).status_code == 200


def test_trigger_list_and_inspect(client, lobby):
    ops = lobby.headers("ops")
    r = client.post("/v1/schedules/triage/run", headers=ops)
    assert r.status_code == 202 and r.json()["source"] == "manual"
    run_id = r.json()["id"]
    for _ in range(300):
        run = client.get(f"/v1/runs/{run_id}", headers=ops).json()
        if run["state"] == "done":
            break
        time.sleep(0.01)
    assert run["state"] == "done" and run["room_url"]
    assert [x["id"] for x in client.get("/v1/runs", headers=ops).json()] == [run_id]
    sched = client.get("/v1/schedules", headers=ops).json()
    assert sched[0]["name"] == "triage" and sched[0]["next_fire"]  # the loop scheduled it
    assert client.post("/v1/schedules/nope/run", headers=ops).status_code == 404
    assert client.get("/v1/runs/run_NOPE", headers=ops).status_code == 404


def test_metrics(client, lobby):
    client.post("/v1/schedules/triage/run", headers=lobby.headers("ops"))
    text = client.get("/metrics").text
    assert "dispatchd_schedules 1" in text and "dispatchd_config_ok 1" in text
    assert "dispatchd_runs_" in text
