"""Everything `sc` prints, and nothing it fetches.

WHY FORMATTING LIVES IN A MODULE THAT CANNOT REACH THE NETWORK. `sc` is the
command an operator runs when they suspect the platform is lying to them, so
the one thing its output must never do is invent a number. Keeping every rule
about how a figure is shown in a module with no client, no credentials and no
sockets means those rules can be tested exhaustively and offline -- which is
the only way the stale case gets tested at all, because a stale reading is by
definition one you cannot produce on demand from a live system.

THE THREE MARKS. They are the whole vocabulary, and they are `cs status`'s:

    12%     a measurement, recent enough to trust
    ~12%    the last measurement, too old to trust -- a projection, not a fact
    --      not measured (an em dash on a terminal that can draw one)

An operator who already reads `cs status` on their laptop should not have to
learn a second vocabulary for the same five facts about the same five accounts.

NEVER 0 FOR UNKNOWN. "this account has used none of its quota" and "nobody has
asked this account how much quota it has used" are different claims, and a
renderer that collapses them turns a dead poller into a healthy-looking pool.
Every formatter here returns the em dash rather than a zero when its input is
absent, and `Reading.known` is the only thing that decides which.

TWO TRAPS THIS FILE DELIBERATELY AVOIDS, both already paid for elsewhere in
this repository:

  * `pool.get("enabled", True)` is the Python spelling of `.enabled // true`
    in jq -- it reports a PAUSED pool as open, which is the one thing that
    column exists to show. `_tri_enabled` compares explicitly and returns
    None for "the payload did not say", which renders as a mark, not as open.
  * `windows.get(key) or {}` swallows a real reading of zero. Absence is
    tested with `is None`, never with truthiness.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from swarm_common import states as _states
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES
from swarm_common.states import CONCURRENCY_STATES, PENDING_STATES, TaskState

# --------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------
#
# A Style is data, not a terminal. Whoever owns stdout decides what goes in it
# (`sc.py` does); this module only reads it. That is what lets a test render
# the 60-column no-colour ASCII case without owning a terminal at all.

#: SGR codes, by tone name. Applied only when `Style.color` is true, which is
#: only ever true when the caller has confirmed stdout is a TTY.
_SGR = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "good": "\x1b[32m",
    "warn": "\x1b[33m",
    "bad": "\x1b[31m",
    "accent": "\x1b[36m",
}
_RESET = "\x1b[0m"

#: Severity order, worst first. Used to sort findings and to pick a tone.
SEVERITIES = ("down", "warn", "note")
_SEVERITY_TONE = {"down": "bad", "warn": "warn", "note": "dim"}
_SEVERITY_MARK = {"down": "x", "warn": "!", "note": "-"}


@dataclass(frozen=True)
class Style:
    """How wide, whether colour is allowed, and whether the font has glyphs.

    `unicode` is not cosmetic. A terminal running under `LANG=C` raises
    UnicodeEncodeError on the bar glyphs, which would turn `sc` into a
    traceback at exactly the moment someone needs it to work.
    """

    width: int = 80
    color: bool = False
    unicode: bool = True

    #: Below this, the table engine stops dropping columns and starts
    #: truncating. Nothing useful survives narrower than this anyway.
    MIN_WIDTH = 32

    @property
    def usable(self) -> int:
        return max(self.MIN_WIDTH, self.width)

    @property
    def dash(self) -> str:
        """The mark for "not measured". Never a zero, never an empty cell."""
        return "—" if self.unicode else "--"

    @property
    def filled(self) -> str:
        return "▰" if self.unicode else "#"

    @property
    def empty(self) -> str:
        return "▱" if self.unicode else "."

    @property
    def ellipsis(self) -> str:
        return "…" if self.unicode else "..."

    @property
    def marker(self) -> str:
        return "▸" if self.unicode else ">"

    @property
    def unlimited(self) -> str:
        return "∞" if self.unicode else "inf"

    @property
    def sep(self) -> str:
        """The separator between facts on one line.

        Style-bound rather than hard-coded because a middle dot is the single
        commonest way a "pure ASCII" screen turns out not to be one: it hides
        in subtitles and joined fragments, far from the bar glyphs anyone
        remembers to check.
        """
        return " · " if self.unicode else " - "

    def paint(self, text: str, tone: str | None) -> str:
        if not self.color or not tone:
            return text
        code = _SGR.get(tone)
        return f"{code}{text}{_RESET}" if code else text


PLAIN = Style()


@dataclass(frozen=True)
class Cell:
    """A table cell whose colour cannot corrupt the column arithmetic.

    Tone is kept beside the text rather than baked into it, because an SGR
    sequence is four invisible characters that `len()` counts and a terminal
    does not -- which is how coloured tables end up ragged.
    """

    text: str
    tone: str | None = None


def _text(value: Any) -> str:
    return value.text if isinstance(value, Cell) else str(value)


def _tone(value: Any) -> str | None:
    return value.tone if isinstance(value, Cell) else None


# --------------------------------------------------------------------------
# Table
# --------------------------------------------------------------------------


#: The narrowest a flexible column is allowed to become before the table gives
#: up and drops something instead. Below this a truncated cell is all ellipsis.
#:
#: A column that may be dropped altogether is allowed to shrink further than
#: one that may not: losing four characters of a STATE that `sc trouble`
#: repeats in full is cheaper than losing four characters of the account id
#: that says WHICH account the row is about.
_FLEX_FLOOR = 8
_FLEX_FLOOR_DROPPABLE = 5


def _floor(col: "Column") -> int:
    return _FLEX_FLOOR if col.drop == 0 else _FLEX_FLOOR_DROPPABLE


@dataclass(frozen=True)
class Column:
    key: str
    heading: str
    align: str = "left"
    #: Higher drops sooner. 0 never drops: a table that has shed its identity
    #: column is not a narrower table, it is a different one.
    drop: int = 0
    #: Absorbs truncation once there is nothing left to drop.
    flex: bool = False


def _fit(text: str, width: int, style: Style) -> str:
    if len(text) <= width:
        return text
    if width <= len(style.ellipsis):
        return text[:width]
    return text[: width - len(style.ellipsis)] + style.ellipsis


def render_table(
    columns: Sequence[Column],
    rows: Sequence[dict[str, Any]],
    style: Style,
    *,
    indent: int = 2,
    gutter: int = 2,
    heading: bool = True,
) -> list[str]:
    """Lay a table out, dropping columns rather than wrapping them.

    A wrapped terminal table is unreadable in a way a narrower one is not: the
    eye loses which continuation line belongs to which row. So when the width
    runs out, whole columns go -- highest `drop` first -- and only when nothing
    droppable is left does the flex column get truncated with an ellipsis.
    """
    if not rows:
        return []

    live = list(columns)
    budget = style.usable - indent

    def widths(cols: Sequence[Column]) -> dict[str, int]:
        out: dict[str, int] = {}
        for col in cols:
            longest = len(col.heading) if heading else 0
            for row in rows:
                longest = max(longest, len(_text(row.get(col.key, ""))))
            out[col.key] = longest
        return out

    def total(cols: Sequence[Column], w: dict[str, int]) -> int:
        return sum(w[c.key] for c in cols) + gutter * max(0, len(cols) - 1)

    def minimum(cols: Sequence[Column], wid: dict[str, int]) -> int:
        """What this column set costs once every flexible column has shrunk.

        Dropping is measured against THIS rather than against the natural
        width, because a column that truncation could have saved should not be
        thrown away -- one account with a wordy reason would otherwise cost
        every other account its STATE column entirely.

        A `drop=0` column counts at its FULL width even when it is flexible.
        Those are the identity columns -- the account id, the profile name --
        and shrinking one to make room for a column that could simply be
        dropped trades the label for the thing it labels.
        """
        base = sum(
            min(wid[c.key], _floor(c)) if (c.flex and c.drop > 0) else wid[c.key]
            for c in cols
        )
        return base + gutter * max(0, len(cols) - 1)

    w = widths(live)
    while minimum(live, w) > budget:
        droppable = [c for c in live if c.drop > 0]
        if not droppable:
            break
        victim = max(droppable, key=lambda c: (c.drop, len(c.heading)))
        live = [c for c in live if c.key != victim.key]
        w = widths(live)

    over = total(live, w) - budget
    if over > 0:
        # Shed from the most-droppable flexible column first, all the way to
        # its floor, before touching the next -- the same priority order the
        # drop loop uses. Taking from the widest instead would truncate the
        # account id, which nothing else on the screen repeats, in order to
        # spare a STATE that `sc trouble` prints in full.
        for col in sorted(
            (c for c in live if c.flex), key=lambda c: (-c.drop, c.key)
        ):
            if over <= 0:
                break
            take = min(over, max(0, w[col.key] - _floor(col)))
            w[col.key] -= take
            over -= take

    lines: list[str] = []
    pad = " " * indent

    if heading:
        parts = []
        for col in live:
            cell = _fit(col.heading, w[col.key], style)
            parts.append(cell.rjust(w[col.key]) if col.align == "right" else cell.ljust(w[col.key]))
        lines.append(style.paint(pad + (" " * gutter).join(parts).rstrip(), "dim"))

    for row in rows:
        parts = []
        for col in live:
            raw = row.get(col.key, "")
            text = _fit(_text(raw), w[col.key], style)
            laid = text.rjust(w[col.key]) if col.align == "right" else text.ljust(w[col.key])
            parts.append(style.paint(laid, _tone(raw)))
        lines.append((pad + (" " * gutter).join(parts)).rstrip())
    return lines


def detail(text: str, style: Style, *, indent: int = 4, tone: str | None = "dim") -> list[str]:
    """A prose line under a heading, wrapped to the terminal rather than past it.

    Transport errors are the longest strings `sc` ever prints and the ones it
    most needs to print in full -- truncating the explanation of why a figure
    is missing would leave only the fact that it is missing.
    """
    pad = " " * indent
    body = textwrap.wrap(text, width=max(20, style.usable - indent)) or [""]
    return [style.paint(pad + line, tone) for line in body]


def section(title: str, subtitle: str, style: Style) -> str:
    label = title.upper()
    head = style.paint(label, "bold")
    if not subtitle:
        return head
    room = style.usable - len(label) - 2
    return f"{head}  {style.paint(_fit(subtitle, max(0, room), style), 'dim')}"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    """An ISO timestamp, or None. Never raises: a malformed timestamp is an
    unknown time, and an unknown time is a mark, not a crash."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _span(seconds: float, style: Style) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "<1m"
    minutes, hours = seconds // 60, seconds // 3600
    if hours < 1:
        return f"{minutes}m"
    if hours < 24:
        return f"{hours}h {minutes % 60:02d}m"
    days = hours // 24
    return f"{days}d {hours % 24:02d}h"


