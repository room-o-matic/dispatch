"""Definitions created through the operator API, and how they join the config file's.

The operator's YAML file stays authoritative for what it defines; the API adds templates,
schedules and webhooks alongside, never edits or shadows a file definition. The two are
merged and validated as one set (so a schedule naming a missing template, or deleting a
template still in use, is refused). API webhooks get a secret generated here, shown once.

A secret retired by a rotation or a deletion is journaled (by fingerprint) before the change
commits, and a restore clears any stored secret the journal lists, so an old snapshot can't
bring a retired secret back. That webhook answers 503 until an operator rotates it.
"""

import hashlib
import json
import secrets
import sqlite3
from pathlib import Path

import yaml

from dispatchd import db, ops
from dispatchd.config import Definitions

KINDS = {"template": "templates", "schedule": "schedules", "webhook": "webhooks"}


class DefinitionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class Store:
    def __init__(self, db_path: Path, journal: ops.Journal | None = None):
        self.db_path = db_path
        self.journal = journal or ops.Journal(None)

    def _retire(self, conn: sqlite3.Connection, name: str, unless: str | None = None) -> None:
        """Journal the current secret of webhook `name` (if any) before it's replaced."""
        r = conn.execute(
            "select secret from definitions where kind = 'webhook' and name = ?", (name,)
        ).fetchone()
        if r and r["secret"] and r["secret"] != unless:
            self.journal.append(
                "webhook_secret_retired", name=name, sha256=fingerprint(r["secret"])
            )

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def bodies(self) -> dict[str, dict]:
        out: dict[str, dict] = {k: {} for k in KINDS.values()}
        conn = self._conn()
        try:
            for r in conn.execute("select kind, name, body_json from definitions"):
                out[KINDS[r["kind"]]][r["name"]] = json.loads(r["body_json"])
        finally:
            conn.close()
        return out

    def meta(self, kind: str, name: str) -> dict | None:
        conn = self._conn()
        try:
            r = conn.execute(
                "select created_by, created_at, updated_at from definitions"
                " where kind = ? and name = ?",
                (kind, name),
            ).fetchone()
        finally:
            conn.close()
        return dict(r) if r else None

    def secret(self, name: str) -> str | None:
        conn = self._conn()
        try:
            r = conn.execute(
                "select secret from definitions where kind = 'webhook' and name = ?", (name,)
            ).fetchone()
        finally:
            conn.close()
        return r["secret"] if r else None

    def put(
        self, kind: str, name: str, body: dict, by: str, now: str, secret: str | None = None
    ) -> None:
        conn = self._conn()
        try:
            with conn:
                if kind == "webhook" and secret:
                    self._retire(conn, name, unless=secret)
                conn.execute(
                    "insert into definitions (kind, name, body_json, secret, created_by,"
                    " created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?)"
                    " on conflict (kind, name) do update set body_json = excluded.body_json,"
                    " secret = coalesce(excluded.secret, secret),"
                    " updated_at = excluded.updated_at",
                    (kind, name, json.dumps(body), secret, by, now, now),
                )
        finally:
            conn.close()

    def delete(self, kind: str, name: str) -> bool:
        conn = self._conn()
        try:
            with conn:
                if kind == "webhook":
                    self._retire(conn, name)
                return (
                    conn.execute(
                        "delete from definitions where kind = ? and name = ?", (kind, name)
                    ).rowcount
                    > 0
                )
        finally:
            conn.close()


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def clear_retired_secrets(conn: sqlite3.Connection, journal: ops.Journal) -> list[str]:
    """After a restore: drop every stored secret the journal says was retired."""
    retired = {e["sha256"] for e in journal.entries() if e["kind"] == "webhook_secret_retired"}
    cleared = []
    for r in conn.execute("select name, secret from definitions where secret is not null"):
        if fingerprint(r["secret"]) in retired:
            cleared.append(r["name"])
    with conn:
        for name in cleared:
            conn.execute(
                "update definitions set secret = null where kind = 'webhook' and name = ?", (name,)
            )
    return cleared


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def merge(file_raw: dict, api: dict[str, dict]) -> tuple[Definitions, dict]:
    """Validate the file's and the API's definitions as one set. Returns the definitions
    and the origin of each ("file" or "api"), keyed (kind, name)."""
    merged: dict[str, dict] = {k: dict(file_raw.get(k) or {}) for k in KINDS.values()}
    origin = {(k, n): "file" for k in KINDS.values() for n in merged[k]}
    for kind, items in api.items():
        for name, body in items.items():
            if name in merged[kind]:
                raise ValueError(
                    f"{kind[:-1]} {name!r} is defined in both the config file"
                    " and the API; remove one"
                )
            merged[kind][name] = body
            origin[(kind, name)] = "api"
    for name, w in (file_raw.get("webhooks") or {}).items():
        if not (w or {}).get("secret_env"):
            raise ValueError(f"webhook {name!r} in the config file needs secret_env")
    return Definitions.model_validate(merged), origin


def load_file(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}
