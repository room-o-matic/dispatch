from datetime import UTC, datetime

import pytest

from dispatchd.cron import Cron, CronError


def nxt(expr, after, tz="UTC"):
    return Cron.parse(expr).next_after(after, tz)


def test_every_15_minutes():
    t = datetime(2026, 10, 4, 10, 7, tzinfo=UTC)
    assert nxt("*/15 * * * *", t) == datetime(2026, 10, 4, 10, 15, tzinfo=UTC)
    assert nxt("*/15 * * * *", datetime(2026, 10, 4, 10, 15, tzinfo=UTC)).minute == 30


def test_weekday_mornings_in_a_time_zone_across_dst():
    # Friday 2026-10-30 07:00 New York, then the weekend, then Monday after DST ends (Nov 1)
    friday = datetime(2026, 10, 30, 11, 30, tzinfo=UTC)  # 07:30 EDT
    monday = nxt("0 7 * * 1-5", friday, "America/New_York")
    assert (monday.year, monday.month, monday.day, monday.hour) == (2026, 11, 2, 7)
    assert monday.utcoffset().total_seconds() == -5 * 3600  # EST now


def test_day_of_month_or_day_of_week():
    # classic cron: both restricted -> either matches. 2026-10-04 is a Sunday.
    t = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert nxt("0 9 15 * 0", t).day == 4  # Sunday comes before the 15th
    assert nxt("0 9 15 * 7", t).day == 4  # 7 is Sunday too


def test_ranges_lists_and_steps():
    c = Cron.parse("0,30 8-18/2 * 1,6 *")
    assert c.hours == {8, 10, 12, 14, 16, 18} and c.minutes == {0, 30} and c.months == {1, 6}


@pytest.mark.parametrize(
    "bad",
    [
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "*/0 * * * *",
        "5-1 * * * *",
        "x * * * *",
        "0 0 31 2 *",
    ],
)
def test_invalid(bad):
    with pytest.raises(CronError):
        c = Cron.parse(bad)
        c.next_after(datetime(2026, 1, 1, tzinfo=UTC), "UTC")
