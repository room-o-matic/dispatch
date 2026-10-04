import pytest
from pydantic import ValidationError

from dispatchd.config import Definitions, seconds

BASE = {
    "templates": {"t": {"workers": [{"name": "claude", "worker_type": "claude", "profile": "p"}]}},
}


def defs(**extra):
    return Definitions.model_validate({**BASE, **extra})


def test_durations():
    assert [seconds(v) for v in (90, "90s", "30m", "2h", "1d", "45")] == [
        90,
        90,
        1800,
        7200,
        86400,
        45,
    ]
    with pytest.raises(ValueError):
        seconds("soon")


def test_a_valid_definition():
    d = defs(
        schedules={
            "daily": {
                "cron": "0 7 * * 1-5",
                "timezone": "America/New_York",
                "use": "t",
                "goal": "triage",
            }
        },
        webhooks={
            "ask": {"secret_env": "HOOK_ASK", "use": "t", "task_template": "Question: {prompt}"}
        },
    )
    t = d.templates["t"]
    assert t.room.admission == "closed" and t.room.listed is False  # safe defaults
    assert t.run.max_duration == 3600 and t.run.on_overlap == "skip"
    assert d.missing_secrets({}) == ["ask: $HOOK_ASK"]
    assert d.missing_secrets({"HOOK_ASK": "s"}) == []


@pytest.mark.parametrize(
    "extra, message",
    [
        (
            {"schedules": {"x": {"cron": "0 7 * * *", "use": "nope", "goal": "g"}}},
            "unknown template",
        ),
        ({"schedules": {"x": {"cron": "0 7 * *", "use": "t", "goal": "g"}}}, "5 fields"),
        ({"schedules": {"x": {"cron": "0 0 31 2 *", "use": "t", "goal": "g"}}}, "never matches"),
        (
            {
                "schedules": {
                    "x": {"cron": "0 7 * * *", "timezone": "Mars/Base", "use": "t", "goal": "g"}
                }
            },
            "unknown time zone",
        ),
        (
            {"webhooks": {"x": {"secret_env": "S", "use": "t", "task_template": "no slot"}}},
            "{prompt}",
        ),
        ({"templates": {"t": {}}}, "at least one worker or peer"),
        (
            {
                "templates": {
                    "t": {
                        "workers": [
                            {"name": "a", "worker_type": "w", "profile": "p"},
                            {"name": "a", "worker_type": "w", "profile": "p"},
                        ]
                    }
                }
            },
            "unique",
        ),
        ({"templates": {"t": {"peers": [{"agent": "odin"}]}}}, "pattern"),
        ({"templates": {"Bad_Name": BASE["templates"]["t"]}}, "must match"),
        ({"templates": {"t": {**BASE["templates"]["t"], "shell": True}}}, "Extra inputs"),
    ],
)
def test_invalid_definitions(extra, message):
    with pytest.raises(ValidationError, match=message):
        Definitions.model_validate({**BASE, **extra})