def format_until(value: Any, now: datetime, style: Style) -> str:
    """How long until `value`. `cleared` once it is in the past."""
    when = parse_time(value)
    if when is None:
        return style.dash
    delta = (when - now).total_seconds()
    if delta <= 0:
        return "cleared"
    return _span(delta, style)


def format_ago(value: Any, now: datetime, style: Style) -> str:
    when = parse_time(value)
    if when is None:
        return style.dash
    delta = (now - when).total_seconds()
    if delta < 0:
        return "just now"
    return _span(delta, style) + " ago"


def format_age(value: Any, now: datetime, style: Style) -> str:
    when = parse_time(value)
    if when is None:
        return style.dash
    return _span(max(0.0, (now - when).total_seconds()), style)


def clock(value: Any) -> str:
    """`03:52:52Z` for an event's `at`, or `--:--:--` when it carried none.

    WHY LIVE OUTPUT CARRIES A TIME AT ALL. `swarm tail` and `swarm follow`
    print each task's events as they are read, task by task, so the order on
    screen is the order of the POLL and not of the platform. Measured on
    2026-09-25 (#88, SC-F9): a dependent step's cancel printed above its
    upstream's, which reads as the cascade running backwards. A time on every
    event line is what lets a reader put them back in order -- and, for
    `tail`, the events of one poll are sorted by it as well.

    UTC, and SAYS so with the `Z`: the API stamps UTC, and a bare `03:52:52`
    beside a local clock would be hours out with nothing to show it.
    ASCII only, so it survives the `--ascii` rule every other mark obeys.
    """
    when = parse_time(value)
    if when is None:
        return "--:--:--"
    return when.astimezone(timezone.utc).strftime("%H:%M:%SZ")


# --------------------------------------------------------------------------
# Who is asking, whose the figures are, and whether a listing is whole
# --------------------------------------------------------------------------


def tenant_of(me: Any) -> str | None:
    """The tenant id out of a `GET /v1/tenants/me` answer, or None.

    NESTED. The route answers `{"tenant": {"tenant_id": ...}, "principal":
    {...}}` (swarm_api/routes/tenants.py), and three readers in this package
    read `tenant_id` off the top level instead: the `sc` header, `swarm
    doctor` and `sc whoami`/`sc login`. None of them errored -- the key was
    simply absent -- so on 2026-09-25 (#88, SC-F3/F4) the header showed the
    tenant as "not measured" and doctor printed `tenant None` in the same run
    that had just read it. ONE reader, here, so the next shape change is one
    edit.

    The flat shape is still accepted after the nested one. It is what every
    fake in this suite used to answer, and a deployment older than the nesting
    would answer it too; reading it costs nothing and misreads nothing.
    """
    if not isinstance(me, dict):
        return None
    tenant = me.get("tenant")
    if isinstance(tenant, dict) and tenant.get("tenant_id"):
        return str(tenant["tenant_id"])
    flat = me.get("tenant_id")
    return str(flat) if flat else None


def principal_of(me: Any) -> dict[str, Any]:
    """`principal` out of the same answer: email, groups, is_admin.

    `{}` when there is none, never None, so a caller reads each field with
    `.get` and an absent field prints as "(not reported)" rather than as the
    word None -- which is what `swarm doctor` printed for `admin` on
    2026-09-25 while the route was serving `is_admin: false`.
    """
    if not isinstance(me, dict):
        return {}
    principal = me.get("principal")
    if isinstance(principal, dict):
        return principal
    return {key: me[key] for key in ("email", "groups", "is_admin") if key in me}


class Listing(list):
    """A list that also says WHOSE it is and whether it is WHOLE.

    A plain list can say neither, and both were missing on 2026-09-25 (#88):
    `sc` printed ACCOUNTS "0 accounts" and AGENTS "0 running" -- one tenant's
    figures -- directly beside CAPACITY's `claude-code 8/40`, which was
    another tenant's work on a shared pool, with nothing on screen to say the
    two were measured over different populations (SC-F5); and AGENTS was built
    from the newest 100 of 324 tasks and presented as complete (SC-F11).

    A SUBCLASS rather than a new return type, so every renderer and test that
    already takes a list keeps working, and `sc --json` still dumps a list.
    `tenant_id` is the tenant the ROUTE says it answered for -- every tenant-
    scoped listing route echoes the resolved one -- and `incomplete` is a list
    of sentences, empty when the listing is whole.
    """

    def __init__(
        self,
        items: Iterable[Any] = (),
        *,
        tenant_id: str | None = None,
        incomplete: Iterable[str] = (),
        window: "TaskWindow | None" = None,
    ) -> None:
        super().__init__(items)
        self.tenant_id = str(tenant_id) if tenant_id else None
        self.incomplete = [str(line) for line in incomplete]
        self.window = window


@dataclass(frozen=True)
class TaskWindow:
    """The newest-N page a task listing read beside its live states, SAID (#190).

    `sc.fetch_tasks` reads every LIVE task by state and, beside them, one page
    of the newest tasks of any state -- the only place a FAILED or
    DEAD_LETTERED task is read from. `sc trouble` counted those as "N task(s)
    ... in the window listed" and listed no window anywhere; the window lived
    in a docstring and a constant. So the page describes itself: how many it
    held, whether that was every task the tenant has, and when the oldest of
    them was created -- which is what a reader needs to tell "5 today" from
    "5 ever" from "5 of the newest hundred".
    """

    limit: int
    count: int
    oldest: Any = None
    whole: bool = False


def listing_tenant(items: Any) -> str | None:
    """The tenant a `Listing` was answered for; None for a plain list."""
    return getattr(items, "tenant_id", None) or None


