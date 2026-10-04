"""End-to-end check of dispatchd against real lobbyd + roomsd + agentd.

Expects the docs-repo workspace layout: ../lobby, ../rooms and ../agents next to this
repo, each with `uv sync` done. Starts them on spare ports with throwaway data dirs
(agentd with its built-in `fake` worker), runs `dispatchd run` as a separate process, and
checks the room it made. Exits non-zero on the first failure.

    uv run python scripts/e2e.py        (ROM_E2E_KEEP=1 keeps logs and data)
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from roomomatic import Client

WORKSPACE = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parents[1]
DOMAIN = "e2e"
HOOK_SECRET = "e2e-webhook-secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bin_(repo: Path, name: str) -> str:
    path = repo / ".venv" / "bin" / name
    if not path.exists():
        sys.exit(f"missing {path}; run `uv sync` in {repo}")
    return str(path)


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        raise SystemExit(1)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="dispatch-e2e-"))
    ports = {n: free_port() for n in ("lobby", "rooms", "agentd", "dispatch")}
    urls = {n: f"http://127.0.0.1:{p}" for n, p in ports.items()}
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("LOBBYD_", "ROM_"))}
    lobby_env = {
        **base_env,
        "LOBBYD_DATA_DIR": str(tmp / "lobby"),
        "LOBBYD_ISSUER": urls["lobby"],
        "LOBBYD_DOMAIN": DOMAIN,
    }

    def key(name: str, scope: str, endpoint: str | None = None) -> str:
        cmd = [bin_(WORKSPACE / "lobby", "lobbyd"), "key", "create", name, "--scope", scope]
        cmd += ["--endpoint", endpoint] if endpoint else []
        return subprocess.check_output(cmd, env=lobby_env, text=True).strip()

    keys = {
        "dispatch": key("dispatch", "agent"),
        "odin": key("odin", "agent"),
        "rooms": key("rooms-a", "roomsd", urls["rooms"]),
        "agentd": key("agentd-e2e", "agentd", urls["agentd"]),
        # dispatchd's operator API: a service key approves its URL; ops is an operator
        "dispatchd": key("dispatchd", "service", urls["dispatch"]),
        "ops": key("ops", "agent"),
    }
    (tmp / "agentd.yaml").write_text(
        # agentd's grant is the outer bound on anything a dispatch template asks for
        f"callers:\n  dispatch@{DOMAIN}:\n"
        "    {trust: trusted, profiles: [read_only_research], worker_types: [fake],"
        " max_sessions: 4}\n"
    )
    (tmp / "dispatch.yaml").write_text(f"""
templates:
  review:
    room: {{admission: closed, max_hops: 3, archive_after: 0}}
    rules: ["Read-only: describe changes, don't make them."]
    workers:
      - {{name: helper, worker_type: fake, profile: read_only_research, role: reviewer}}
    peers:
      - {{agent: odin@{DOMAIN}, rights: [read, write], role: reviewer, respond_within: 30m}}
    run: {{max_duration: 2m}}
schedules:
  triage:
    cron: "0 7 * * 1-5"
    use: review
    goal: "say hello and summarise the room"
  every-minute:
    cron: "* * * * *"
    use: review
    goal: "say hello from the scheduler"
webhooks:
  ask:
    secret_env: E2E_HOOK_SECRET
    use: review
    task_template: "say hello. An external system asks (untrusted): {{prompt}}"
