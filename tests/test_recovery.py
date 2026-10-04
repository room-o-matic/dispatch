from test_runner import TEMPLATE, Stub

from dispatchd import cli, db, recovery
from dispatchd.config import Definitions, Settings
from dispatchd.runner import Runner


def test_restore_fails_unfinished_runs_instead_of_resuming(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data", config_path=tmp_path / "x.yaml", lobby_url="http://l"
    )
    db.init_db(settings.db_path)
    defs = Definitions.model_validate({"templates": {"review": TEMPLATE}})
    runner = Runner(Stub(), settings.db_path, lambda: defs, sleep=lambda s: None)
    done, _ = runner.create("manual", "a", "review", "Goal")
    runner.execute(done["id"])
    live, _ = runner.create("manual", "b", "review", "Goal")  # unfinished in the snapshot
    recovery.backup(settings, tmp_path / "bk")

    monkeypatch.setenv("DISPATCHD_DATA_DIR", str(settings.data_dir))
    assert cli.main(["verify-backup", str(tmp_path / "bk")]) == 0
    assert cli.main(["restore", str(tmp_path / "bk")]) == 1  # refuses without --force
    assert cli.main(["restore", str(tmp_path / "bk"), "--force"]) == 0
    assert '"runs_failed": 1' in capsys.readouterr().out
    assert runner.get(done["id"])["state"] == "done"
    restored = runner.get(live["id"])
    assert restored["state"] == "failed" and "restored_from_backup" in restored["error"]
    assert runner.unfinished() == []  # nothing gets resumed by the next start