def listing_window(items: Any) -> TaskWindow | None:
    """The newest-N window a `Listing` read; None for a plain list."""
    window = getattr(items, "window", None)
    return window if isinstance(window, TaskWindow) else None


def format_since(value: Any, now: datetime) -> str:
    """`03:28Z` for today, `2026-09-01 00:10Z` for any other day, "" when unknown."""
    when = parse_time(value)
    if when is None:
        return ""
    when = when.astimezone(timezone.utc)
    if when.date() == now.astimezone(timezone.utc).date():
        return when.strftime("%H:%MZ")
    return when.strftime("%Y-%m-%d %H:%MZ")


def window_scope(items: Any, whose: str, now: datetime) -> tuple[str, str]:
    """`("of the newest 100 tasks of tenant eng", " (created since 03:28Z)")`.

    The population a count over a task listing was taken from, and when its
    oldest member was created -- the two halves of "which window". A plain
    list, which has no window to name, is named by how many tasks it held.
    """
    window = listing_window(items)
    if window is None:
        return f"of the {len(items)} tasks{whose} this read returned", ""
    scope = (
        f"of all {window.count} tasks{whose}"
        if window.whole
        else f"of the newest {window.count} tasks{whose}"
    )
    since = format_since(window.oldest, now)
    return scope, f" (created since {since})" if since else ""


def listing_gaps(items: Any) -> list[str]:
    """What a `Listing` did not read, as sentences; `[]` for a plain list."""
    return list(getattr(items, "incomplete", None) or [])


def scoped(subtitle: str, tenant: str | None, style: Style) -> str:
    """A section subtitle that names the tenant its figures belong to.

    Only for TENANT-SCOPED sections. The capacity section is the other kind --
    shared pools, counted across every tenant -- and says that in its own body
    (`render_capacity`), so the two can no longer be read as one population.
    """
    if not tenant:
        return subtitle
    head = f"tenant {tenant}"
    return f"{head}{style.sep}{subtitle}" if subtitle else head


def scope_tenant(snap: "Snapshot") -> str | None:
    """Which tenant this snapshot's tenant-scoped figures belong to.

    `/v1/tenants/me` first, because it is the identity route; then whatever
    tenant a listing route echoed. They are the same resolved tenant -- every
    route resolves it through the same call -- so the fallbacks only matter for
    a narrow view that did not fetch the identity at all.
    """
    capacity = snap.capacity if isinstance(snap.capacity, dict) else {}
    return (
        tenant_of(snap.tenant)
        or listing_tenant(snap.tasks)
        or listing_tenant(snap.accounts)
        or (str(capacity["tenant_id"]) if capacity.get("tenant_id") else None)
    )


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One window's utilisation, and how much it can be trusted.

    `known` and `stale` are independent. A known-but-stale reading is a real
    number about a moment that has passed; an unknown reading is no number at
    all. Printing the second as the first is the failure this type exists to
    make impossible.
    """

    key: str
    utilization: float | None = None
    resets_at: Any = None
    reset: bool = False
    stale: bool = True

    @property
    def known(self) -> bool:
        return self.utilization is not None

    @property
    def trusted(self) -> bool:
        """A reset window's last reading describes a window that is over, so it
        is a projection even when it arrived a second ago."""
        return self.known and not self.stale and not self.reset


def account_is_stale(account: dict[str, Any]) -> bool:
    """Absent provenance is stale provenance.

    The broker computes `stale` and always sends it. If it is missing, this
    payload came from somewhere that does not track reading age -- which is
    exactly the case where a number must not be presented as current.
    """
    if account.get("observed_at") is None:
        return True
    flag = account.get("stale")
    if flag is None:
        return True
    return bool(flag)


def read_window(account: dict[str, Any], key: str) -> Reading:
    windows = account.get("windows")
    entry = windows.get(key) if isinstance(windows, dict) else None
    stale = account_is_stale(account)
    if not isinstance(entry, dict):
        return Reading(key=key, stale=stale)
    raw = entry.get("utilization")
    # `is None`, not falsiness: 0.0 is a measurement.
    util = None
    if raw is not None:
        try:
            util = float(raw)
        except (TypeError, ValueError):
            util = None
    return Reading(
        key=key,
        utilization=util,
        resets_at=entry.get("resets_at"),
        reset=bool(entry.get("reset")),
        stale=stale,
    )


#: Provider window keys, mapped to the two-character heading an operator
#: already knows. The broker's contract says the keys are the PROVIDER's and
#: are not fixed, so an unrecognised key keeps its own name rather than being
#: relabelled into a column it may not mean.
_WINDOW_HEADINGS = {
    "five_hour": "5H",
    "session": "5H",
    "5h": "5H",
    "seven_day": "7D",
    "weekly": "7D",
    "weekly_all": "7D",
    "7d": "7D",
}
_SHORT_KEYS = ("five_hour", "session", "5h")
_LONG_KEYS = ("seven_day", "weekly", "weekly_all", "7d")


def window_heading(key: str) -> str:
    return _WINDOW_HEADINGS.get(key, key.replace("_", " ").upper()[:7])


def window_keys(accounts: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    """The two window keys this pool actually reports, in the order to show.

    Preference before position: a provider that names its windows the way
    Anthropic does gets 5H and 7D in the right columns. Anything else is shown
    in the order it arrived, under its own name, rather than being forced into
    a heading that would misdescribe it.
    """
    ordered: list[str] = []
    for account in accounts:
        windows = account.get("windows")
        if isinstance(windows, dict):
            for key in windows:
                if key not in ordered:
                    ordered.append(key)
    if not ordered:
        return None, None
    short = next((k for k in _SHORT_KEYS if k in ordered), None)
    long = next((k for k in _LONG_KEYS if k in ordered), None)
    rest = [k for k in ordered if k not in (short, long)]
    if short is None:
        short = rest.pop(0) if rest else None
    if long is None:
        long = rest.pop(0) if rest else None
    return short, long


def format_percent(reading: Reading, style: Style) -> str:
    if not reading.known:
        return style.dash
    pct = f"{round(reading.utilization * 100)}%"
    return pct if reading.trusted else f"~{pct}"


def format_bar(reading: Reading, style: Style, cells: int = 5) -> str:
    if not reading.known:
        return ""
    filled = min(cells, max(0, int(round(reading.utilization * cells))))
    # A non-zero utilisation that rounds to nothing still gets one cell: an
    # empty bar beside "3%" reads as a rendering bug.
    if filled == 0 and reading.utilization > 0:
        filled = 1
    return style.filled * filled + style.empty * (cells - filled)


def format_window(reading: Reading, style: Style) -> Cell:
    if not reading.known:
        return Cell(f"{style.dash:>5}", "dim")
    text = f"{format_percent(reading, style):>5}  {format_bar(reading, style)}"
    tone = None
    if reading.utilization >= 0.98:
        tone = "bad"
    elif reading.utilization >= 0.85:
        tone = "warn"
    elif not reading.trusted:
        tone = "dim"
    return Cell(text, tone)


def binding_window(readings: Sequence[Reading]) -> Reading | None:
    """The window that will stop this account first: the fullest known one."""
    known = [r for r in readings if r.known]
    if not known:
        return None
    return max(known, key=lambda r: r.utilization)


_ACCOUNT_STATE_TONE = {
    "AVAILABLE": None,
    "PAUSED": "warn",
    "DRAINING": "dim",
    "REAUTH_REQUIRED": "bad",
}
_ACCOUNT_STATE_TEXT = {
    "AVAILABLE": "available",
    "PAUSED": "paused",
    "DRAINING": "draining",
    "REAUTH_REQUIRED": "reauth needed",
}


def format_account_state(account: dict[str, Any], style: Style = PLAIN) -> Cell:
    raw = str(account.get("state") or "")
    text = _ACCOUNT_STATE_TEXT.get(raw, raw.lower() or "?")
    reason = str(account.get("reason") or "").strip()
    if reason and raw != "AVAILABLE":
        text = f"{text}{style.sep}{reason}"
    return Cell(text, _ACCOUNT_STATE_TONE.get(raw, "dim"))


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


#: Every field `account_to_api` documents, and nothing else. An ALLOW-list,
#: not a deny-list, because the field that must never reach a screen or a file
#: is the one nobody has thought of yet: `Credential.redacted()` already
#: exposes `access_token_len`, and a length is a real hint about a secret.
#: Anything the broker starts sending tomorrow is dropped here until someone
#: adds it on purpose.
_ACCOUNT_FIELDS = (
    "account_id",
    "owner_tenant",
    "label",
    "provider",
    "state",
    "reason",
    "lend_to",
    "assigned",
    "observed_at",
    "stale",
)
_WINDOW_FIELDS = ("utilization", "resets_at", "reset")


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    """One account, reduced to the fields this surface is allowed to show.

    The table is defended by construction -- it prints named columns and can
    only show what it asks for -- but `sc --json` dumps the payload as
    fetched, so without this the same account would obey one rule on screen
    and none in a file. `sc` never sees key material today; this is what keeps
    that true when the broker's shape widens.
    """
    out = {key: account[key] for key in _ACCOUNT_FIELDS if key in account}
    windows = account.get("windows")
    if isinstance(windows, dict):
        out["windows"] = {
            name: {key: entry[key] for key in _WINDOW_FIELDS if key in entry}
            for name, entry in windows.items()
            if isinstance(entry, dict)
        }
    return out


def render_accounts(
    accounts: Sequence[dict[str, Any]] | None,
    style: Style,
    now: datetime,
    *,
    error: str | None = None,
) -> list[str]:
    """The pool, in `cs status`'s columns: ACCOUNT | 5H | 7D | CLEARS | STATE."""
    if accounts is None:
        return [
            f"  {style.paint(style.dash + ' the account pool could not be read', 'bad')}",
        ] + detail(error or "no reason reported", style) + detail(
            "no figure is shown because every one of them would be a guess: "
            "utilisation here is UNKNOWN, not zero",
            style,
        )
    if not accounts:
        return [
            f"  {style.paint('no accounts registered', 'warn')}",
            f"    {style.paint('a claude-code runner has no subscription credential to run on', 'dim')}",
        ]

    short_key, long_key = window_keys(accounts)
    columns = [
        Column("account", "ACCOUNT", flex=True),
        # Drop priorities, easiest-to-lose first: CLEARS is derivable from the
        # reset time, STATE is repeated in full by `sc trouble`, and the 7-day
        # window moves slower than the 5-hour one. The 5-hour figure and the
        # account it belongs to are what a narrow screen must still show --
        # dropping the utilisation would leave a table about nothing.
        #
        # USED, ON THE HEAD (OV-1, owner decision 2026-09-25): one polarity on
        # every surface -- the console's Overview, its Accounts table and this
        # -- and every percentage carries its word. The figures were always
        # utilisation; the heads now say so, as the Accounts table's do.
        Column("short", f"{window_heading(short_key) if short_key else '5H'} USED", drop=1),
        Column("long", f"{window_heading(long_key) if long_key else '7D'} USED", drop=2),
        Column("clears", "CLEARS", align="right", drop=4),
        Column("state", "STATE", flex=True, drop=3),
    ]

    rows: list[dict[str, Any]] = []
    for account in accounts:
        account_id = str(account.get("account_id") or account.get("label") or "?")
        short = read_window(account, short_key) if short_key else Reading("5h")
        long = read_window(account, long_key) if long_key else Reading("7d")
        binding = binding_window([short, long])
        rows.append(
            {
                "account": Cell(account_id),
                "short": format_window(short, style),
                "long": format_window(long, style),
                "clears": Cell(
                    format_until(binding.resets_at, now, style) if binding else style.dash,
                    "dim" if binding is None else None,
                ),
                "state": format_account_state(account, style),
            }
        )

    lines = render_table(columns, rows, style)

    for account in accounts:
        if account.get("lend_to"):
            who = ", ".join(str(t) for t in account["lend_to"])
            lines += detail(f"{account.get('account_id')} lends to {who}", style)
    if any(account_is_stale(a) for a in accounts):
        joiner = f" {style.sep} " if style.unicode else "   "
        lines += detail(
            f"~ projected from an older reading{joiner}{style.dash} not measured", style
        )
    return lines


