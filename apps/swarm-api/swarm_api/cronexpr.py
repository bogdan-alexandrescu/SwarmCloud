"""Five-field cron expressions: parse, next firings, words (docs/schedules.md §2.3, §6.3).

swarm-api's own parser, over `zoneinfo`, not a dependency (§0: no cron library
is a dependency of any app). ONE implementation serves the schedule tick, the
create/edit validation, the console's "next five" and the words, so the form
and the tick cannot disagree: the console asks `POST /v1/schedules:preview`
rather than carrying a second parser in TypeScript, which would drift.

GRAMMAR. Five fields: minute (0-59), hour (0-23), day of month (1-31), month
(1-12, `JAN`-`DEC`) and day of week (0-7, Sunday is 0 and 7, `SUN`-`SAT`).
Each field is a comma list of `*`, `N`, `A-B`, `*/S`, `A-B/S` or `A/S` (A to
the field's top). Aliases: `@hourly`, `@daily`, `@weekly`, `@monthly`.

REFUSED, each by its own code so the form can say what to change: a sixth
(seconds) field, `@reboot` and any other alias, Quartz's `L`, `W`, `#` and
`?`, a reversed range (`FRI-MON`), a step of 0, and an expression that can
never fire (`0 0 31 2 *`).

DAY OF MONTH AND DAY OF WEEK COMBINE AS VIXIE CRON DOES: when BOTH are
restricted, a day matching EITHER fires, and the words say "or". "Restricted"
is Vixie's own test, a field that does not begin with `*`, so `*/2` in the day
of month still ANDs with a day of week, exactly as cron does.

DAYLIGHT SAVING. A slot is a local wall time resolved to a UTC instant:
  * a wall time that does not exist (spring forward) fires ONCE, at the first
    instant after the gap -- every missing time maps there, and the strict
    "after" of `next_after` collapses them into one firing;
  * a wall time that happens twice (fall back) fires ONCE, at its FIRST
    occurrence (fold=0); the second occurrence is never a slot.
That is the rule `outcomes._resolve_wall` uses for bucket edges; it is
restated here rather than imported so this module stays stdlib-only and
importable with no cloud client. The slot is the UTC instant, so two slots
can never share a key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

#: The aliases §2.3 allows, and what each means. `@yearly`, `@annually`,
#: `@midnight` and `@reboot` are not in the list and are refused by name.
ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

MONTH_NAMES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
DAY_NAMES = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")

#: How many firings `min_gap` measures. §2.3: the smallest gap between the
#: next 50 firings, so `*/5` is caught whatever its spelling.
GAP_SAMPLE = 50

#: How far `next_after` searches before it calls an expression dead. Nine
#: years covers `0 0 29 2 *` across a century year that is not a leap year
#: (2096 -> 2104), the longest gap any valid expression can have.
SEARCH_DAYS = 366 * 9

_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")
_QUARTZ = set("LW#?")
_ELEMENT = re.compile(r"^(?:(\*)|(\d+)(?:-(\d+))?)(?:/(\d+))?$")
#: The most days any month has, February counted with its 29th.
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class CronError(ValueError):
    """An expression or time zone this parser refuses. `code` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CronExpr:
    """A parsed expression. Sets are of allowed values; day of week 7 is folded to 0."""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: Vixie's "restricted" test: the field did not begin with `*`.
    days_restricted: bool
    weekdays_restricted: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_dom = day.day in self.days
        in_dow = (day.weekday() + 1) % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return in_dom or in_dow
        return in_dom and in_dow


def _substitute_names(text: str, names: tuple[str, ...], base: int) -> str:
    for index, name in enumerate(names):
        text = text.replace(name, str(index + base))
    return text


def _parse_field(text: str, index: int) -> frozenset[int]:
    low, high = _BOUNDS[index]
    label = _FIELD_NAMES[index]
    upper = text.upper()
    if index == 3:
        upper = _substitute_names(upper, MONTH_NAMES, 1)
    elif index == 4:
        upper = _substitute_names(upper, DAY_NAMES, 0)
    for char in upper:
        if char in _QUARTZ:
            raise CronError(
                "quartz_syntax",
                f"{label}: {char!r} is Quartz syntax, which this cron does not support",
            )
        if not (char.isdigit() or char in "*,-/"):
            raise CronError("bad_character", f"{label}: {char!r} is not allowed in {text!r}")
    values: set[int] = set()
    for element in upper.split(","):
        match = _ELEMENT.match(element)
        if not match:
            raise CronError("bad_field", f"{label}: {element!r} is not N, A-B, * or a step of one")
        star, first, last, step_text = match.groups()
        if star:
            start, end = low, high
        else:
            start = int(first)
            end = int(last) if last is not None else (high if step_text else start)
        step = int(step_text) if step_text else 1
        for value in (start, end):
            if not low <= value <= high:
                raise CronError("out_of_range", f"{label}: {value} is outside {low}-{high}")
        if start > end:
            raise CronError("reversed_range", f"{label}: {element!r} runs backwards")
        if step < 1 or step > high - low + 1:
            raise CronError("bad_step", f"{label}: the step in {element!r} must be 1-{high - low + 1}")
        values.update(range(start, end + 1, step))
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


