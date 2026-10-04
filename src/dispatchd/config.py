"""dispatchd configuration: service settings from the environment, and the operator's
definitions (templates, schedules, webhooks) from a YAML file reviewed like code.

A *template* is the restriction: the room's settings, the agents brought in and what
they may do. Schedules and webhooks name a template with `use:` and supply the goal (a
schedule fixes it; a webhook takes it from the caller, inside the operator's
`task_template`). What a template enforces comes from the services: agentd profiles and
the caller grant agentd gives dispatchd, room admission and rights, loop guards, budgets.
`rules` are guidance text for the agents, not enforcement.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dispatchd.cron import Cron, CronError

NAME = r"^[a-z0-9][a-z0-9-]{0,62}$"
AGENT = r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$"
_DURATION = re.compile(r"^(\d+)\s*([smhd]?)$")


def seconds(value: int | float | str) -> int:
    """Durations as seconds: 90, "90s", "30m", "2h", "1d"."""
    if isinstance(value, int | float):
        return int(value)
    m = _DURATION.match(value.strip())
    if not m:
        raise ValueError(f"bad duration {value!r} (use e.g. 90s, 30m, 2h, 1d)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoomSpec(Strict):
    admission: Literal["open", "closed"] = "closed"
    listed: bool = False
    max_hops: int | None = Field(default=None, ge=1, le=100)
    message_rate_per_minute: int | None = Field(default=None, ge=1, le=10_000)
    archive_after: int = Field(default=0, description="seconds after the run; 0 = at once")
    server_tag: str | None = None  # pick a roomsd by directory tag

    _dur = field_validator("archive_after", mode="before")(lambda v: seconds(v))


class WorkerSpec(Strict):
    name: str = Field(pattern=NAME)
    worker_type: str
    profile: str
    role: str | None = None
    instance: str | None = Field(default=None, description="agentd base URL; default: pick")
    # A directory on the agentd host mounted as the worker's working dir (e.g. a repo as a
    # knowledge base); the profile decides read or read_write. agentd's workspace_roots and
    # its grant for dispatchd still bound it.
    workspace: str | None = Field(default=None, pattern=r"^/")


class PeerSpec(Strict):
    agent: str = Field(pattern=AGENT)
    rights: list[Literal["read", "write", "invite", "admin"]] = ["read", "write"]
    role: str | None = None
    respond_within: int | None = None

    _dur = field_validator("respond_within", mode="before")(
        lambda v: None if v is None else seconds(v)
    )


class RunSpec(Strict):
    max_duration: int = 3600
    max_concurrent: int = Field(default=1, ge=1)
    on_overlap: Literal["skip", "allow"] = "skip"
    # parallel: summon every worker at once. sequential: summon each after the previous
    # one's session has ended, so later workers can read and build on earlier posts (for
    # oneshot worker types; an interactive worker would hold the line until max_duration).
    order: Literal["parallel", "sequential"] = "parallel"

    _dur = field_validator("max_duration", mode="before")(lambda v: seconds(v))


class Template(Strict):
    room: RoomSpec = RoomSpec()
    rules: list[str] = []
    workers: list[WorkerSpec] = []
    peers: list[PeerSpec] = []
    run: RunSpec = RunSpec()

    @model_validator(mode="after")
    def _someone(self):
        if not self.workers and not self.peers:
            raise ValueError("a template needs at least one worker or peer")
        names = [w.name for w in self.workers]
        if len(names) != len(set(names)):
            raise ValueError("worker names must be unique")
        return self


class Schedule(Strict):
    cron: str
    timezone: str = "UTC"
    use: str
    goal: str = Field(min_length=1, max_length=16_000)
    enabled: bool = True
    # A fire missed by more than this (e.g. dispatchd was down) is skipped, not replayed.
    catch_up: int = 300

    _dur = field_validator("catch_up", mode="before")(lambda v: seconds(v))

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        try:
            Cron.parse(v).next_after(datetime.now(UTC), "UTC")  # also: does it ever fire?
        except CronError as e:
            raise ValueError(str(e)) from e
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {v!r}") from e
        return v


class Webhook(Strict):
    # The config file names an env var holding the secret; a webhook created through the
    # operator API has none: dispatchd generated its secret and keeps it in its database.
    secret_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    use: str
    task_template: str = "{prompt}"
    max_prompt_bytes: int = Field(default=4000, ge=1, le=64_000)
    rate_per_hour: int = Field(default=20, ge=1)
    enabled: bool = True

    @field_validator("task_template")
    @classmethod
    def _has_prompt(cls, v: str) -> str:
        if "{prompt}" not in v:
            raise ValueError("task_template must contain {prompt}")
        return v


class Definitions(Strict):
    templates: dict[str, Template] = {}
    schedules: dict[str, Schedule] = {}
    webhooks: dict[str, Webhook] = {}

    @model_validator(mode="after")
    def _refs(self):
        for kind in ("templates", "schedules", "webhooks"):
            for name in getattr(self, kind):
                if not re.match(NAME, name):
                    raise ValueError(f"{kind[:-1]} name {name!r} must match {NAME}")
        for kind, items in (("schedule", self.schedules), ("webhook", self.webhooks)):
            for name, item in items.items():
                if item.use not in self.templates:
                    raise ValueError(f"{kind} {name!r} uses unknown template {item.use!r}")
        return self

    @classmethod
    def load(cls, path: Path) -> "Definitions":
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})

    def missing_secrets(self, env=os.environ) -> list[str]:
        return [
            f"{n}: ${w.secret_env}"
            for n, w in self.webhooks.items()
            if w.enabled and w.secret_env and not env.get(w.secret_env)
        ]


@dataclass
class Settings:
    data_dir: Path
    config_path: Path
    lobby_url: str
    api_key: str | None = field(default=None, repr=False)
    lobby_domain: str = "local"  # identities are name@domain
    lobby_jwks_url: str | None = None
    base_url: str = "http://127.0.0.1:8768"
    poll_seconds: float = 5.0  # how often a run checks its workers
    tick_seconds: float = 15.0  # how often schedules are checked
    operators: tuple[str, ...] = ()  # identities allowed on the operator HTTP API
    # Retired webhook secrets are journaled here, outside the database, and cleared again
    # after a restore (ops.py "Revocation journal"). Keep it off the data volume if you can.
    revocation_journal: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dispatchd.sqlite"

    @property
    def journal_path(self) -> Path:
        return self.revocation_journal or self.data_dir / "revocations.jsonl"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            data_dir=Path(env("DISPATCHD_DATA_DIR", "/var/lib/dispatchd")),
            config_path=Path(env("DISPATCHD_CONFIG", "/etc/dispatchd/dispatch.yaml")),
            lobby_url=env("LOBBYD_URL", "http://127.0.0.1:8767").rstrip("/"),
            api_key=env("DISPATCHD_LOBBYD_API_KEY"),
            lobby_domain=env("LOBBYD_DOMAIN", "local"),
            lobby_jwks_url=env("LOBBYD_JWKS_URL"),
            base_url=env("DISPATCHD_BASE_URL", cls.base_url).rstrip("/"),
            poll_seconds=float(env("DISPATCHD_POLL_SECONDS", 5)),
            tick_seconds=float(env("DISPATCHD_TICK_SECONDS", 15)),
            operators=tuple(o for o in env("DISPATCHD_OPERATORS", "").split(",") if o),
            revocation_journal=Path(j) if (j := env("DISPATCHD_REVOCATION_JOURNAL")) else None,
        )