def accounts_subtitle(
    accounts: Sequence[dict[str, Any]] | None,
    style: Style,
    *,
    tenant: str | None = None,
) -> str:
    if accounts is None:
        return "unreadable"
    assigned = sum(int(a.get("assigned") or 0) for a in accounts)
    stale = sum(1 for a in accounts if account_is_stale(a))
    bits = [f"{len(accounts)} account{'s' if len(accounts) != 1 else ''}"]
    bits.append(f"{assigned} agent{'s' if assigned != 1 else ''} assigned")
    if stale:
        bits.append(f"{stale} stale")
    # The pool a tenant may RUN on: the accounts it owns and the ones lent to
    # it. That is a tenant-scoped answer and it is printed beside platform-wide
    # pools, so it says whose it is -- see `scoped`.
    return scoped(style.sep.join(bits).strip(), tenant or listing_tenant(accounts), style)


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------


def _tri_enabled(pool: dict[str, Any]) -> bool | None:
    """True, False, or "the payload did not say".

    `pool.get("enabled", True)` is `.enabled // true` in another language: it
    reports a paused pool as open. The third state exists so that a payload
    which omitted the field renders as a mark instead of as permission.
    """
    if "enabled" not in pool:
        return None
    return pool["enabled"] is not False


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Binding:
    """Which pool actually stops a profile, and how much room is left in it.

    `unknown` means THE ROOM CANNOT BE KNOWN, which is not the same as zero
    and not the same as unlimited. It is set when a required pool did not
    report its ceiling, or did not report whether it is enabled -- a pool that
    may be paused may also refuse the next task outright.
    """

    pool: str | None = None
    room: int | None = None
    limit: int | None = None
    active: int | None = None
    paused: bool = False
    unknown: bool = False
    unconfigured: bool = False


def binding_pool(
    required: Sequence[str],
    pools: dict[str, dict[str, Any]],
    units: int,
) -> Binding:
    """The pool with the least headroom for one more task of this profile.

    Admission is all-or-nothing across `required`, so the ceiling a profile
    actually feels is the tightest of them -- not the global one, which is the
    one people quote. A pool that is absent from `pools` is unlimited by
    construction (admission.py says so); that is a fact, not a gap, so it is
    skipped rather than marked.

    ONE UNGRADEABLE POOL MAKES THE WHOLE ANSWER UNKNOWN, and that is the same
    rule stated from the other side: if admission needs every required pool to
    say yes, then a pool that did not report its ceiling -- or did not report
    whether it is paused -- could be the one that refuses, and the true room
    may be 0. Returning the tightest pool that DID report would print a
    confident number whose only support is that the pool which might have
    contradicted it stayed silent. An unknown pool therefore wins over every
    measured one, whatever order the payload happened to list them in.
    """
    if not required:
        return Binding(unknown=True)

    best: Binding | None = None
    ungradeable: Binding | None = None
    considered = 0
    for name in required:
        pool = pools.get(name)
        if pool is None:
            continue
        considered += 1
        enabled = _tri_enabled(pool)
        limit = _int_or_none(pool.get("effective_limit"))
        active = _int_or_none(pool.get("active"))
        if enabled is False:
            # A paused pool is not unknown: it refuses, now, and saying so is
            # more useful than saying nothing is known.
            return Binding(pool=name, room=0, limit=limit, active=active, paused=True)
        if limit is None or enabled is None:
            # USED is still carried: `3/10` is a measurement even when what it
            # implies about the next task is not.
            if ungradeable is None:
                ungradeable = Binding(pool=name, limit=limit, active=active, unknown=True)
            continue
        available = _int_or_none(pool.get("available"))
        if available is None:
            available = max(0, limit - (active or 0))
        room = available // max(1, units)
        candidate = Binding(pool=name, room=room, limit=limit, active=active)
        if best is None or room < best.room:
            best = candidate
    if ungradeable is not None:
        return ungradeable
    if best is None:
        return Binding(unconfigured=considered == 0)
    return best


def format_room(binding: Binding, style: Style) -> Cell:
    """The mark, never a number, whenever the room is not actually known.

    `unknown` is tested as well as `room is None` on purpose: they mean the
    same thing today, and the day they stop meaning the same thing is the day
    this column would otherwise print an inference as a measurement.
    """
    if binding.paused:
        return Cell("paused", "bad")
    if binding.unconfigured:
        return Cell(style.unlimited, "dim")
    if binding.unknown or binding.room is None:
        return Cell(style.dash, "dim")
    tone = "bad" if binding.room == 0 else ("warn" if binding.room <= 1 else None)
    return Cell(str(binding.room), tone)