def parse(text: str) -> CronExpr:
    """Parse `text`, or raise `CronError` naming what to change."""
    if not isinstance(text, str) or not text.strip():
        raise CronError("empty", "a cron expression is required")
    source = " ".join(text.split())
    expanded = source
    if source.startswith("@"):
        alias = source.lower()
        if alias not in ALIASES:
            raise CronError(
                "unknown_alias",
                f"{source} is not supported; the aliases are {', '.join(ALIASES)}",
            )
        expanded = ALIASES[alias]
    fields = expanded.split(" ")
    if len(fields) == 6:
        raise CronError("seconds_field", "six fields: a seconds field is not supported; use five")
    if len(fields) != 5:
        raise CronError(
            "field_count",
            f"{len(fields)} fields: an expression is minute, hour, day of month, month, day of week",
        )
    parsed = [_parse_field(field, index) for index, field in enumerate(fields)]
    expr = CronExpr(
        source=source,
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=parsed[4],
        days_restricted=not fields[2].startswith("*"),
        weekdays_restricted=not fields[4].startswith("*"),
    )
    if not _can_fire(expr):
        raise CronError("never_fires", f"{source} names no day that exists, so it would never fire")
    return expr


def _can_fire(expr: CronExpr) -> bool:
    # A day of week only ever widens (OR) or meets a day-of-month set that
    # begins with `*` (AND), and every month has every weekday, so only a
    # day-of-month-only expression can name no real day (`31 2`).
    if expr.weekdays_restricted:
        return True
    return any(day <= _MONTH_DAYS[month - 1] for month in expr.months for day in expr.days)


# --------------------------------------------------------------------------
# Time zones and instants
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    try:
        return frozenset(available_timezones())
    except Exception:  # pragma: no cover - depends on the image
        return frozenset()


def zone(name: str) -> ZoneInfo:
    """The IANA zone `name`, or `CronError`. A deployment with no zone database says so."""
    if not isinstance(name, str) or not name:
        raise CronError("unknown_timezone", "timezone is required: an IANA name such as Europe/London")
    known = _known_zones()
    if not known:
        raise CronError(
            "no_tz_database",
            "this deployment has no time zone database, so no timezone resolves; the image needs tzdata",
        )
    if name not in known:
        raise CronError("unknown_timezone", f"{name!r} is not an IANA time zone, such as Europe/London")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise CronError("unknown_timezone", f"{name!r} could not be loaded") from None


def _wall(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz).replace(tzinfo=None)


def resolve_wall(wall: datetime, tz: ZoneInfo) -> datetime:
    """The UTC instant of naive local `wall`: fold=0 when ambiguous, the gap's end when missing."""
    first = wall.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    if _wall(first, tz) == wall:
        return first
    other = wall.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    lo_s = int(min(first, other).timestamp())
    hi_s = int(max(first, other).timestamp())
    # The offset changes somewhere in (lo, hi]; the first instant whose wall
    # clock has passed `wall` is the end of the gap.
    while hi_s - lo_s > 1:
        mid = (lo_s + hi_s) // 2
        if _wall(datetime.fromtimestamp(mid, timezone.utc), tz) >= wall:
            hi_s = mid
        else:
            lo_s = mid
    return datetime.fromtimestamp(hi_s, timezone.utc)


def next_after(expr: CronExpr, after: datetime, tz: ZoneInfo) -> datetime:
    """The first slot strictly after `after` (aware), as an aware UTC instant.

    Local wall times are walked in order and each resolved to an instant. That
    map never runs backwards (fold=0 is the earliest reading of an ambiguous
    time, and a missing time maps to the gap's end), so the first candidate
    past `after` is the answer, and starting from `after`'s own wall clock
    misses nothing.
    """
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    start = _wall(after, tz).replace(second=0, microsecond=0)
    minutes = sorted(expr.minutes)
    hours = sorted(expr.hours)
    day = start.date()
    for _ in range(SEARCH_DAYS):
        if expr.day_matches(day):
            for hour in hours:
                if day == start.date() and hour < start.hour:
                    continue
                for minute in minutes:
                    wall = datetime(day.year, day.month, day.day, hour, minute)
                    if wall < start:
                        continue
                    instant = resolve_wall(wall, tz)
                    if instant > after:
                        return instant
        day += timedelta(days=1)
    raise CronError("never_fires", f"{expr.source} has no slot within {SEARCH_DAYS // 366} years")


def next_n(expr: CronExpr, after: datetime, tz: ZoneInfo, count: int) -> list[datetime]:
    """The next `count` slots after `after`, in UTC."""
    out: list[datetime] = []
    moment = after
    for _ in range(count):
        moment = next_after(expr, moment, tz)
        out.append(moment)
    return out


def min_gap(expr: CronExpr, after: datetime, tz: ZoneInfo, sample: int = GAP_SAMPLE) -> timedelta:
    """The smallest gap between the next `sample` firings (§2.3's minimum-interval test)."""
    slots = next_n(expr, after, tz, sample)
    return min(later - earlier for earlier, later in zip(slots, slots[1:]))


