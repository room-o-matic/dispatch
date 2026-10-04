import argparse
import json
import sys

from dispatchd.config import Definitions, Settings


def _defs(settings: Settings) -> Definitions:
    return Definitions.load(settings.config_path)


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    try:
        defs = _defs(settings)
    except Exception as e:  # noqa: BLE001 - report any config error plainly
        print(f"error: {settings.config_path}: {e}", file=sys.stderr)
        return 1
    missing = defs.missing_secrets()
    print(
        f"ok: {len(defs.templates)} template(s), {len(defs.schedules)} schedule(s),"
        f" {len(defs.webhooks)} webhook(s)"
    )
    for m in missing:
        print(f"warning: webhook secret not set: {m}", file=sys.stderr)
    return 0


def _runner(settings: Settings):
    from dispatchd import db
    from dispatchd.runner import Runner, default_client

    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    return Runner(
        default_client(settings),
        settings.db_path,
        lambda: _defs(settings),
        poll_seconds=settings.poll_seconds,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Run a schedule now, in the foreground (also useful to try a template)."""
    settings = Settings.from_env()
    defs = _defs(settings)
    if args.schedule not in defs.schedules:
        print(f"error: no schedule {args.schedule!r}", file=sys.stderr)
        return 1
    s = defs.schedules[args.schedule]
    runner = _runner(settings)
    run, _ = runner.create("manual", args.schedule, s.use, args.goal or s.goal)
    print(f"{run['id']} {run['state']}", flush=True)
    run = runner.execute(run["id"])
    print(
        json.dumps(
            {k: run[k] for k in ("id", "state", "room_url", "sessions", "offers", "error")},
            indent=2,
        )
    )
    return 0 if run["state"] == "done" else 1


def cmd_runs(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    from dispatchd import db
    from dispatchd.runner import Runner

    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    runner = Runner(None, settings.db_path, lambda: _defs(settings))
    for r in runner.recent(name=args.name, limit=args.limit):
        print(
            f"{r['id']}  {r['state']:8} {r['source']:8} {r['name']:20} {r['room_url'] or '-'}"
            f"{'  ' + r['error'] if r['error'] else ''}"
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("dispatchd.app:create_app", factory=True, host=args.host, port=args.port)
    return 0


def cmd_schedules(args: argparse.Namespace) -> int:
    """Each schedule and when it fires next (as of the last scheduler pass)."""
    settings = Settings.from_env()
    from dispatchd import db
    from dispatchd.runner import Runner
    from dispatchd.scheduler import Scheduler

    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    defs = _defs(settings)
    state = Scheduler(Runner(None, settings.db_path, lambda: defs), lambda: defs).state()
    for name, s in defs.schedules.items():
        st = state.get(name, {})
        status = "disabled" if not s.enabled else f"next {st.get('next_fire') or '(not yet)'}"
        print(f"{name:24} {s.cron:16} {s.timezone:20} {status}  last {st.get('last_fire') or '-'}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from pathlib import Path

    from dispatchd import db, recovery

    settings = Settings.from_env()
    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    manifest = recovery.backup(settings, Path(args.out))
    print(json.dumps({k: v for k, v in manifest.items() if k != "files"}, indent=2))
    return 0


def cmd_verify_backup(args: argparse.Namespace) -> int:
    from pathlib import Path

    from dispatchd import ops

    try:
        m = ops.verify_backup(Path(args.path))
    except ops.BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"ok: {m['service']} schema v{m['schema_version']}, taken {m['created_at']}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from pathlib import Path

    from dispatchd import ops, recovery

    try:
        report = recovery.restore(Settings.from_env(), Path(args.source), force=args.force)
    except (ops.BackupError, ops.SchemaError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dispatchd", description="Scheduled and webhook-triggered rooms"
    )
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the service: scheduler and run execution")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8768)
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("check-config", help="validate $DISPATCHD_CONFIG").set_defaults(
        func=cmd_check_config
    )
    sub.add_parser("schedules", help="schedules and when they fire next").set_defaults(
        func=cmd_schedules
    )
    r = sub.add_parser("run", help="run a schedule now, in the foreground")
    r.add_argument("schedule")
    r.add_argument("--goal", help="override the schedule's goal for this run")
    r.set_defaults(func=cmd_run)
    ls = sub.add_parser("runs", help="list recent runs")
    ls.add_argument("--name")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=cmd_runs)
    b = sub.add_parser("backup", help="snapshot the database (safe while serving)")
    b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_backup)
    v = sub.add_parser("verify-backup", help="check a backup's checksums and integrity")
    v.add_argument("path")
    v.set_defaults(func=cmd_verify_backup)
    r = sub.add_parser("restore", help="restore a backup into $DISPATCHD_DATA_DIR (stopped)")
    r.add_argument("source")
    r.add_argument("--force", action="store_true", help="move an existing database aside")
    r.set_defaults(func=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