def format_used(binding: Binding, style: Style) -> Cell:
    if binding.limit is None or binding.active is None:
        return Cell(style.dash, "dim")
    tone = "bad" if binding.active >= binding.limit else None
    return Cell(f"{binding.active}/{binding.limit}", tone)


def profile_refused(name: str, spec: Any) -> str | None:
    """Why no task of this profile can be dispatched at all, or None.

    ROOM IS ABOUT POOLS, AND A POOL WITH ROOM DOES NOT MAKE A PROFILE
    DISPATCHABLE. On 2026-09-25 (#88, SC-F13) `sc capacity` showed ROOM for
    `codex` while `swarm profiles` and every dispatch refused it: the pools it
    binds on had headroom, and the catalogue has disabled the profile. The
    API's capacity route does not serve a profile's availability (CP-3 holds
    that half), so two sources are read here, the served one first:

      * `available: false` on the profile's own entry, for an API that serves it;
      * the FROZEN catalogue, `swarm_common.profiles.RUNNER_PROFILES` -- the
        same module the API imports, so this is the platform's own answer and
        not a copy of it (profiles.py makes the same argument for `check`).
    """
    if isinstance(spec, dict) and spec.get("available") is False:
        return str(spec.get("disabled_reason") or "the API reports it unavailable")
    profile = RUNNER_PROFILES.get(name)
    if profile is not None and not profile.available:
        return profile.disabled_reason or "disabled in the runner catalogue"
    return None


def render_capacity(
    capacity: dict[str, Any] | None,
    style: Style,
    *,
    error: str | None = None,
    profiles_only: bool = False,
) -> list[str]:
    if capacity is None:
        return [
            f"  {style.paint(style.dash + ' capacity could not be read', 'bad')}",
        ] + detail(error or "no reason reported", style)

    pools = {p["name"]: p for p in capacity.get("pools") or [] if isinstance(p, dict) and p.get("name")}
    profiles = capacity.get("runner_profiles") or {}

    rows: list[dict[str, Any]] = []
    refused: list[str] = []
    for name in sorted(profiles):
        spec = profiles[name] or {}
        if profile_refused(name, spec) is not None:
            # NO ROOM AND NO BINDING POOL. Either would be a figure about a
            # pool presented as a figure about a profile nothing can run.
            refused.append(name)
            rows.append(
                {
                    "profile": Cell(name, "dim"),
                    "class": Cell(str(spec.get("resource_class") or style.dash), "dim"),
                    "backend": Cell(str(spec.get("backend") or style.dash), "dim"),
                    "binds": Cell(style.dash, "dim"),
                    "used": Cell(style.dash, "dim"),
                    "room": Cell("disabled", "bad"),
                }
            )
            continue
        units = _int_or_none(spec.get("units")) or 1
        binding = binding_pool(list(spec.get("pools") or []), pools, units)
        rows.append(
            {
                "profile": Cell(name),
                "class": Cell(str(spec.get("resource_class") or style.dash), "dim"),
                "backend": Cell(str(spec.get("backend") or style.dash), "dim"),
                "binds": Cell(binding.pool or (style.unlimited if binding.unconfigured else style.dash),
                              "dim" if binding.pool is None else None),
                "used": format_used(binding, style),
                "room": format_room(binding, style),
            }
        )

    # UNITS, NOT "USED" (#192). The cell is the binding pool's `active/limit`,
    # and admission adds a task's `units` to `active` on every pool it takes
    # (`swarm_common/admission.py`) -- so it counts units, while ROOM beside it
    # counts agents of the profile. The header says which, and the note under
    # the table says how the two relate.
    lines = render_table(
        [
            Column("profile", "PROFILE", flex=True),
            Column("class", "CLASS", drop=3),
            Column("backend", "BACKEND", drop=4),
            Column("binds", "BINDS ON", flex=True, drop=1),
            Column("used", "UNITS", align="right", drop=2),
            Column("room", "ROOM", align="right"),
        ],
        rows,
        style,
    )
    if not rows:
        lines = [f"  {style.paint('no runner profiles reported', 'warn')}"]
    else:
        if refused:
            lines += detail(
                f"disabled in the runner catalogue, so no pool's room applies: "
                f"{', '.join(refused)}",
                style,
            )
        # THE POPULATION, SAID. A shared pool's figure counts every tenant's
        # work; the tenant-scoped sections around this one count the caller's
        # only. On 2026-09-25 (#88, SC-F5) `claude-code 8/40` -- another
        # tenant's work -- sat beside AGENTS "0 running" with nothing to say
        # the two were different populations.
        #
        # AND THE UNIT, SAID (#192). This note used to say the figure "counts
        # every tenant's agents", so two browser agents at `4/10` read as four.
        tenant = capacity.get("tenant_id")
        own = f"tenant:{tenant}" if tenant else "your own tenant pool"
        lines += detail(units_note(own), style)

    if profiles_only:
        return lines

    pool_rows: list[dict[str, Any]] = []
    for name in sorted(pools):
        pool = pools[name]
        enabled = _tri_enabled(pool)
        limit = _int_or_none(pool.get("effective_limit"))
        active = _int_or_none(pool.get("active"))
        available = _int_or_none(pool.get("available"))
        if enabled is False:
            state = Cell("paused", "bad")
        elif enabled is None:
            state = Cell("? not reported", "warn")
        elif limit == 0:
            state = Cell("closed", "bad")
        elif available == 0:
            state = Cell("full", "warn")
        else:
            state = Cell("open", "dim")
        pool_rows.append(
            {
                "pool": Cell(name),
                "used": Cell(
                    style.dash if limit is None or active is None else f"{active}/{limit}",
                    "dim" if limit is None or active is None else None,
                ),
                "free": Cell(style.dash if available is None else str(available),
                             "dim" if available is None else None),
                "state": state,
            }
        )

    if pool_rows:
        lines = (
            render_table(
                [
                    Column("pool", "POOL", flex=True),
                    # Units, like the profile table's column (#192); FREE is
                    # the pool's free units, which ROOM divides.
                    Column("used", "UNITS", align="right"),
                    Column("free", "FREE", align="right", drop=2),
                    Column("state", "STATE", drop=1),
                ],
                pool_rows,
                style,
            )
            + [""]
            + lines
        )
    return lines


def units_note(own: str) -> str:
    """The sentence under the capacity table: what UNITS and ROOM each count.

    The per-class units are read from `swarm_common.profiles.RESOURCE_CLASSES`
    -- the numbers admission charges -- so the note cannot go on saying 2 the
    day the catalogue says 3.
    """
    per_class = ", ".join(
        f"{name} {resource.units}"
        for name, resource in sorted(RESOURCE_CLASSES.items(), key=lambda kv: (kv[1].units, kv[0]))
    )
    return (
        "UNITS is capacity units in use out of the pool's limit, not agents: an "
        f"agent takes its resource class's units ({per_class}). On a shared pool "
        f"it counts every tenant's; only {own} counts yours alone. ROOM counts "
        "agents: how many more of that profile fit before the binding pool refuses."
    )


def _fit_phrase(room: int) -> str:
    if room <= 0:
        return "no more agents fit"
    if room == 1:
        return "1 more agent fits"
    return f"{room} more agents fit"