def preview(text: str, tz_name: str, now: datetime, count: int = 5) -> dict:
    """What `POST /v1/schedules:preview` answers before any type check (§6.3).

    `next` is ISO 8601 in the schedule's zone, with its offset, so the console
    can show it there and convert it to the viewer's zone without a parser.
    """
    expr = parse(text)
    tz = zone(tz_name)
    slots = next_n(expr, now, tz, max(count, GAP_SAMPLE))
    gap = min(later - earlier for earlier, later in zip(slots, slots[1:]))
    return {
        "words": words(expr),
        "next": [slot.astimezone(tz).isoformat() for slot in slots[:count]],
        "min_gap_minutes": int(gap.total_seconds() // 60),
    }


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

_MONTH_WORDS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_DAY_WORDS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _join(items: list[str], last: str = "and") -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {last} {items[-1]}"


def _runs(values: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for value in values:
        if runs and value == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], value)
        else:
            runs.append((value, value))
    return runs


def _ranged(values: list[int], name, single=None) -> str:
    """Values as a list, with runs of three or more as `A to B`."""
    single = single or name
    parts: list[str] = []
    for first, last in _runs(values):
        if last - first >= 2:
            parts.append(f"{name(first)} to {name(last)}")
        else:
            parts.extend(single(value) for value in range(first, last + 1))
    return _join(parts)


def _step(values: frozenset[int], low: int, high: int) -> int | None:
    """S when `values` is exactly `low`, `low+S`, ... up to `high`; else None."""
    ordered = sorted(values)
    if len(ordered) < 2 or ordered[0] != low:
        return None
    step = ordered[1] - ordered[0]
    return step if ordered == list(range(low, high + 1, step)) else None


def _time_words(expr: CronExpr) -> tuple[str, bool]:
    """The time-of-day phrase, and whether it is a frequency (no "every day" needed)."""
    all_hours = len(expr.hours) == 24
    minute_step = _step(expr.minutes, 0, 59)
    if all_hours:
        if minute_step == 1:
            return "every minute", True
        if minute_step:
            return f"every {minute_step} minutes", True
        if expr.minutes == {0}:
            return "every hour", True
        marks = _join([f":{m:02d}" for m in sorted(expr.minutes)])
        return f"every hour at {marks}", True
    hours = sorted(expr.hours)
    if len(expr.minutes) * len(hours) <= 6:
        return _join([f"{h:02d}:{m:02d}" for h in hours for m in sorted(expr.minutes)]), False
    hour_step = _step(expr.hours, 0, 23)
    if hour_step and len(expr.minutes) == 1:
        (minute,) = expr.minutes
        return f"every {hour_step} hours" + (f" at :{minute:02d}" if minute else ""), True
    if minute_step:
        every = "every minute" if minute_step == 1 else f"every {minute_step} minutes"
        if len(_runs(hours)) == 1:
            return f"{every} from {hours[0]:02d}:00 to {hours[-1]:02d}:59", True
        return f"{every} in the {_join([f'{h:02d}:00' for h in hours])} hours", True
    marks = _join([f":{m:02d}" for m in sorted(expr.minutes)])
    return f"at {marks} past {_join([f'{h:02d}' for h in hours])} o'clock", True


def _weekday_words(weekdays: frozenset[int]) -> str:
    if weekdays == {1, 2, 3, 4, 5}:
        return "on weekdays"
    if weekdays == {0, 6}:
        return "on weekends"
    return "on " + _ranged(sorted(weekdays), lambda d: _DAY_WORDS[d], lambda d: _DAY_WORDS[d] + "s")


def _day_words(expr: CronExpr) -> str:
    dom_step = _step(expr.days, 1, 31)
    if dom_step == 2:
        dom = "on odd-numbered days"
    elif dom_step and dom_step > 2:
        dom = f"every {dom_step} days from the 1st"
    else:
        dom = "on the " + _ranged(sorted(expr.days), _ordinal)
    if expr.days_restricted and expr.weekdays_restricted:
        return f"{dom}, or {_weekday_words(expr.weekdays)}"
    # Otherwise the two AND (Vixie), and a field that begins with `*` can
    # still narrow (`*/2`), so the words follow the sets, not the spelling.
    dom_narrow = len(expr.days) < 31
    dow_narrow = len(expr.weekdays) < 7
    if dom_narrow and dow_narrow:
        return f"{dom} that fall {_weekday_words(expr.weekdays)}"
    if dow_narrow:
        return _weekday_words(expr.weekdays)
    if dom_narrow:
        return dom
    return ""


def words(expr: CronExpr) -> str:
    """The expression in words, e.g. `0 9 1 * MON` -> "09:00 on the 1st, or on Mondays"."""
    time_part, frequency = _time_words(expr)
    day_part = _day_words(expr)
    parts = [time_part]
    if day_part:
        parts.append(day_part)
    elif not frequency:
        parts.append("every day")
    if len(expr.months) < 12:
        parts.append("in " + _ranged(sorted(expr.months), lambda m: _MONTH_WORDS[m - 1]))
    return " ".join(parts)
