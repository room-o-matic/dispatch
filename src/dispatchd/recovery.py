"""Backup and restore for dispatchd (see ops.py for the mechanics).

A snapshot is older than the dispatchd it replaces. Before serving, a restore marks every
run the snapshot shows as pending or running as failed (restored_from_backup) instead of
resuming it: since the snapshot, its workers may have finished, been stopped or been
restored themselves, and resuming would summon work nobody asked for again. The run keeps
its room and session URLs for an operator to look at. Schedules keep their next fire
times; a fire missed while dispatchd was down is skipped by the usual catch-up rule.

Webhook secrets retired since the snapshot (rotated, or the webhook deleted) are cleared
again from the revocation journal; such a webhook answers 503 until an operator rotates it.

Not restorable: webhook deliveries after the snapshot are unknown to it, so a caller's
redelivery of one of those starts a new run.
"""

from pathlib import Path

from dispatchd import db, ops
from dispatchd.config import Settings
from dispatchd.runner import now_iso
from dispatchd.store import clear_retired_secrets


def backup(settings: Settings, dest: Path) -> dict:
    return ops.backup(settings.db_path, dest, service="dispatchd", schema_version=db.SCHEMA_VERSION)


def post_restore(settings: Settings) -> dict:
    conn = db.connect(settings.db_path)
    try:
        with conn:
            n = conn.execute(
                "update runs set state = 'failed', error = 'restored_from_backup: not resumed',"
                " finished_at = ?, updated_at = ? where state in ('pending', 'running')",
                (now_iso(), now_iso()),
            ).rowcount
        cleared = clear_retired_secrets(conn, ops.Journal(settings.journal_path))
    finally:
        conn.close()
    return {"runs_failed": n, "webhook_secrets_cleared": cleared}


def restore(settings: Settings, src: Path, *, force: bool = False) -> dict:
    report = ops.restore(
        src,
        settings.data_dir,
        service="dispatchd",
        max_schema_version=db.SCHEMA_VERSION,
        force=force,
    )
    report["schema"] = db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    report["invalidated"] = post_restore(settings)
    report["report_path"] = str(ops.write_report(settings.data_dir, report))
    return report