def capacity_subtitle(capacity: dict[str, Any] | None, style: Style) -> str:
    """The profile with the fewest agents still to fit -- the real ceiling, named.

    RANKED BY AGENTS, AND SAYS SO (#192). The ranking was always the fewest
    agents of a profile that still fit (ROOM), but the line printed the binding
    pool's UNITS -- `tightest: resource:browser 0/10` -- which named an empty
    pool the tightest because a browser agent takes two units. It now names
    the profile, the agents that fit, and the pool, with its units labelled.

    A pool that could not be graded outranks every measured one, for the same
    reason it does in `binding_pool`: naming a "tightest" pool while another
    required pool's ceiling is unknown is a confident answer with a hole in
    it, and the subtitle is the line people quote.
    """
    if capacity is None:
        return "unreadable"
    pools = {p["name"]: p for p in capacity.get("pools") or [] if isinstance(p, dict) and p.get("name")}
    profiles = capacity.get("runner_profiles") or {}
    paused: Binding | None = None
    ungradeable: Binding | None = None
    tightest: tuple[str, Binding] | None = None
    for name, spec in profiles.items():
        if profile_refused(name, spec) is not None:
            # A profile nothing can dispatch has no ceiling worth quoting, and
            # the subtitle is the line people quote.
            continue
        units = _int_or_none((spec or {}).get("units")) or 1
        binding = binding_pool(list((spec or {}).get("pools") or []), pools, units)
        if binding.pool is None:
            continue
        if binding.paused:
            paused = paused or binding
        elif binding.unknown or binding.room is None:
            ungradeable = ungradeable or binding
        elif tightest is None or binding.room < tightest[1].room:
            tightest = (name, binding)
    if paused is not None:
        return f"{paused.pool} is paused"
    if ungradeable is not None:
        return f"{ungradeable.pool} ceiling unknown"
    if tightest is None:
        return "no pool limits configured"
    name, binding = tightest
    return (
        f"tightest: {name}, {_fit_phrase(binding.room or 0)} on {binding.pool} "
        f"({format_used(binding, style).text} units)"
    )


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------

#: WHICH STATES COUNT AS RUNNING IS THE FROZEN CONTRACT'S ANSWER, NOT THIS
#: MODULE'S. `swarm_common.states` is the single definition of "only
#: LEASED/DISPATCHED/STARTING/RUNNING create infrastructure demand", and a
#: status tool that kept its own copy would go on reporting the old answer for
#: months after the contract gained a state -- under-reporting the one
#: invariant the whole platform is built around. The module is enum-only: no
#: client, no credentials, no network, so importing it keeps this file as
#: offline as it was. The values (not the members) are what is compared,
#: because a task payload carries its state as a plain string.
RUNNING_STATES = frozenset(state.value for state in CONCURRENCY_STATES)
QUEUED_STATES = frozenset(state.value for state in PENDING_STATES)

#: The active states in LIFECYCLE order, for the headline's breakdown. Read off
#: the frozen enum's own order rather than written out, for the reason above.
_ACTIVE_ORDER = tuple(state.value for state in TaskState if state.value in RUNNING_STATES)

#: States a task never leaves. Named FINISHED rather than TERMINAL_STATES on
#: purpose: `test_the_contract_is_not_shadowed_by_a_local_terminal_set` holds
#: this module to not re-exporting that name. It is the contract's set plus the
#: `DEAD_LETTER` spelling older payloads use, the same pair `client.TERMINAL`
#: carries.
FINISHED_STATES = frozenset(state.value for state in _states.TERMINAL_STATES) | {"DEAD_LETTER"}

_STATE_TONE = {
    "RUNNING": "good",
    "STARTING": "good",
    "DISPATCHED": "good",
    "LEASED": "good",
    "PARKED": "warn",
    "READY": None,
    "QUEUED": "dim",
    "SUBMITTED": "dim",
    "FAILED": "bad",
    "DEAD_LETTERED": "bad",
    "DEAD_LETTER": "bad",
    "CANCELLED": "dim",
    "SUCCEEDED": "good",
}


def task_id_of(task: dict[str, Any]) -> str:
    """`task_to_api` says `id`; a dispatch response says `task_id`.

    Both shapes reach this renderer, and guessing wrong prints a column of
    question marks over perfectly good data.
    """
    return str(task.get("id") or task.get("task_id") or "?")


#: HOW MANY CHARACTERS OF A TASK ID A LINE IS LABELLED WITH -- one number, for
#: every surface. On 2026-09-25 (#88, SC-F15) one task was shown three ways in
#: one session: in full by `swarm workflow`, as its last 8 by `tail` and
#: `follow`, and as its last 10 by `sc agents`, so matching a line in one to a
#: row in another meant counting characters. Eight, because the id's random
#: tail is what distinguishes it and eight hex characters of it do.
SHORT_ID_CHARS = 8

#: A step id is the caller's own name for the step and can be as long as they
#: like; the label is a prefix, so it is clipped before it crowds out the id.
_STEP_LABEL_CHARS = 12


def short_id(task_id: str, keep: int = SHORT_ID_CHARS) -> str:
    return task_id if len(task_id) <= keep else task_id[-keep:]


def task_label(task_id: Any, step_id: Any = None) -> str:
    """How a line names one task: `b 4674b39f` for a workflow step, else `4674b39f`.

    THE STEP ID FIRST, because it is the name the reader chose and the one
    their spec uses. A workflow's steps were shown by task id alone, which
    nobody has written down anywhere, so reading `tail` for a five-step
    workflow meant keeping the id-to-step map in your head (#88, SC-F15).
    """
    short = short_id(str(task_id or "?"))
    step = str(step_id or "").strip()
    if not step:
        return short
    if len(step) > _STEP_LABEL_CHARS:
        step = step[: _STEP_LABEL_CHARS - 2] + ".."
    return f"{step} {short}"


def describe_blocker(blocker: Any) -> str:
    """One `blocked_by` entry as words: `provider concurrency limit (provider:anthropic)`.

    The scheduler writes each entry as `{"pool": ..., "reason": ...}`. Printed
    as it is stored, that is a Python dict repr, which is what `swarm
    workflow-status` printed on 2026-09-25 (#88, SC-F16).
    """
    if isinstance(blocker, dict):
        pool = blocker.get("pool") or ""
        reason = str(blocker.get("reason") or "").lower().replace("_", " ")
        return f"{reason} ({pool})" if pool else reason
    return str(blocker)


def describe_blockers(blocked: Any) -> list[str]:
    """Every `blocked_by` entry, each through `describe_blocker`."""
    if not blocked:
        return []
    items = blocked if isinstance(blocked, list) else [blocked]
    return [describe_blocker(item) for item in items]


def task_note(task: dict[str, Any], style: Style) -> Cell:
    park = task.get("park_reason")
    if park:
        return Cell(str(park).lower().replace("_", " "), "warn")
    blocked = describe_blockers(task.get("blocked_by"))
    if blocked:
        return Cell(blocked[0], "warn")
    error = task.get("last_error")
    if error:
        return Cell(str(error).splitlines()[0], "bad")
    return Cell("", None)


def render_agents(
    tasks: Sequence[dict[str, Any]] | None,
    style: Style,
    now: datetime,
    *,
    error: str | None = None,
) -> list[str]:
    if tasks is None:
        return [
            f"  {style.paint(style.dash + ' agents could not be listed', 'bad')}",
        ] + detail(error or "no reason reported", style)

    running = [t for t in tasks if t.get("state") in RUNNING_STATES]
    queued = [t for t in tasks if t.get("state") in QUEUED_STATES]
    # What the listing did NOT read, said under whatever it did. A table built
    # from part of the tenant's tasks and presented as the whole is the
    # defect (#88, SC-F11); the sentence is how a reader knows which it is.
    gaps: list[str] = []
    for gap in listing_gaps(tasks):
        gaps += detail(gap, style, tone="warn")
    if not running and not queued:
        return [f"  {style.paint('nothing running, nothing queued', 'dim')}"] + gaps

    def rows_for(items: Sequence[dict[str, Any]], anchor: str) -> list[dict[str, Any]]:
        out = []
        for task in items:
            state = str(task.get("state") or "?")
            metadata = task.get("metadata") or {}
            out.append(
                {
                    "task": Cell(task_label(task_id_of(task), task.get("step_id"))),
                    # AS THE API SPELLS IT. `swarm status`, `swarm result`,
                    # `workflow-status` and `follow` all print the state in
                    # upper case, and this table and `sc task` printed it in
                    # lower -- two spellings of one vocabulary (#88, SC-F19).
                    "state": Cell(state, _STATE_TONE.get(state, None)),
                    "profile": Cell(str(task.get("runner_profile") or style.dash), "dim"),
                    "label": Cell(str(metadata.get("unit") or ""), "dim"),
                    "age": Cell(format_age(task.get(anchor) or task.get("created_at"), now, style),
                                "dim"),
                    "note": task_note(task, style),
                }
            )
        return out

    columns = [
        # FLEXIBLE, and never dropped: a workflow step's label carries its step
        # id, and at the narrowest widths truncating that beats overflowing.
        Column("task", "TASK", flex=True),
        Column("state", "STATE"),
        Column("profile", "PROFILE", drop=3),
        Column("label", "LABEL", flex=True, drop=4),
        Column("age", "AGE", align="right", drop=2),
        Column("note", "NOTE", flex=True, drop=1),
    ]

    lines: list[str] = []
    if running:
        lines += render_table(columns, rows_for(running, "started_at"), style)
    if queued:
        if running:
            lines.append("")
        lines.append(style.paint(f"  queued ({len(queued)})", "dim"))
        lines += render_table(columns, rows_for(queued, "created_at"), style, heading=False)
    return lines + gaps