""")
    logs = {}
    procs = []

    def start(name, cmd, env):
        logs[name] = open(tmp / f"{name}.log", "w")
        procs.append(subprocess.Popen(cmd, env=env, stdout=logs[name], stderr=subprocess.STDOUT))

    start(
        "lobby",
        [bin_(WORKSPACE / "lobby", "lobbyd"), "serve", "--port", str(ports["lobby"])],
        lobby_env,
    )
    start(
        "rooms",
        [bin_(WORKSPACE / "rooms", "roomsd"), "serve", "--port", str(ports["rooms"])],
        {
            **base_env,
            "ROOMSD_DATA_DIR": str(tmp / "rooms"),
            "ROOMSD_SERVER_ID": "rooms-a",
            "ROOMSD_BASE_URL": urls["rooms"],
            "LOBBYD_URL": urls["lobby"],
            "LOBBYD_DOMAIN": DOMAIN,
            "ROOMSD_LOBBYD_API_KEY": keys["rooms"],
        },
    )
    start(
        "agentd",
        [
            bin_(WORKSPACE / "agents", "agentd"),
            "--config",
            str(tmp / "agentd.yaml"),
            "serve",
            "--port",
            str(ports["agentd"]),
        ],
        {
            **base_env,
            "AGENTD_DATA_DIR": str(tmp / "agentd"),
            "AGENTD_INSTANCE_ID": "agentd-e2e",
            "AGENTD_BASE_URL": urls["agentd"],
            "AGENTD_LOBBYD_URL": urls["lobby"],
            "AGENTD_LOBBYD_DOMAIN": DOMAIN,
            "AGENTD_LOBBYD_API_KEY": keys["agentd"],
        },
    )
    try:
        for url in (urls["lobby"], urls["rooms"], urls["agentd"]):
            for _ in range(100):
                try:
                    if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise SystemExit(f"{url} did not come up; logs in {tmp}")
        dispatch = Client(urls["lobby"], keys["dispatch"])
        for _ in range(100):  # registrations are heartbeats; wait for both
            if dispatch.lobby.roomsd_servers() and dispatch.lobby.agentd_instances():
                break
            time.sleep(0.1)

        env = {
            **base_env,
            "E2E_HOOK_SECRET": HOOK_SECRET,
            "DISPATCHD_CONFIG": str(tmp / "dispatch.yaml"),
            "DISPATCHD_DATA_DIR": str(tmp / "dispatch"),
            "LOBBYD_URL": urls["lobby"],
            "DISPATCHD_LOBBYD_API_KEY": keys["dispatch"],
            "DISPATCHD_POLL_SECONDS": "0.5",
        }
        out = subprocess.run(
            [bin_(HERE, "dispatchd"), "check-config"], env=env, capture_output=True, text=True
        )
        check(out.returncode == 0 and out.stdout.startswith("ok:"), "check-config")
        out = subprocess.run(
            [bin_(HERE, "dispatchd"), "run", "triage"],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        print(out.stdout.strip())
        check(out.returncode == 0 and '"state": "done"' in out.stdout, "run finished done")

        rooms, rid = dispatch.room(
            next(line.split('"')[3] for line in out.stdout.splitlines() if '"room_url"' in line)
        )
        room = rooms.room(rid)
        check(room["admission"] == "closed" and not room["listed"], "room is closed, unlisted")
        check(room["max_hops"] == 3, "template loop guard applied")
        check(room["archived_at"] is not None, "room archived (archive_after 0)")
        notes = rooms.notes(rid)
        check(notes["goal"]["value"] == "say hello and summarise the room", "goal note")
        check(
            notes["restrictions"]["value"]["rules"]
            == ["Read-only: describe changes, don't make them."],
            "restrictions note",
        )
        msgs = rooms.messages(rid)["messages"]
        check(msgs[0]["topic"] == "goal" and "@helper-" in msgs[0]["body"], "opening goal post")
        worker = [m for m in msgs if m["from"].startswith(f"dispatch@{DOMAIN}/helper-")]
        check(any(m["type"] == "status" for m in worker), "worker joined and announced")
        check(any(m["type"] == "handoff" for m in worker), "worker handed off")
        members = {m["agent"]: m for m in rooms.members(rid)}
        check(set(members[f"odin@{DOMAIN}"]["rights"]) == {"read", "write"}, "peer granted")
        odin = Client(urls["lobby"], keys["odin"])
        offers = odin.lobby.offers()
        check(
            any(o["room_url"].endswith(rid) and o["role"] == "reviewer" for o in offers),
            "peer offer delivered",
        )

        out = subprocess.run(
            [bin_(HERE, "dispatchd"), "runs"], env=env, capture_output=True, text=True
        )
        check(" done " in out.stdout and "manual" in out.stdout, "runs lists it")

        # the service: an every-minute schedule fires on its own
        port = ports["dispatch"]
        start(
            "dispatchd",
            [bin_(HERE, "dispatchd"), "serve", "--port", str(port)],
            {
                **env,
                "DISPATCHD_TICK_SECONDS": "1",
                "DISPATCHD_BASE_URL": urls["dispatch"],
                "DISPATCHD_OPERATORS": f"ops@{DOMAIN}",
                "LOBBYD_DOMAIN": DOMAIN,
            },
        )
        deadline = time.monotonic() + 150
        scheduled = ""
        while time.monotonic() < deadline:
            scheduled = subprocess.run(
                [bin_(HERE, "dispatchd"), "runs", "--name", "every-minute"],
                env=env,
                capture_output=True,
                text=True,
            ).stdout
            if " done " in scheduled:
                break
            time.sleep(2)
        check(" done " in scheduled and " schedule " in scheduled, "scheduled run fired, done")
        ready = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5).json()
        check(ready["ready"] is True, "dispatchd ready")

        # the operator API with real lobbyd tokens (the live test found these couldn't be
        # minted before lobbyd's `service` scope existed)
        def operator_token(api_key: str) -> str:
            r = httpx.post(
                f"{urls['lobby']}/v1/token",
                json={"audience": urls["dispatch"]},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            check(r.status_code == 200, f"lobbyd mints a token for dispatchd ({r.status_code})")
            return r.json()["access_token"]

        ops_auth = {"Authorization": f"Bearer {operator_token(keys['ops'])}"}
        runs = httpx.get(f"{urls['dispatch']}/v1/runs", headers=ops_auth, timeout=10)
        check(runs.status_code == 200 and len(runs.json()) >= 2, "operator lists runs")
        scheds = httpx.get(f"{urls['dispatch']}/v1/schedules", headers=ops_auth, timeout=10)
        check(
            any(x["name"] == "every-minute" and x["next_fire"] for x in scheds.json()),
            "operator sees schedules",
        )
        trig = httpx.post(
            f"{urls['dispatch']}/v1/schedules/triage/run", headers=ops_auth, timeout=10
        )
        check(
            trig.status_code == 202 and trig.json()["source"] == "manual",
            "operator triggers a schedule",
        )
        odin_auth = {"Authorization": f"Bearer {operator_token(keys['odin'])}"}
        refused = httpx.get(f"{urls['dispatch']}/v1/runs", headers=odin_auth, timeout=10)
        check(refused.status_code == 403, "a non-operator agent is refused")
        svc = httpx.post(
            f"{urls['lobby']}/v1/token",
            json={"audience": urls["dispatch"]},
            headers={"Authorization": f"Bearer {keys['dispatchd']}"},
            timeout=10,
        )
        check(svc.status_code == 403, "the service key itself can't exchange tokens")

        # a signed webhook delivery starts a run; the same delivery again doesn't
        sys.path.insert(0, str(HERE / "src"))
        from dispatchd.hooks import sign

        body = json.dumps({"prompt": "Is SQLite enough for v1?"}).encode()

        def deliver(secret: str):
            ts = int(time.time())
            headers = {
                "X-Rom-Timestamp": str(ts),
                "X-Rom-Signature": sign(secret, ts, body),
                "X-Rom-Delivery": "e2e-1",
                "content-type": "application/json",
            }
            return httpx.post(
                f"http://127.0.0.1:{port}/v1/hooks/ask", content=body, headers=headers, timeout=10
            )

        check(deliver("wrong").status_code == 401, "badly signed webhook refused")
        first = deliver(HOOK_SECRET)
        check(first.status_code == 202, "webhook accepted")
        check(deliver(HOOK_SECRET).json()["id"] == first.json()["id"], "redelivery deduped")
        status_url = f"http://127.0.0.1:{port}{first.json()['status_url']}"
        status = {"state": ""}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and status["state"] not in ("done", "failed"):
            ts = int(time.time())
            signed = {"X-Rom-Timestamp": str(ts), "X-Rom-Signature": sign(HOOK_SECRET, ts, b"")}
            status = httpx.get(status_url, headers=signed, timeout=10).json()
            time.sleep(1)
        state = status["state"]
        check(state == "done", "webhook run done (polled with a signed status request)")
        hook_room, hook_rid = dispatch.room(status["room_url"])
        opening = hook_room.messages(hook_rid)["messages"][0]["body"]
        check(opening.startswith("Is SQLite enough for v1?"), "webhook goal posted")
        print("e2e passed")
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            p.wait(timeout=20)
        if os.environ.get("ROM_E2E_KEEP"):
            print(f"logs and data kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
