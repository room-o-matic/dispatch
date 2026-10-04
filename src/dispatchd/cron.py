"""A small 5-field cron (minute hour day-of-month month day-of-week), evaluated in a time
zone. No dependency: schedules are coarse (minutes) and this is easy to reason about.

Each field takes `*`, numbers, `a-b` ranges, `,` lists and `/n` steps (`*/15`,
`1-5/2`). Day of week is 0-6 with Sunday 0 (7 also means Sunday). As in classic cron, if
both day-of-month and day-of-week are restricted, a day matches when either does.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7)]


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise CronError(f"bad step in {part!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = hi if step_s else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"{part!r} is outside {lo}-{hi}")
        out.update(range(start, end + 1, step))
    return out


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset  # 0 = Sunday
    day_any: bool
    weekday_any: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = expr.split()
        if len(parts) != 5:
            raise CronError(f"{expr!r}: need 5 fields (minute hour day month weekday)")
        try:
            sets = [_field(p, lo, hi) for p, (_, lo, hi) in zip(parts, FIELDS, strict=True)]
        except ValueError as e:
            raise CronError(f"{expr!r}: {e}") from e
        weekdays = {0 if d == 7 else d for d in sets[4]}
        return cls(
            expr,
            *(frozenset(s) for s in sets[:4]),
            frozenset(weekdays),
            parts[2] == "*",
            parts[4] == "*",
        )

    def _day_ok(self, t: datetime) -> bool:
        dom = t.day in self.days
        dow = (t.isoweekday() % 7) in self.weekdays
        if self.day_any or self.weekday_any:
            return dom and dow
        return dom or dow

    def next_after(self, after: datetime, tz: str) -> datetime:
        """The first matching minute strictly after `after` (aware), as an aware datetime."""
        zone = ZoneInfo(tz)
        t = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=5 * 366)  # e.g. "0 0 31 2 *" never matches
        while t < limit:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute in self.minutes:
                # Wall-clock arithmetic in a zone: normalise through UTC so DST gaps/folds
                # resolve to a real instant.
                return t.astimezone(ZoneInfo("UTC")).astimezone(zone)
            t += timedelta(minutes=1)
        raise CronError(f"{self.expr!r} never matches")