def active_headline(tasks: Sequence[dict[str, Any]]) -> str:
    """`2 active (2 dispatched, 0 running)` -- demand, and how much of it runs.

    "RUNNING" WAS THE WRONG WORD FOR THIS COUNT. It is every state that holds
    capacity (`CONCURRENCY_STATES`, invariant 1), which is right for demand and
    wrong as a description: on 2026-09-25 (#88, SC-F14) the headline said "2
    running" over two tasks that were DISPATCHED and waiting for a container.
    The total is kept -- it is what the pools are charged for -- and the
    breakdown says how much of it is actually running. RUNNING is always
    named, as 0 when it is 0, because that zero is the finding.
    """
    counts: dict[str, int] = {}
    for task in tasks:
        state = task.get("state")
        if state in RUNNING_STATES:
            counts[state] = counts.get(state, 0) + 1
    running = TaskState.RUNNING.value
    parts = [
        f"{counts[state]} {state.lower()}"
        for state in _ACTIVE_ORDER
        if state != running and counts.get(state)
    ]
    parts.append(f"{counts.get(running, 0)} running")
    return f"{sum(counts.values())} active ({', '.join(parts)})"


def agents_subtitle(
    tasks: Sequence[dict[str, Any]] | None,
    style: Style,
    *,
    tenant: str | None = None,
) -> str:
    if tasks is None:
        return "unreadable"
    queued = sum(1 for t in tasks if t.get("state") in QUEUED_STATES)
    text = f"{active_headline(tasks)}{style.sep}{queued} queued"
    if listing_gaps(tasks):
        text += f"{style.sep}incomplete"
    return scoped(text, tenant or listing_tenant(tasks), style)


# --------------------------------------------------------------------------
# One task
# --------------------------------------------------------------------------


def render_task(
    task: dict[str, Any] | None,
    style: Style,
    now: datetime,
    *,
    error: str | None = None,
    patch: str | None = None,
    no_patch_because: str | None = None,
) -> list[str]:
    if task is None:
        return [
            style.paint(f"{style.dash} that task could not be read", "bad"),
        ] + detail(error or "no reason reported", style, indent=2)

    state = str(task.get("state") or "?")
    # NEVER A BARE DASH FOR "IT NEVER STARTED". The dash means "not measured",
    # and a step cascade-cancelled before it was attempted HAS been measured:
    # it has no start because there was none. `started —` said the opposite
    # (#88, SC-F19).
    if task.get("started_at"):
        started = f"started {format_ago(task.get('started_at'), now, style)}"
    elif state in FINISHED_STATES:
        started = "never started"
    else:
        started = "not started yet"
    lines = [
        # Upper case, as the API and every `swarm` command spell a state.
        f"{style.paint(task_id_of(task), 'bold')}  {style.paint(state, _STATE_TONE.get(state))}",
        style.paint(
            f"  profile   {task.get('runner_profile') or style.dash}"
            f"   attempt {task.get('attempt_count', style.dash)}"
            f"/{task.get('max_attempts', style.dash)}",
            "dim",
        ),
        style.paint(
            f"  created   {format_ago(task.get('created_at'), now, style)}   {started}",
            "dim",
        ),
    ]
    if task.get("step_id"):
        lines.append(
            style.paint(
                f"  step      {task['step_id']} of {task.get('workflow_id') or style.dash}",
                "dim",
            )
        )
    note = task_note(task, style)
    if note.text:
        lines.append(f"  {style.paint('why', 'dim')}       {style.paint(note.text, note.tone)}")

    git = ((task.get("result_summary") or {}).get("git")) or {}
    if not git:
        lines.append(f"  {style.paint('code', 'dim')}      {no_patch_because or style.dash + ' nothing recorded yet'}")
        return lines

    commits = git.get("commit_count")
    lines.append(f"  {style.paint('base', 'dim')}      {git.get('base') or style.dash}")
    lines.append(
        f"  {style.paint('commits', 'dim')}   "
        f"{style.dash if commits is None else commits}"
        f"   +{git.get('insertions', 0)}/-{git.get('deletions', 0)}"
    )
    for commit in git.get("commits") or []:
        sha = str(commit.get("sha") or "")[:10]
        lines.append(f"    {style.paint(sha, 'dim')}  {_fit(str(commit.get('subject') or ''), max(20, style.usable - 16), style)}")
    if git.get("dirty_count"):
        lines.append(f"  {style.paint('uncommitted', 'warn')} {git['dirty_count']} file(s)")
    lines.append(f"  {style.paint('patch', 'dim')}     {patch or no_patch_because or style.dash}")
    pr = git.get("pull_request")
    if pr:
        lines.append(f"  {style.paint('pr', 'dim')}        {pr.get('url') or ('#' + str(pr.get('number')))}")
    else:
        reason = git.get("publish_reason") or "no reason recorded"
        tail = style.sep.strip() + " " + str(reason)
        lines.append(f"  {style.paint('pr', 'dim')}        none {style.paint(tail, 'dim')}")
    return lines


# --------------------------------------------------------------------------
# Trouble
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    severity: str
    where: str
    what: str

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity) if self.severity in SEVERITIES else len(SEVERITIES)


