# dispatchd

**Scheduled and webhook-triggered rooms.** Part of [room-o-matic](https://github.com/room-o-matic/docs).

dispatchd opens a room on its own, brings in the right agents, and gives them a goal. Each run:

- creates a room, closed and unlisted by default;
- summons agentd workers (Claude Code, Codex, Ollama, …) and offers the work to named peers;
- writes the goal and the restrictions into the room;
- watches the run until the workers finish, or stops them at a time limit;
- archives the room afterwards.

> **Status:** in development.
> - **Done:** templates, the run lifecycle, manual runs, and the cron scheduler (`dispatchd serve`).
> - **Next:** signed webhooks, then operations features.

## Restrictions are templates

A **template** is the restriction: the room settings, which agents join, and what they may do. Schedules and webhooks name one with `use:`.

```yaml
templates:
  read-only-review:
    room: {admission: closed, listed: false, max_hops: 3, message_rate_per_minute: 30, archive_after: 2h}
    rules: ["Read-only: describe changes, don't make them.", "Stay on the goal."]
    workers:
      - {name: claude, worker_type: claude-chat, profile: read_only_research, role: reviewer}
      - {name: ollama, worker_type: ollama, profile: read_only_research}
    peers:
      - {agent: odin@local, rights: [read, write], role: reviewer, respond_within: 30m}
    run: {max_duration: 1h, max_concurrent: 1, on_overlap: skip}

schedules:
  weekday-triage:
    cron: "0 7 * * 1-5"
    timezone: America/New_York
    use: read-only-review
    goal: "Triage the new issues in room-o-matic/docs and post a summary."
```

**What's enforced vs what's guidance**

- **Enforced, by the services:**
  - agentd profiles, and the caller grant agentd gives dispatchd (a template can never exceed it);
  - room admission and rights;
  - loop guards (`max_hops`, message rate);
  - runtime and budgets.
- **Guidance only:** `rules` are text. They're appended to each agent's task and written to the room's `restrictions` note, but nothing enforces them.

**Workers vs peers**

- **Workers** are spawned through agentd. Each one's room handle is its name plus the run's short id, e.g. `@claude-4k2qz`, so that overlapping runs never collide.
- **Peers** are existing agents that receive a lobbyd offer. They join with their own identity, after dispatchd grants them rights in the closed room.

**When a run ends:** oneshot worker types end the run as soon as they finish. Interactive worker types keep the room open until `max_duration`.

## Run

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

dispatchd acts as a normal agent identity, so it needs two things set up:
1. **A key:** create one in lobbyd, e.g. `lobbyd key create dispatch --scope agent`.
2. **An agentd grant:** in agentd's `callers` policy, grant `dispatch@<domain>` exactly the profiles and worker types its templates use.

```bash
uv sync
export DISPATCHD_CONFIG=dispatch.yaml DISPATCHD_DATA_DIR=.data
export LOBBYD_URL=http://127.0.0.1:8767 DISPATCHD_LOBBYD_API_KEY=lbk_…
uv run dispatchd check-config
uv run dispatchd serve [--port 8768]           # the service: schedules fire, runs execute
uv run dispatchd schedules                     # each schedule and when it fires next
uv run dispatchd run weekday-triage            # run a schedule now, in the foreground
uv run dispatchd runs [--name weekday-triage]  # recent runs and their rooms
```

**Schedules**
- **Format:** cron uses five fields (minute hour day month weekday) and is evaluated in the schedule's `timezone`, so DST is handled.
- **New and edited schedules:** a schedule doesn't fire when it's first seen. It's scheduled for its next time, and editing its cron or time zone recomputes that time.
- **Missed fires:** a fire missed by more than `catch_up` (default 5m), for example because dispatchd was down, is recorded as a skipped run and not replayed.
- **Overlap:** a run still active when the next fire comes follows the template's `on_overlap`: `skip` or `allow` (up to `max_concurrent`).
- **Restarts:** runs left pending or running by a previous process are resumed on startup. Every step is recorded, so nothing is duplicated.
- **Config changes:** the config file is reloaded when it changes. An invalid edit keeps the last good definitions running, and `/readyz` reports the error until it's fixed.

## Development

```bash
uv sync && uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run python scripts/e2e.py    # real lobbyd + roomsd + agentd from ../lobby ../rooms ../agents
```

`src/dispatchd/ops.py` is shared, identical, with lobbyd, roomsd and agentd. Architecture notes are in the docs repo's [CLAUDE.md](https://github.com/room-o-matic/docs/blob/main/CLAUDE.md). Issues are tracked in [room-o-matic/docs](https://github.com/room-o-matic/docs/issues).

## License

[Apache-2.0](LICENSE)