@dataclass
class Snapshot:
    """Everything `sc` fetched, and what failed while fetching it.

    A field is None when it could not be read and `[]`/`{}` when it was read
    and was empty. Those are different answers and the renderer treats them
    differently, so the fetcher must never turn a failure into an empty list.
    """

    tenant: dict[str, Any] | None = None
    tenant_error: str | None = None
    stats: dict[str, Any] | None = None
    stats_error: str | None = None
    capacity: dict[str, Any] | None = None
    capacity_error: str | None = None
    accounts: list[dict[str, Any]] | None = None
    accounts_error: str | None = None
    #: True when the API answered "there is no such route" for /v1/accounts.
    #: That is what EVERY deployment older than the accounts proxy looks like,
    #: and it is not a broken cluster: the pool is unreadable from here, the
    #: rest of the platform is untouched. Kept apart from `accounts_error`
    #: because the two produce the same sentence on screen and must not
    #: produce the same severity -- grading an absent route as "down" turns
    #: the first `sc trouble` run on current production into a red screen.
    accounts_absent: bool = False
    tasks: list[dict[str, Any]] | None = None
    tasks_error: str | None = None
    api_url: str = ""
    tier: str = ""
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def find_trouble(snap: Snapshot, style: Style = PLAIN) -> list[Finding]:
    """Everything wrong right now, worst first.

    An area that could not be READ is itself a finding. Silence about a
    subsystem is how an operator concludes it is fine.
    """
    out: list[Finding] = []
    now = snap.now
    # WHOSE, on every finding about a tenant-scoped figure. The trouble list
    # mixes the caller's tasks and accounts with pools every tenant shares, and
    # a finding that does not say which it is about reads as the platform's
    # when it is the tenant's, or the other way round (#88, SC-F5).
    tenant = scope_tenant(snap)
    whose = f" of tenant {tenant}" if tenant else ""

    for area, error in (
        ("identity", snap.tenant_error),
        ("stats", snap.stats_error),
        ("capacity", snap.capacity_error),
        ("accounts", snap.accounts_error),
        ("agents", snap.tasks_error),
    ):
        if not error:
            continue
        # "I could not read it" is a finding; "this deployment does not have
        # that route" is a fact about the deployment. Only the first one is a
        # reason to call the cluster down.
        absent = area == "accounts" and snap.accounts_absent
        out.append(Finding("note" if absent else "down", area, f"unreadable {style.dash} {error}"))

    if snap.stats is not None and snap.stats.get("dispatch_paused"):
        out.append(Finding("down", "dispatch", "paused platform-wide; nothing will be admitted"))

    if snap.accounts is not None:
        if not snap.accounts:
            out.append(
                Finding(
                    "warn",
                    "accounts",
                    f"none registered{' for tenant ' + tenant if tenant else ''}; "
                    "a claude-code runner has no credential to run on",
                )
            )
        for account in snap.accounts:
            ident = str(account.get("account_id") or account.get("label") or "?")
            state = str(account.get("state") or "")
            if state == "REAUTH_REQUIRED":
                out.append(Finding(
                    "down", ident,
                    f"needs re-authentication {style.dash} "
                    f"{account.get('reason') or 'no reason given'}",
                ))
            elif state == "PAUSED":
                out.append(Finding(
                    "warn", ident,
                    f"paused {style.dash} {account.get('reason') or 'no reason given'}",
                ))
            elif state == "DRAINING":
                out.append(Finding("note", ident, "draining; it takes no new agents"))
            if account_is_stale(account):
                seen = account.get("observed_at")
                out.append(
                    Finding(
                        "warn",
                        ident,
                        "no reading has ever arrived; its utilisation is unknown"
                        if seen is None
                        else f"last read {format_ago(seen, now, style)}; figures are projections",
                    )
                )
            for key in (account.get("windows") or {}):
                reading = read_window(account, key)
                if reading.known and reading.utilization >= 0.98 and not reading.reset:
                    out.append(
                        Finding(
                            "warn" if reading.utilization < 1.0 else "down",
                            ident,
                            # "used", because every percentage carries its word
                            # (OV-1): a bare "98%" beside a window name reads
                            # as either room or use.
                            f"{window_heading(key)} at {format_percent(reading, style)} used"
                            f", clears in {format_until(reading.resets_at, now, style)}",
                        )
                    )

    if snap.capacity is not None:
        for pool in snap.capacity.get("pools") or []:
            if not isinstance(pool, dict) or not pool.get("name"):
                continue
            name = pool["name"]
            enabled = _tri_enabled(pool)
            limit = _int_or_none(pool.get("effective_limit"))
            active = _int_or_none(pool.get("active"))
            available = _int_or_none(pool.get("available"))
            if enabled is False:
                out.append(Finding("warn", name, "pool paused; admission into it is refused"))
                continue
            if enabled is None:
                out.append(Finding("note", name, "pool did not report whether it is enabled"))
            # A pool named for a tenant is that tenant's alone; every other
            # pool is shared, and its `active` is every tenant's agents.
            shared = not (name.startswith("tenant:") or ":tenant:" in name)
            if limit == 0:
                out.append(Finding("down", name, "effective limit is 0; nothing can be admitted"))
            elif available == 0 and limit:
                # UNITS, as the capacity table now labels them (#192): a pool
                # of 10 is full at five browser agents, not ten.
                out.append(
                    Finding(
                        "warn",
                        name,
                        f"full at {active}/{limit} units"
                        + (", counting every tenant's" if shared else ""),
                    )
                )

    if snap.tasks is not None:
        parked: dict[str, int] = {}
        dead = 0
        failed = 0
        for task in snap.tasks:
            state = task.get("state")
            if state == "PARKED":
                reason = str(task.get("park_reason") or "no reason recorded")
                parked[reason] = parked.get(reason, 0) + 1
            elif state in ("DEAD_LETTERED", "DEAD_LETTER"):
                dead += 1
            elif state == "FAILED":
                failed += 1
        for reason, count in sorted(parked.items(), key=lambda kv: -kv[1]):
            out.append(
                Finding("warn", "parked", f"{count} task(s){whose}: {reason.lower().replace('_', ' ')}")
            )
        # THE WINDOW, NAMED (#190). Parked tasks are live and every live task
        # is read; failed and dead-lettered ones are finished and come only
        # from the newest-N page, so their counts are over that page, and the
        # finding says which page -- "in the window listed" named none.
        scope, since = window_scope(snap.tasks, whose, now)
        if dead:
            out.append(
                Finding("down", "dead-lettered", f"{dead} {scope} gave up after every attempt{since}")
            )
        if failed:
            out.append(Finding("note", "failed", f"{failed} {scope} failed{since}"))

    out.sort(key=lambda f: (f.rank, f.where))
    return out


#: The widest the WHERE column may grow, as a fraction of the screen. One
#: account id long enough to fill half the line must not push every reason on
#: the screen down to two words.
_WHERE_SHARE = 3


def render_trouble(findings: Sequence[Finding], style: Style) -> list[str]:
    """One finding per row, with the reason WRAPPED rather than truncated.

    This is deliberately not a table. A transport error -- `GET /v1/accounts
    -> 404: ...` -- is the longest string `sc` ever prints and the one it most
    needs to print whole: truncated at an 80-column edge it keeps the half
    that says something is unreadable and loses the half that says which thing
    and what to do about it. The overview already prints those in full through
    `detail()`; before this, the same text was shorter in `sc trouble`, which
    is the view an operator opens precisely to read it.
    """
    if not findings:
        return [f"  {style.paint('nothing wrong right now', 'good')}"]

    indent = 2
    gutter = 2
    label = min(
        max(len(f.where) for f in findings),
        max(6, style.usable // _WHERE_SHARE),
    )
    lead = indent + 2 + label + gutter  # "  x " + WHERE + gutter
    body = max(8, style.usable - lead)

    lines: list[str] = []
    for finding in findings:
        tone = _SEVERITY_TONE.get(finding.severity)
        mark = _SEVERITY_MARK.get(finding.severity, "-")
        where = _fit(finding.where, label, style).ljust(label)
        head = style.paint(f"{' ' * indent}{mark} {where}", tone)
        what_tone = "dim" if finding.severity == "note" else None
        # `break_long_words` is textwrap's default and is load-bearing here: a
        # URL or a path with no spaces in it would otherwise run off the edge
        # of the screen the wrapping exists to fit.
        wrapped = textwrap.wrap(finding.what, body) or [""]
        lines.append((head + " " * gutter + style.paint(wrapped[0], what_tone)).rstrip())
        for continuation in wrapped[1:]:
            lines.append((" " * lead + style.paint(continuation, what_tone)).rstrip())
    return lines


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


def render_header(snap: Snapshot, style: Style) -> list[str]:
    # `tenant_of`, not `.get("tenant_id")`: the identity route nests it, and
    # the flat read showed a tenant that WAS read as not measured (#88, SC-F4).
    tenant = scope_tenant(snap) or style.dash
    # Ordered by what an operator needs when only part of it fits: the tenant
    # they are acting as first, then the tier that explains a refusal, then the
    # URL, which is the longest and the least often surprising.
    bits = ["SwarmCloud", str(tenant)]
    if snap.tier:
        bits.append(snap.tier)
    if snap.api_url:
        bits.append(snap.api_url)
    joiner = " · " if style.unicode else "  "
    while len(bits) > 2 and len(joiner.join(bits)) > style.usable:
        bits.pop()
    line = _fit(joiner.join(bits), style.usable, style)
    return [style.paint(line, "bold") if style.color else line]


def render_overview(snap: Snapshot, style: Style) -> list[str]:
    now = snap.now
    tenant = scope_tenant(snap)
    lines = render_header(snap, style)
    lines.append("")

    lines.append(
        section("accounts", accounts_subtitle(snap.accounts, style, tenant=tenant), style)
    )
    lines += render_accounts(snap.accounts, style, now, error=snap.accounts_error)
    lines.append("")

    lines.append(section("capacity", capacity_subtitle(snap.capacity, style), style))
    lines += render_capacity(snap.capacity, style, error=snap.capacity_error, profiles_only=True)
    lines.append("")

    lines.append(section("agents", agents_subtitle(snap.tasks, style, tenant=tenant), style))
    lines += render_agents(snap.tasks, style, now, error=snap.tasks_error)
    lines.append("")

    findings = find_trouble(snap, style)
    worst = findings[0].severity if findings else "ok"
    lines.append(section("trouble", "" if worst == "ok" else f"{len(findings)} finding(s)", style))
    lines += render_trouble(findings, style)
    return lines


def join(lines: Sequence[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"
