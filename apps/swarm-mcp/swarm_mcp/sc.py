"""`sc` -- what the SwarmCloud cluster is doing right now, in one screen.

WHY THIS IS A SEPARATE COMMAND FROM `swarm`. `swarm` is imperative: dispatch,
tail, apply, integrate. `sc` is interrogative and read-only TOWARDS THE
CLUSTER -- it never writes to it, never refreshes an account's credential and
never cancels anything. Keeping the two apart means `sc` can be reached for
without reading the flags first, which is the only way a status command gets
used at the moment it is needed.

THE ONE EXCEPTION IS `sc account ...` (S10, owner decision 2026-10-01): pause,
resume, drain and remove one of your own accounts, set how many agents it may
hold (`max-agents`, #2222), and add one through the API's browser sign-in. The
API served those routes and nothing reached them. They are a separate
subcommand, never a view, held out of every skill's grant by
`test_plugin_commands.py`, and `remove` and `max-agents` take the label typed
back.

THE SECOND IS THE ISSUE RUN (#454): `sc run --issue owner/repo#N` creates a
run -- one planner task -- and `sc plan approve|edit|reject <run>` moves its
plan. A plan holds no capacity until it is approved, and an approval sends the
digest of the plan `sc` PRINTED, after `approve` is typed back. Held out of
every grant exactly as the account verbs are; `sc runs`, `sc run show` and
`sc plan show` are views.

THE THIRD IS ONBOARDING (#780, lane OB9): `sc setup` walks the person through
connecting GitHub as themselves, enabling owners and choosing repositories,
and `sc access` grants, revokes and verifies them later. They change what
SwarmCloud may do as that person, so they are held out of every grant as the
account verbs are; `sc setup status`, `sc access list`, `orgs` and `repos`
read. No token, code or state is printed (see the section's own note).
`sc setup token --owner <org>` (D5) is the one verb a token goes IN through:
read from stdin, never an argument, posted once and never printed.

THE FOURTH IS `sc admin purge` (owner request 2026-10-11): the audited purge
of FAILED, CANCELLED and DEAD_LETTERED history (`POST /v1/admin/history:purge`,
full admin only). It prints the API's dry run -- every workflow and task it
would delete, and why the rest were kept -- and deletes nothing unless
`--delete` is given AND `purge` is typed at a terminal; SWARM_ASSUME_YES is
ignored. Held out of every grant as the account verbs are.

IT ALSO SAYS WHICH CLUSTER, AND WHO YOU ARE ON IT (2026-09-25). `sc context`,
`sc login`, `sc logout` and `sc whoami` are the `kubectl config` / `gh auth`
half: which deployment this machine talks to (config.py) and the developer's
own sign-in to it (signin.py). They write only the developer's OWN state -- a
config file and a credential-store entry -- and never the cluster, so the
promise above holds. No skill or slash command may grant them to a model:
`sc login` opens a browser and blocks, `sc logout` revokes a sign-in, and
`sc context use` moves every later call to another cluster. That is why the
`sc` skill and `/sc` are granted each VIEW by name rather than `sc` as a
prefix -- a prefix grant covers these too, and did until review caught it --
and why `overview` exists as a named view: it is the one view a flag could
not otherwise follow without the grant also matching `sc --json login`.
`test_plugin_commands.py` compiles every grant the way Claude Code matches it
and runs each command it allows through these parsers to see what it reaches.

THE DIVISION OF LABOUR WITH render.py IS THE POINT. Everything in this file
does I/O and decides nothing about presentation; everything in `render.py`
decides presentation and does no I/O. That is what makes the stale-reading rule
testable: a stale reading cannot be summoned from a live cluster on demand, so
if the rule lived next to the fetch it would only ever be checked by hand.

FETCH FAILURE IS NOT AN EMPTY RESULT. Every fetcher here returns
`(value, None)` or `(None, why)`, and never `([], why)`. An empty list is a
measurement -- "there are no accounts" -- and a renderer that received one in
place of a failure would print a clean, confident, wrong screen.

`sc` NEVER SEES KEY MATERIAL. It reads `/v1/accounts`, whose documented shape
carries no token and not even a token's length, and it has no route that could
return one. Refreshing a credential REVOKES the previous one, so the broker is
the platform's single writer; a status command has no business anywhere near
that, and this one has no code path that could reach it.

IT ALSO NEVER SENDS ITS CREDENTIAL ANYWHERE BUT THE API. Every request goes
through the one `SwarmClient` this process built, whose bearer token is minted
for that client's audience. There is no environment variable here that names a
second host to try, because forwarding an ID token minted for swarm-api to a
host named by a variable is how a bearer credential ends up somewhere it can
be replayed from.

IT ACTS IN THE TENANT SWARM_TENANT NAMES (SC-TENANT, owner decision
2026-10-12: "sc reads SWARM_TENANT"). Once #1204 made a person's ready
personal workspace their default tenant, a client that sends no
`X-Swarm-Tenant` moved off the group it used to read. So `sc` chooses as the
bridge and `swarm` do -- `client.chosen_tenant`, read once per invocation --
and every request it makes names that tenant; unset or blank, it sends no
header and the API's default applies. `sc whoami` and `sc config` say which
tenant answered and which setting chose it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from swarm_common.states import CONCURRENCY_STATES, PENDING_STATES, TERMINAL_STATES, TaskState

from . import board, keys, render, session
from .client import SwarmClient, SwarmError, chosen_tenant, outputs_of
from .follow import with_console
from .invocation import help_command, terminal_command
from .patches import explain_absence, masked_counts, patch_uri
from .render import Finding, Snapshot, Style

#: THE EXIT CODES ARE THE MACHINE-READABLE HALF OF THIS SURFACE, so all five
#: commands answer the same question the same way:
#:
#:   0  everything this view printed was read, and nothing is down
#:   1  something this view is about could not be read -- including the case
#:      where `sc` could not connect at all and printed nothing
#:   3  it was all read, and something is DOWN
#:
#: 1 and 3 are different facts and a wrapper needs both: "I could not ask" is
#: an alert about the path to the cluster, "it is broken" is an alert about
#: the cluster. Collapsing either into 0 is what makes `sc || alert` useless,
#: and that is exactly what the default view used to do.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_TROUBLE = 3
#: The line itself was refused and nothing was sent -- argparse's own code for
#: a usage error. `sc key set` uses it for a key typed on the command line.
EXIT_USAGE = 2


# --------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------


def style_for(
    stream: Any,
    *,
    width: int | None = None,
    color: bool | None = None,
    ascii_only: bool | None = None,
) -> Style:
    """Decide the three presentation facts, here, where the terminal is.

    NO COLOUR WHEN STDOUT IS NOT A TTY. `sc | grep`, `sc > file` and `sc` read
    by an MCP host all get plain text; SGR codes in a pipe are noise that also
    breaks any downstream column arithmetic. NO_COLOR is honoured because it is
    the convention, FORCE_COLOR because `sc | less -R` is a real thing people do.

    WIDTH FALLS BACK TO 80, not to the terminal's size, when stdout is not a
    terminal: there is no terminal to measure, and 80 is the width every other
    tool assumes in that case.
    """
    is_tty = bool(getattr(stream, "isatty", lambda: False)())

    if color is None:
        if os.environ.get("FORCE_COLOR", "").strip():
            color = True
        elif os.environ.get("NO_COLOR") is not None:
            color = False
        else:
            color = is_tty

    if width is None:
        columns = os.environ.get("COLUMNS", "").strip()
        if columns.isdigit():
            width = int(columns)
        elif is_tty:
            width = shutil.get_terminal_size((80, 24)).columns
        else:
            width = 80

    if ascii_only is None:
        encoding = (getattr(stream, "encoding", "") or "").lower()
        # A terminal under LANG=C raises UnicodeEncodeError on the bar glyphs,
        # which would turn `sc` into a traceback at the moment it is needed.
        ascii_only = "utf" not in encoding.replace("-", "")

    return Style(width=max(20, width), color=bool(color), unicode=not ascii_only)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _attempt(fn: Callable[[], Any]) -> tuple[Any, SwarmError | None]:
    """Run a fetch, returning `(value, None)` or `(None, error)`.

    The EXCEPTION is returned rather than its text, because what went wrong
    has to be graded as well as printed -- a route this deployment does not
    have is not the same finding as a route that failed -- and re-deriving
    that from a formatted message would put a string search on the path that
    decides whether the screen turns red.
    """
    try:
        return fn(), None
    except SwarmError as exc:
        return None, exc
    except Exception as exc:  # pragma: no cover - transport level
        return None, SwarmError(f"{type(exc).__name__}: {exc}")


def fetch_tenant(client: SwarmClient) -> dict[str, Any]:
    return client.request("GET", "/v1/tenants/me")


def fetch_stats(client: SwarmClient) -> dict[str, Any]:
    return client.request("GET", "/v1/stats")


def fetch_capacity(client: SwarmClient) -> dict[str, Any]:
    return client.request("GET", "/v1/capacity")


#: One page of `/v1/tasks`. The API clamps it to its own `max_page_size`, and
#: `next_page_token` carries on from wherever that left it.
TASK_PAGE = 100

#: Pages followed for ONE state before `sc` stops and says it stopped. A tenant
#: with two thousand queued tasks has a problem `sc` should report rather than
#: download; the sentence it leaves in `Listing.incomplete` is the report.
MAX_PAGES_PER_STATE = 20


def _live_states() -> list[str]:
    """Every state a task can still leave, in the frozen enum's order.

    Read from the contract, as `render.RUNNING_STATES` is: a status tool with
    its own list goes on missing a state for months after the contract gains
    one.
    """
    return [
        state.value
        for state in TaskState
        if state in CONCURRENCY_STATES or state in PENDING_STATES
    ]


def _tasks_page(client: SwarmClient, params: list[tuple[str, str]]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    data = client.request("GET", f"/v1/tasks?{query}")
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise SwarmError("GET /v1/tasks answered without a `tasks` field")
    return data


def _every_task_in(client: SwarmClient, state: str) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """All of this tenant's tasks in one state: `(tasks, tenant_id, gap)`.

    `gap` is None when every page was read, and a sentence when the page
    ceiling stopped the walk -- never a silent short list.
    """
    tasks: list[dict[str, Any]] = []
    tenant: str | None = None
    token: str | None = None
    for _ in range(MAX_PAGES_PER_STATE):
        params = [("state", state), ("limit", str(TASK_PAGE))]
        if token:
            params.append(("page_token", token))
        data = _tasks_page(client, params)
        tasks += [t for t in data["tasks"] if isinstance(t, dict)]
        tenant = tenant or data.get("tenant_id")
        token = data.get("next_page_token")
        if not token:
            return tasks, tenant, None
    return tasks, tenant, (
        f"more than {len(tasks)} {state} tasks exist; sc read the newest "
        f"{len(tasks)} and stopped, so the {state} count is a floor, not a total"
    )


def fetch_tasks(client: SwarmClient, *, limit: int = TASK_PAGE) -> render.Listing:
    """This tenant's tasks: EVERY live one, plus the newest `limit` of any state.

    IT READ THE NEWEST 100 AND CALLED THEM THE TENANT'S TASKS. On 2026-09-25
    (#88, SC-F11) the tenant had 324; `sc agents` read 100 of them and printed
    "N running · M queued" as though it had counted all of them, so a task
    older than the newest hundred -- a long PARKED step, a stuck DISPATCHED one
    -- could not appear however alive it was.

    So the LIVE states are asked for BY NAME (`?state=`, which the route has
    always taken) and each is paged to the end with `next_page_token`. Those
    are what AGENTS and the parked findings are about, and they are now
    complete. The newest-`limit` window is still read beside them, for the one
    thing that is about recent history rather than about now: the trouble
    view's FAILED and DEAD_LETTERED counts, which name that window -- how many
    tasks, and created since when (`render.TaskWindow`, #190).

    Concurrently, because it is nine round trips where it was one, and a
    status command that takes nine seconds gets replaced by a guess. The first
    failure is raised as it came -- `collect` records it, and a failure is
    never an empty list.
    """
    jobs: dict[str, Callable[[], Any]] = {
        state: (lambda state=state: _every_task_in(client, state)) for state in _live_states()
    }
    jobs["recent"] = lambda: _tasks_page(client, [("limit", str(limit))])
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        results = {name: future.result() for name, future in futures.items()}

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    tenant: str | None = None
    gaps: list[str] = []

    def take(items: list[dict[str, Any]]) -> None:
        for task in items:
            key = render.task_id_of(task)
            if key in seen and key != "?":
                continue
            seen.add(key)
            merged.append(task)

    for state in _live_states():
        tasks, answered_for, gap = results[state]
        tenant = tenant or answered_for
        if gap:
            gaps.append(gap)
        take(tasks)
    recent = results["recent"]
    tenant = tenant or recent.get("tenant_id")
    newest = [t for t in recent["tasks"] if isinstance(t, dict)]
    take(newest)
    # THE WINDOW SAYS WHAT IT IS (#190): how many the page held, whether that
    # was every task (no next page), and when the oldest was created. A FAILED
    # or DEAD_LETTERED count is taken over this page and nothing else, and the
    # trouble view names it from here.
    created = [
        stamp for stamp in (render.parse_time(t.get("created_at")) for t in newest)
        if stamp is not None
    ]
    window = render.TaskWindow(
        limit=limit,
        count=len(newest),
        oldest=min(created).isoformat() if created else None,
        whole=not recent.get("next_page_token"),
    )
    return render.Listing(merged, tenant_id=tenant, incomplete=gaps, window=window)


def fetch_task(client: SwarmClient, task_id: str) -> dict[str, Any]:
    """`GET /v1/tasks/{id}` answers `{"task": {...}}`.

    Unwrapped by the CLIENT, in one place, rather than by a second rule kept
    here. This file used to hold its own copy; the copy was right and the
    client's absence of one was wrong, so every `swarm` command read the
    envelope as the task while `sc` read it correctly.
    """
    return client.task(task_id)


#: The read the console's Overview "Needs a look" takes its Leases check from
#: (`swarm-ui/src/api.ts` `loadLeases`), with the same window, so a lease held
#: past its TTL is counted over the same rows on both surfaces (#532, 5b).
LEASES_PATH = "/v1/admin/leases?active_only=true&limit=200"


class LeasesRefused(SwarmError):
    """The API refused the lease read rather than failing it.

    `/v1/admin/leases` is admin-gated, as the console's check is, so a caller
    who is not an admin gets a 403 on every run; a deployment older than the
    route gets the API's own 404. Neither says anything is wrong with a
    lease, so the caller grades it as information, as `AccountsRouteAbsent`.
    """


def fetch_leases(client: SwarmClient) -> dict[str, Any]:
    """Every unreleased lease the console's Overview reads, as one page."""
    try:
        data = client.request("GET", LEASES_PATH)
    except SwarmError as exc:
        if exc.status == 403 or (exc.status == 404 and not exc.edge):
            raise LeasesRefused(
                f"{exc} -- /v1/admin/leases is admin-gated, as the console's "
                "Leases check is; a lease held past its TTL is UNKNOWN from "
                "here, not absent"
                if exc.status == 403
                else f"{exc} -- this deployment's swarm-api has no "
                "/v1/admin/leases route"
            ) from None
        raise
    if not isinstance(data, dict) or not isinstance(data.get("leases"), list):
        raise SwarmError("the leases response carried no `leases` list")
    return data


#: The console's Overview reads `/v1/workflows` for its Workflows check, and
#: that response carries the reconciler's `stalled_workflows` (#616). One row
#: is asked for: the stalled list does not depend on the page, and every
#: workflow on the page costs a read per step to derive.
WORKFLOWS_PATH = "/v1/workflows?limit=1"


def fetch_workflows(client: SwarmClient) -> dict[str, Any]:
    """The workflow page whose `stalled_workflows` `sc trouble` lists."""
    data = client.request("GET", WORKFLOWS_PATH)
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
        raise SwarmError("the workflows response carried no `workflows` list")
    return data


class AccountsRouteAbsent(SwarmError):
    """swarm-api has no `/v1/accounts` route on this deployment.

    Not a failure of the pool and not an empty pool: a deployment older than
    the accounts proxy, where the pool simply cannot be read from here. The
    caller grades it as information rather than as a cluster that is down,
    which is the difference between `sc trouble` being usable on current
    production and being a red screen on it.
    """


def fetch_accounts(client: SwarmClient) -> list[dict[str, Any]]:
    """The account pool, read through swarm-api and through nothing else.

    swarm-api PROXIES `/v1/accounts` to quota-broker, which is the platform's
    single writer for subscription credentials -- swarm-api must never touch
    Secret Manager for accounts, and neither may this.

    THERE IS DELIBERATELY NO SECOND HOST TO FALL BACK TO. An earlier version
    read `SWARM_BROKER_URL` and retried there with the same client, which sent
    the ID token minted for swarm-api -- verbatim, as a bearer -- to whatever
    host that variable named, where it could be replayed against swarm-api.
    That is the cross-audience mistake commit 411d086 fixed one layer down.
    It could not have worked anyway: on the IAP tier the token is minted for
    the IAP client id, which the broker refuses, and on the proxy tier no
    Authorization header is sent at all. An operator on the VPC who wants to
    read quota-broker directly points `SWARM_API_URL` at it, so the token is
    minted for the host that will actually receive it.

    A deployment without the route is NOT "zero accounts". It is an unknown
    pool, and this raises so the caller records a failure rather than an empty
    list.
    """
    try:
        data = client.request("GET", "/v1/accounts")
    except SwarmError as via_api:
        # `edge` distinguishes the API's own 404 ("no such route") from
        # Google's HTML 404 ("the ingress refused you before the API saw
        # it"). Only the first one means this deployment lacks the proxy.
        if via_api.status == 404 and not via_api.edge:
            raise AccountsRouteAbsent(
                f"{via_api} -- this deployment's swarm-api has no /v1/accounts "
                "route, so the account pool cannot be read from here. It is "
                "UNKNOWN, not empty"
            ) from None
        raise
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if accounts is None:
        raise SwarmError("the accounts response carried no `accounts` field")
    # A Listing, so the section can say WHOSE pool this is: the route answers
    # for the caller's resolved tenant and echoes it (#88, SC-F5).
    return render.Listing(
        [a for a in accounts if isinstance(a, dict)], tenant_id=data.get("tenant_id")
    )


#: Which fetches each subcommand actually needs. Asking for everything on every
#: command would make `sc accounts` wait on a task listing it will not print --
#: and, worse, report a task-listing failure as though it were an account one.
#:
#: Each view fetches exactly what it PRINTS, which is also what its exit code
#: is about. The narrow views used to fetch `/v1/tenants/me` and then render
#: none of it: a round trip whose failure was invisible on screen and could
#: not honestly be allowed to change the exit code either.
NEEDS = {
    "overview": ("tenant", "stats", "capacity", "accounts", "tasks", "leases"),
    "accounts": ("accounts",),
    "agents": ("tasks",),
    "capacity": ("capacity",),
    "trouble": ("tenant", "stats", "capacity", "accounts", "tasks", "leases", "workflows"),
}


def collect(client: SwarmClient, wanted: tuple[str, ...]) -> Snapshot:
    """Fetch what a command needs, concurrently, recording each failure.

    Concurrent because these are independent round trips through IAP and a
    status command that takes four seconds gets replaced by a guess. The ID
    token is minted once, up front, rather than raced for by one thread per fetch, each
    of which would shell out to gcloud.
    """
    snap = Snapshot(now=datetime.now(timezone.utc))
    snap.api_url = client.base_url
    snap.tier = getattr(client.tier, "value", str(client.tier))

    if "tenant" in wanted:
        snap.tenant, tenant_error = _attempt(lambda: fetch_tenant(client))
        snap.tenant_error = str(tenant_error) if tenant_error is not None else None

    jobs: dict[str, Callable[[], Any]] = {}
    if "stats" in wanted:
        jobs["stats"] = lambda: fetch_stats(client)
    if "capacity" in wanted:
        jobs["capacity"] = lambda: fetch_capacity(client)
    if "accounts" in wanted:
        jobs["accounts"] = lambda: fetch_accounts(client)
    if "tasks" in wanted:
        jobs["tasks"] = lambda: fetch_tasks(client)
    if "leases" in wanted:
        jobs["leases"] = lambda: fetch_leases(client)
    if "workflows" in wanted:
        jobs["workflows"] = lambda: fetch_workflows(client)

    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(_attempt, fn) for name, fn in jobs.items()}
            for name, future in futures.items():
                value, error = future.result()
                setattr(snap, name, value)
                setattr(snap, f"{name}_error", str(error) if error is not None else None)
                if name == "accounts" and isinstance(error, AccountsRouteAbsent):
                    # The reason is still printed in full; only its SEVERITY
                    # changes. "This deployment has no such route" and "the
                    # pool is broken" read the same on screen and must not
                    # grade the same.
                    snap.accounts_absent = True
                if name == "leases" and isinstance(error, LeasesRefused):
                    snap.leases_refused = True
    return snap


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _emit(lines: list[str], stream: Any) -> None:
    stream.write(render.join(lines))


def _cluster_exit(snap: Snapshot, findings: Sequence[Finding]) -> int:
    """The exit code for a whole-cluster view: `sc` and `sc trouble`.

    UNREADABLE OUTRANKS DOWN. Both are non-zero, and both used to be 3 here,
    which lost the distinction the codes exist for: a cluster that cannot be
    reached at all is an alert about the path to it, not about it.

    AN ABSENT `/v1/accounts` ROUTE IS NEITHER. It is what every deployment
    older than the accounts proxy looks like, so counting it as unreadable
    would make `sc` report a healthy, unchanged production cluster as broken
    -- the one backwards-compatibility bar this surface has. `sc accounts`
    still fails on it, because that view is ABOUT the pool and has nothing
    else to show; the overview has five other areas and prints the reason.
    """
    unreadable = [
        error
        for error in (
            snap.tenant_error,
            snap.stats_error,
            snap.capacity_error,
            snap.tasks_error,
            None if snap.accounts_absent else snap.accounts_error,
            None if snap.leases_refused else snap.leases_error,
            snap.workflows_error,
        )
        if error
    ]
    if unreadable:
        return EXIT_FAIL
    return EXIT_TROUBLE if any(f.severity == "down" for f in findings) else EXIT_OK


def cmd_overview(client: SwarmClient, args, out) -> int:
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    snap = collect(client, NEEDS["overview"])
    # The same findings the screen shows, so the code and the screen cannot
    # disagree about what is wrong.
    code = _cluster_exit(snap, render.find_trouble(snap, style))
    if args.json:
        _dump(snap, out)
        return code
    _emit(render.render_overview(snap, style), out)
    return code


def cmd_accounts(client: SwarmClient, args, out) -> int:
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    snap = collect(client, NEEDS["accounts"])
    # This view is ABOUT one area, so that area being unreadable is the whole
    # command failing -- whatever the reason, including a route this
    # deployment does not have: there is nothing else on the screen.
    code = EXIT_FAIL if snap.accounts is None else EXIT_OK
    if args.json:
        _dump(snap, out)
        return code
    lines = [
        render.section("accounts", render.accounts_subtitle(snap.accounts, style), style)
    ] + render.render_accounts(snap.accounts, style, snap.now, error=snap.accounts_error)
    _emit(lines, out)
    return code


def cmd_agents(client: SwarmClient, args, out) -> int:
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    snap = collect(client, NEEDS["agents"])
    # This view is ABOUT one area, so that area being unreadable is the whole
    # command failing -- whatever the reason, including a route this
    # deployment does not have: there is nothing else on the screen.
    code = EXIT_FAIL if snap.tasks is None else EXIT_OK
    if args.json:
        _dump(snap, out)
        return code
    lines = [
        render.section("agents", render.agents_subtitle(snap.tasks, style), style)
    ] + render.render_agents(snap.tasks, style, snap.now, error=snap.tasks_error)
    _emit(lines, out)
    return code


def cmd_capacity(client: SwarmClient, args, out) -> int:
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    snap = collect(client, NEEDS["capacity"])
    # This view is ABOUT one area, so that area being unreadable is the whole
    # command failing -- whatever the reason, including a route this
    # deployment does not have: there is nothing else on the screen.
    code = EXIT_FAIL if snap.capacity is None else EXIT_OK
    if args.json:
        _dump(snap, out)
        return code
    lines = [
        render.section("capacity", render.capacity_subtitle(snap.capacity, style), style)
    ] + render.render_capacity(snap.capacity, style, error=snap.capacity_error)
    _emit(lines, out)
    return code


def cmd_task(client: SwarmClient, args, out) -> int:
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    now = datetime.now(timezone.utc)
    task, failure = _attempt(lambda: fetch_task(client, args.task_id))
    error = str(failure) if failure is not None else None
    if args.json:
        out.write(json.dumps(task if task is not None else {"error": error}, indent=2, default=str) + "\n")
        return EXIT_OK if task is not None else EXIT_FAIL
    uri = patch_uri(task) if task else None
    # WHAT IT PRODUCED (#143), from the artifacts route: `complete` there is
    # what tells "not uploaded yet" from "none". A failed listing is said on
    # its line and does not fail the command -- the task itself was read.
    made = None
    if task is not None:
        listing, listing_failure = _attempt(lambda: client.artifacts(args.task_id))
        made = outputs_of(
            task,
            listing,
            listing_error=str(listing_failure) if listing_failure is not None else None,
        )
    _emit(
        render.render_task(
            task,
            style,
            now,
            error=error,
            patch=uri,
            no_patch_because=None if uri or not task else explain_absence(task),
            produced=made,
            fetch_with=terminal_command(f"swarm artifact {args.task_id} <name>"),
        ),
        out,
    )
    return EXIT_OK if task is not None else EXIT_FAIL


def cmd_trouble(client: SwarmClient, args, out) -> int:
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    snap = collect(client, NEEDS["trouble"])
    findings = render.find_trouble(snap, style)
    code = _cluster_exit(snap, findings)
    if args.json:
        out.write(
            json.dumps(
                [{"severity": f.severity, "where": f.where, "what": f.what} for f in findings],
                indent=2,
            )
            + "\n"
        )
    else:
        lines = [
            render.section(
                "trouble",
                "" if not findings else f"{len(findings)} finding(s)",
                style,
            )
        ] + render.render_trouble(findings, style)
        _emit(lines, out)
    return code


# --------------------------------------------------------------------------
# Running workflows: `sc workflows`, `swarm_workflows` and the session hook
# --------------------------------------------------------------------------
#
# Owner decision, 2026-10-02: a workflow running in SwarmCloud shows in Claude
# Code as live [SwarmCloud] rows WITHOUT anyone attaching it by id. This is the
# read every part of that stands on -- the `sc workflows` view, the MCP tool
# `swarm_workflows` that `/sc attach --all` lists through, and the plugin's
# SessionStart hook (`--session-start`). It writes nothing.

#: The workflow states that are not over: every task state outside
#: TERMINAL_STATES, and UNKNOWN -- what the API derives when it could not read
#: every step, which is a workflow nobody has shown to be finished. Asked of
#: the route by name, so a tenant's finished history is never paged through.
ACTIVE_WORKFLOW_STATES: tuple[str, ...] = tuple(
    s.value for s in TaskState if s not in TERMINAL_STATES
) + ("UNKNOWN",)

_TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_STATES)

#: One page of `GET /v1/workflows`, as the running-workflow walk asks for it;
#: `check` imports it rather than restating it (#1749 found the walk's
#: constants stated twice, in this module and in check.py).
#:
#: NO PAGE CAP (owner decision 2026-10-20, #1749). The walk used to stop after
#: 4 pages of 50 and say so; in tenant eng that day every check and attach-all
#: hit it, and a session's work older than the tenant's newest 200 running
#: workflows was never flagged. The fix the owner chose is the server-side
#: filter -- the check and attach-all ask for their own session's work
#: (`claude_session=`), which is a page or two -- and the walk reads to the
#: last page. It stops short only when a read fails, or the API hands back a
#: token it was already given, and says which.
WORKFLOW_PAGE_SIZE = 50

#: How many workflow detail reads (`GET /v1/workflows/{id}`) run at once.
#: EVERY running workflow is read (BOARD-ORDER, owner 2026-10-16): its
#: progress -- steps done of the total, and how long its current step has run
#: -- is what orders the list, and a workflow left unread could not be placed.
#: Eight at a time, as `launch._read_workflows` reads, so 60 running workflows
#: are 8 rounds of reads, not 60 requests in one burst.
WORKFLOW_DETAIL_CONCURRENCY = 8

#: How many workflows the session-start context names one by one.
SESSION_START_NAMED = 10


def _label_of(envelope: dict[str, Any]) -> str | None:
    """The spec's `label`, as the step tasks carry it (`metadata.unit`).

    The frozen `Workflow` has no metadata field; `workflows.submit` puts the
    label on every step task, so it is read back from the first that has one.
    """
    for task in envelope.get("tasks") or []:
        unit = (task.get("metadata") or {}).get("unit") if isinstance(task, dict) else None
        if isinstance(unit, str) and unit.strip():
            return unit.strip()
    return None


def pull_request_wait(workflow: dict[str, Any]) -> str | None:
    """"waiting for PR #41 to merge", or why it will not start; None otherwise.

    The API's sentence (`workflow.after_pull_requests.text`, SPEED-D3), read
    only while the workflow waits for its `after` pull requests or after one
    closed unmerged. A released wait says nothing: its steps say the rest.
    """
    wait = workflow.get("after_pull_requests")
    if not isinstance(wait, dict) or wait.get("state") not in ("waiting", "closed"):
        return None
    text = wait.get("text")
    return text if isinstance(text, str) and text else None


def group_by_workspace(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`entries` closest to finish first (`board.order`), grouped by their
    `workspace`: one block per workspace, the blocks in the order of each
    block's closest-to-finish entry, and `board.order` inside each block
    (BOARD-ORDER, owner 2026-10-16: "keep the workspace groups", replacing
    the "newest first" half of SC-ROWS). The order is total, so two reads of
    the same state list the same order. plugin/workflows/run.js's
    `groupByWorkspace` is the same."""
    order: list[Any] = []
    groups: dict[Any, list[dict[str, Any]]] = {}
    for entry in board.order(entries):
        key = entry.get("workspace")
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(entry)
    return [entry for key in order for entry in groups[key]]


def _running_entry(workflow: dict[str, Any], now: datetime, tenant: Any = None) -> dict[str, Any]:
    from . import workflows

    created = render.parse_time(workflow.get("created_at"))
    entry: dict[str, Any] = {
        "workflow_id": workflow.get("workflow_id"),
        # The workspace it runs in, from its own document (SC-ROWS).
        "workspace": workflows.workspace_of(workflow, fallback=tenant),
        "label": None,
        "title": None,
        "state": workflow.get("state"),
        "created_at": workflow.get("created_at"),
        "age_seconds": max(0, int((now - created).total_seconds())) if created else None,
        "steps_total": len(workflow.get("steps") or []),
        "current_steps": None,
        # Steps done of the total and the current steps' ages (`board.progress`),
        # from the detail read; None until it is read.
        "progress": None,
        "waiting_for": pull_request_wait(workflow),
        # The Claude Code session that started it (SC-SESSION-SCOPE), as
        # swarm-api serves it off the step tasks; the detail read fills it
        # in from the tasks themselves for a deployment that serves none.
        "claude_session": session.owner_of(workflow),
    }
    return with_console(entry, workflow)


def _read_detail(client: SwarmClient, entry: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """The label, the unfinished steps and the progress, from one workflow read."""
    from . import workflows

    try:
        envelope = workflows.fetch(client, str(entry["workflow_id"]))
    except SwarmError as exc:
        entry["steps_unread_because"] = str(exc)
        return entry
    workflow = envelope["workflow"]
    if workflow.get("state"):
        entry["state"] = workflow["state"]
    entry["waiting_for"] = pull_request_wait(workflow)
    entry["label"] = _label_of(envelope)
    entry["claude_session"] = (entry.get("claude_session") or session.owner_of(workflow)
                               or session.owner_of_tasks(envelope.get("tasks") or []))
    # The repository its steps clone (SC-WATCH): the frozen `Workflow` has
    # none of its own, so `/sc check` scopes a project's workflows by this.
    entry["repository_url"] = next(
        (t["repository_url"] for t in envelope.get("tasks") or []
         if isinstance(t, dict) and isinstance(t.get("repository_url"), str) and t["repository_url"]),
        None,
    )
    # The spec's short name, when it gave one: what /sc attach --all titles
    # each workflow's run with (`workflows.title_name`).
    entry["title"] = workflows.stored_names(envelope)["title"]
    links = {
        step.get("step_id"): step
        for step in workflow.get("steps") or []
        if isinstance(step, dict)
    }
    current = []
    for row in workflows.step_rows(envelope):
        if row.get("state") in _TERMINAL_VALUES:
            continue
        step = {"step_id": row.get("step_id"), "task_id": row.get("task_id"), "state": row.get("state")}
        if row.get("park_reason"):
            step["park_reason"] = row["park_reason"]
        current.append(with_console(step, links.get(row.get("step_id"))))
    entry["current_steps"] = current
    entry["progress"] = board.progress(envelope, now or datetime.now(timezone.utc))
    return entry


def walk_running_workflows(
    client: SwarmClient,
    *,
    claude_session: str | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """Every page of the tenant's unfinished workflows, newest first:
    `{workflows, tenant_id, pages, stalled_workflows?, filtered_by_api?,
    incomplete_because?}`.

    The route is asked for `active=true` and `ACTIVE_WORKFLOW_STATES`, and,
    when given, for one Claude Code session's work and one repository's
    (CHECK-FILTER, #1749) -- and each row is checked AGAIN for being
    unfinished, because a deployment older than those filters ignores them.
    `filtered_by_api` is present only when the route echoed the session or
    repository filter in `filter`, i.e. applied it; it carries the route's
    `left_out` counts summed over the pages, which a caller reports beside
    the rows it left out itself.

    EVERY PAGE is read: a page that comes back short, or full, says nothing
    about what is behind it. The walk stops early only on a failed read --
    the first page's failure raises (`SwarmError`: "could not ask" is never an
    empty list), a later one is kept as `incomplete_because` with what was
    read -- or on a token the route already handed back, which would loop."""
    out: dict[str, Any] = {"workflows": [], "tenant_id": None, "pages": 0}
    token: str | None = None
    seen: set[str] = set()
    echoed: dict[str, Any] | None = None
    while True:
        try:
            page = client.workflows(
                states=ACTIVE_WORKFLOW_STATES,
                active=True,
                limit=WORKFLOW_PAGE_SIZE,
                page_token=token,
                **({"claude_session": claude_session} if claude_session else {}),
                **({"repository": repository} if repository else {}),
            )
        except SwarmError as exc:
            if out["pages"] == 0:
                raise
            out["incomplete_because"] = (
                f"reading page {out['pages'] + 1} of the running workflows failed ({exc}); "
                "older running workflows, if any, are not listed"
            )
            break
        out["pages"] += 1
        out["tenant_id"] = out["tenant_id"] or page.get("tenant_id")
        if "stalled_workflows" in page and "stalled_workflows" not in out:
            out["stalled_workflows"] = page["stalled_workflows"]
        out["workflows"] += [w for w in page["workflows"]
                             if isinstance(w, dict) and w.get("state") not in _TERMINAL_VALUES]
        filtered = page.get("filter")
        if isinstance(filtered, dict) and ("claude_session" in filtered or "repository" in filtered):
            if echoed is None:
                echoed = {"claude_session": filtered.get("claude_session"),
                          "repository": filtered.get("repository"), "left_out": {}}
            for reason, count in (filtered.get("left_out") or {}).items():
                if isinstance(count, int):
                    echoed["left_out"][reason] = echoed["left_out"].get(reason, 0) + count
        following = page.get("next_page_token") or None
        if following is None:
            break
        if following in seen or following == token:
            out["incomplete_because"] = (
                f"GET /v1/workflows handed back the same page token after {out['pages']} pages; "
                "older running workflows, if any, are not listed"
            )
            break
        seen.add(following)
        token = following
    if echoed is not None:
        out["filtered_by_api"] = echoed
    return out


def running_workflows(
    client: SwarmClient,
    *,
    now: datetime | None = None,
    detail_reads: int | None = None,
    claude_session: str | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """The caller's tenant's workflows that are not over, CLOSEST TO FINISH
    FIRST in workspace blocks (`group_by_workspace`, `board.order`), read to
    the last page (`walk_running_workflows`) -- the whole tenant's, or with
    `claude_session` / `repository` only those the API keeps for them.

    EVERY one is read in detail (`detail_reads` None), at most
    `WORKFLOW_DETAIL_CONCURRENCY` at a time: its progress is what orders it.
    `detail_reads` caps that for a caller that needs no order (a test); the
    rest say they were not read and sort below every read one. Each read
    entry carries `progress` (`board.progress`) with the historical medians
    beside it (`board.annotate`), and the listing says what the medians
    cost under `medians` -- one shared read per 15 minutes, never one per
    workflow; a median that could not be read leaves them out and says so.

    The tenant is the API's: `GET /v1/workflows` answers for the caller's own
    tenant (`tenant_scope`), and nothing here names one. A workflow whose
    detail read says it has since finished is dropped: attaching a finished
    one would start rows with nothing to watch. Raises `SwarmError` when the
    list itself cannot be read; `incomplete_because` is said only when a
    later page's read failed.
    """
    now = now or datetime.now(timezone.utc)
    walk = walk_running_workflows(client, claude_session=claude_session, repository=repository)
    tenant = walk["tenant_id"]
    found = [_running_entry(workflow, now, tenant) for workflow in walk["workflows"]]

    detailed = found if detail_reads is None else found[: max(0, detail_reads)]
    if detailed:
        with ThreadPoolExecutor(max_workers=min(WORKFLOW_DETAIL_CONCURRENCY, len(detailed))) as pool:
            list(pool.map(lambda entry: _read_detail(client, entry, now), detailed))
    for entry in found[len(detailed):]:
        entry["steps_unread_because"] = (
            f"only {detail_reads} running workflows were asked to be read step by step"
        )
    listed = group_by_workspace([entry for entry in found if entry.get("state") not in _TERMINAL_VALUES])
    out: dict[str, Any] = {
        "tenant_id": tenant,
        "count": len(listed),
        "complete": "incomplete_because" not in walk,
        "workflows": listed,
    }
    if "filtered_by_api" in walk:
        out["filtered_by_api"] = walk["filtered_by_api"]
    if any(isinstance(entry.get("progress"), dict) for entry in listed):
        found_medians = board.medians(client, now=now)
        for entry in listed:
            board.annotate(entry, found_medians)
        out["medians"] = {
            key: found_medians[key]
            for key in ("window_days", "requests", "tasks_read", "read_at", "reused",
                        "unavailable_because")
            if key in found_medians
        }
        out["medians"]["steps_with_history"] = len(found_medians.get("by_step") or {})
    if "incomplete_because" in walk:
        out["incomplete_because"] = walk["incomplete_because"]
    return out


# Running SINGLE tasks (#830, owner 2026-10-07: "we need to be able to auto
# attach on single tasks too"). A task sent with `swarm_dispatch` belongs to no
# workflow, so `GET /v1/workflows` never lists it, and five lanes dispatched
# that day ran without a row. These are the caller's own unfinished tasks
# outside any workflow, read through the existing `GET /v1/tasks` filters only:
# `state` (one value per request) and `submitted_by=me`.

#: One page of ONE state's walk. `state` and `submitted_by` together are
#: filtered after the route takes its cursor (`Store.list_tasks`), so a page
#: holds only the caller's share of the tenant's tasks in that state and may
#: come back short with a token: a tenant whose other members queue hundreds
#: of tasks needs more than one page to reach the caller's. The walk reads to
#: the last page, as the workflow walk does (#1749: a cap of 3 pages left a
#: caller's older tasks unlisted behind other members' newer ones), and stops
#: short only on a failed read or a repeated token.
SINGLE_TASK_PAGE_SIZE = 50

#: How many single tasks the session-start context names one by one.
SESSION_START_TASKS_NAMED = 10


def _task_label(task: dict[str, Any]) -> str | None:
    """The label the dispatch gave (`swarm_dispatch`'s `label`, stored as
    `metadata.unit`), or None."""
    unit = (task.get("metadata") or {}).get("unit") if isinstance(task.get("metadata"), dict) else None
    if isinstance(unit, str) and " ".join(unit.split()):
        return " ".join(unit.split())
    return None


def _single_task_entry(task: dict[str, Any], now: datetime, tenant: Any = None) -> dict[str, Any]:
    from . import workflows
    from .client import task_id_of

    created = render.parse_time(task.get("created_at"))
    entry: dict[str, Any] = {
        "task_id": task_id_of(task),
        # The workspace it runs in, from its own document (SC-ROWS).
        "workspace": workflows.workspace_of(task, fallback=tenant),
        "label": _task_label(task),
        "state": task.get("state"),
        "runner_profile": task.get("runner_profile"),
        "created_at": task.get("created_at"),
        "age_seconds": max(0, int((now - created).total_seconds())) if created else None,
        # How long it has been in its run (its last attempt's start, else its
        # creation): what orders it among single tasks (`board.order`).
        "state_age_seconds": (
            max(0, int((now - since).total_seconds()))
            if (since := render.parse_time(task.get("started_at")) or created) else None
        ),
        # What it clones (SC-WATCH): `/sc check` attaches only this project's.
        "repository_url": task.get("repository_url"),
        # The Claude Code session that dispatched it (SC-SESSION-SCOPE).
        "claude_session": session.owner_of(task),
    }
    if task.get("park_reason"):
        entry["park_reason"] = task["park_reason"]
    return with_console(entry, task)


def _callers_tasks_in(
    client: SwarmClient, state: str, *, claude_session: str | None = None
) -> tuple[list[dict[str, Any]], str | None, str | None, dict[str, Any] | None]:
    """The caller's own tasks in one state, every page: `(tasks, tenant_id,
    gap, filtered)`. `gap` is None when the last page was read, and a
    sentence when a later page's read failed or the route repeated a token;
    the first page's failure raises.

    With `claude_session`, the route is asked for that session's tasks only
    (CHECK-FILTER-TASKS, #1749), so other sessions' rows cost no pages.
    `filtered` is present only when the route echoed the session in
    `filter`, i.e. applied it: `{claude_session, left_out, left_out_single}`,
    the counts summed over the pages, which a caller reports beside the rows
    it left out itself. A deployment older than the filter ignores the
    parameter and serves every row, so `filtered` is None and the caller's
    own ownership test is what leaves the rest out."""
    found: list[dict[str, Any]] = []
    tenant: str | None = None
    token: str | None = None
    seen: set[str] = set()
    echoed: dict[str, Any] | None = None
    pages = 0
    while True:
        params = [("state", state), ("submitted_by", "me"), ("limit", str(SINGLE_TASK_PAGE_SIZE))]
        if claude_session:
            params.append(("claude_session", claude_session))
        if token:
            params.append(("page_token", token))
        try:
            data = _tasks_page(client, params)
        except SwarmError as exc:
            if pages == 0:
                raise
            return found, tenant, f"reading page {pages + 1} of the {state} tasks failed ({exc})", echoed
        pages += 1
        found += [t for t in data["tasks"] if isinstance(t, dict)]
        tenant = tenant or data.get("tenant_id")
        filtered = data.get("filter")
        if claude_session and isinstance(filtered, dict) and "claude_session" in filtered:
            if echoed is None:
                echoed = {"claude_session": filtered.get("claude_session"),
                          "left_out": {}, "left_out_single": {}}
            for key in ("left_out", "left_out_single"):
                counts = filtered.get(key)
                for reason, count in (counts.items() if isinstance(counts, dict) else ()):
                    if isinstance(count, int):
                        echoed[key][reason] = echoed[key].get(reason, 0) + count
        following = data.get("next_page_token") or None
        if following is None:
            return found, tenant, None, echoed
        if following in seen or following == token:
            return (found, tenant,
                    f"GET /v1/tasks handed back the same {state} page token after {pages} pages", echoed)
        seen.add(following)
        token = following


def running_tasks(
    client: SwarmClient,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The caller's running SINGLE tasks, the longest-running first
    (`board.order`, by state age): not finished, in no workflow, and
    submitted by the caller.

    One `GET /v1/tasks?state=<s>&submitted_by=me` walk per unfinished state
    (`_live_states`, read from the frozen contract), in parallel. The route
    answers for the caller's own tenant and resolves `me` to the caller's
    verified email; nothing here names either. Every row is checked AGAIN --
    unfinished, no `workflow_id` -- because a task can move state between two
    of the reads, and an older route that ignored a filter would otherwise
    hand a workflow's step or a finished task a row. A task a PARENT TASK
    submitted (`parent_task_id`) is left out: it was not dispatched from a
    session, and its parent's row is where it shows. Raises `SwarmError` when
    a state's first read fails: "could not ask" is never an empty list. A
    later page's failure keeps what was read and says so in
    `incomplete_because`.
    """
    now = now or datetime.now(timezone.utc)
    states = _live_states()
    with ThreadPoolExecutor(max_workers=len(states)) as pool:
        walks = list(pool.map(lambda state: _callers_tasks_in(client, state), states))
    tenant: str | None = None
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    gaps: list[str] = []
    for state, (tasks, page_tenant, gap, _) in zip(states, walks):
        tenant = tenant or page_tenant
        if gap:
            gaps.append(gap)
        for task in tasks:
            entry = _single_task_entry(task, now, page_tenant)
            task_id = entry["task_id"]
            if (
                not task_id
                or task_id in seen
                or task.get("workflow_id")
                or task.get("parent_task_id")
                or task.get("state") in _TERMINAL_VALUES
            ):
                continue
            seen.add(task_id)
            found.append(entry)
    found.sort(key=lambda entry: str(entry.get("created_at") or ""), reverse=True)
    found = group_by_workspace(found)
    out: dict[str, Any] = {
        "tenant_id": tenant,
        "count": len(found),
        "complete": not gaps,
        "tasks": found,
    }
    if gaps:
        out["incomplete_because"] = (
            f"{'; '.join(gaps)}; older running tasks of yours, if any, are not listed"
        )
    return out


def _age_text(seconds: Any) -> str:
    if not isinstance(seconds, int):
        return "age unknown"
    if seconds < 60:
        return "<1m old"
    if seconds < 3600:
        return f"{seconds // 60}m old"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m old"
    return f"{seconds // 86400}d old"


def _steps_text(entry: dict[str, Any]) -> str:
    steps = entry.get("current_steps")
    if steps is None:
        return "steps not read"
    if not steps:
        return "no unfinished step"
    return ", ".join(f"{s.get('step_id')} {s.get('state') or 'state not read'}" for s in steps)


def _minutes(seconds: Any) -> str:
    return f"{max(0, round(seconds / 60))}m"


def _board_text(entry: dict[str, Any]) -> str:
    """`  3/5  now: review 12m +1  p50 20m  eta ~30m`: where the workflow
    stands (`board.progress`), its longest-running current step and that
    step's age, and the historical medians beside them where they are known.
    '' for a workflow whose progress was not read."""
    p = entry.get("progress")
    if not isinstance(p, dict):
        return ""
    text = f"  {p.get('done')}/{p.get('total')}"
    current = sorted(p.get("current") or [], key=lambda s: -(s.get("age_seconds") or -1))
    if current:
        first = current[0]
        age = f" {_minutes(first['age_seconds'])}" if isinstance(first.get("age_seconds"), int) else ""
        more = f" +{len(current) - 1}" if len(current) > 1 else ""
        text += f"  now: {first.get('step_id')}{age}{more}"
        if isinstance(first.get("median_seconds"), int):
            text += f"  p50 {_minutes(first['median_seconds'])}"
    if isinstance(p.get("eta_seconds"), int):
        text += f"  eta ~{_minutes(p['eta_seconds'])}"
    return text


def session_start_context(
    listing: dict[str, Any] | None,
    tasks: dict[str, Any] | None = None,
    *,
    watch_minutes: int | None = None,
    watch_off: bool = False,
    project: str | None = None,
    layout: str = "rows",
) -> str | None:
    """What the SessionStart hook tells the session, or None when nothing runs.

    `listing` is `running_workflows`, `tasks` is `running_tasks` (#830); either
    may be None when it could not be read, and the other is still named. A
    hook cannot start a Workflow; the context makes the session's first action
    the attach, and says how to turn this off.

    `watch_minutes` (SC-WATCH): the project's remembered `/sc watch`
    interval, handed over by the hook. Claude Code's scheduled prompts end
    with a session, so the context also asks for `/sc watch <N>m` -- and says
    so even when nothing runs.

    `watch_off` (SC-AUTO-WATCH): the hook found an explicit `/sc watch off`
    for the project. Otherwise, when something runs and no interval was ever
    saved, the context asks the session to arm the watch at the default --
    CronList first, so a session that already has one makes no second.

    `project` (SC-BROWSER-FOLLOWUP, BGD 2026-10-25): this checkout's origin
    repository, `host/owner/repo`, when the listings were widened to the
    project's running work (`own_or_project`): entries another session
    started say so, and the attach asked for is `/sc attach --all --project`
    -- or, in the pane `layout`, none for them: the pane lists them once read.
    """
    from . import watch

    running = (listing or {}).get("workflows") or []
    singles = (tasks or {}).get("tasks") or []
    if not running and not singles:
        return watch.rearm_sentence(watch_minutes) if watch_minutes else None
    tenant_id = (listing or {}).get("tenant_id") or (tasks or {}).get("tenant_id")
    tenant = f" for tenant {tenant_id}" if tenant_id else ""
    parts = []
    if running:
        count = len(running)
        noun = "workflow is" if count == 1 else "workflows are"
        named = []
        for entry in running[:SESSION_START_NAMED]:
            label = f' "{entry["label"]}"' if entry.get("label") else ""
            named.append(
                f"{entry.get('workflow_id')}{label} ({entry.get('state')}; {_steps_text(entry)}; "
                f"{_age_text(entry.get('age_seconds'))}{_workspace_clause(entry)}{_started_clause(entry)})"
            )
        if count > SESSION_START_NAMED:
            named.append(f"and {count - SESSION_START_NAMED} more")
        parts.append(
            f"{count} SwarmCloud {noun} running{tenant} and not shown in this session: "
            + "; ".join(named)
            + "."
        )
    if singles:
        count = len(singles)
        noun = "single task you dispatched is" if count == 1 else "single tasks you dispatched are"
        named = []
        for entry in singles[:SESSION_START_TASKS_NAMED]:
            label = f' "{entry["label"]}"' if entry.get("label") else ""
            named.append(
                f"{entry.get('task_id')}{label} ({entry.get('state')}; "
                f"{_age_text(entry.get('age_seconds'))}{_workspace_clause(entry)}{_started_clause(entry)})"
            )
        if count > SESSION_START_TASKS_NAMED:
            named.append(f"and {count - SESSION_START_TASKS_NAMED} more")
        parts.append(
            f"{count} SwarmCloud {noun} running{tenant}, in no workflow, and not shown "
            "in this session: " + "; ".join(named) + "."
        )
    from . import config

    cap = config.max_live_rows()
    rows_text = (
        "it submits nothing, starts one slim row per unfinished "
        f"workflow step and one per single task, and follows at most {cap} workflows "
        f"and {cap} single tasks (the plugin's `max_live_rows`)."
    )
    hook_text = (
        " (This notice comes from the sc plugin's SessionStart hook; turn off its "
        "`auto_attach` option to stop it.)"
    )
    own = [e for e in [*running, *singles] if e.get("this_session", True)]
    if not project:
        text = (
            " ".join(parts)
            + " Each was started by this Claude Code session (SC-SESSION-SCOPE): other sessions' work "
            "in the tenant, and untagged work, is not named."
            + " Before anything else, run `/sc attach --all` to show them as live "
            "[SwarmCloud] rows: " + rows_text + hook_text
        )
        attaching = True
    else:
        text = (
            " ".join(parts)
            + " Named: the work this Claude Code session started, and the work still running for "
            f"this project (repository {project}) whichever session started it -- at session start "
            "only (BGD, 2026-10-25); another repository's work from another session, and untagged "
            "work outside this project, is not named. `/sc check` and the watch stay scoped to "
            "this session."
        )
        if layout == "pane":
            text += (
                " This project's layout is the SwarmCloud pane, which the plugin opens beside the chat "
                "at session start, not read yet (`/sc-pane` opens it; it needs Claude Code's function "
                "hooks): its Refresh lists all of these with one direct call and no model tokens -- "
                "that is where this project's work is tracked, with no model row."
            )
            attaching = bool(own)
            if attaching:
                text += (
                    " Before anything else, run `/sc attach --all`: it gives each workflow this session "
                    "started its ONE quiet /workflows row that notices its finish, and submits nothing."
                )
        else:
            text += (
                " Before anything else, run `/sc attach --all --project` to show them as live "
                "[SwarmCloud] rows tracked in this session: " + rows_text
            )
            attaching = True
        text += hook_text
    if watch_minutes:
        text += " Then: " + watch.rearm_sentence(watch_minutes)
    elif not watch_off and attaching:
        text += " " + watch.arm_sentence()
    return text


def project_key() -> str | None:
    """This checkout's origin repository, `host/owner/repo`, or None when it
    has no origin naming one: then nothing widens to a project (SC-BROWSER-
    FOLLOWUP) -- "no origin" is never read as "every repository"."""
    from .check import resolve_scope

    try:
        return resolve_scope("origin")["key"]
    except Exception:  # noqa: BLE001 - a scope that cannot be read widens nothing
        return None


def own_or_project(listing: dict[str, Any] | None, key: str, sid: str | None,
                   repository: str | None) -> dict[str, Any] | None:
    """`listing` (`running_workflows` or `running_tasks`) with the entries
    under `key` this session started and -- the session start's widening
    (SC-BROWSER-FOLLOWUP, BGD 2026-10-25) -- those that clone `repository`,
    whichever session started them; each kept entry marked `this_session`.
    `repository` None keeps this session's alone (`own_only`)."""
    from .check import repo_key

    if listing is None:
        return None
    kept = []
    for entry in listing.get(key) or []:
        if not isinstance(entry, dict):
            continue
        own = session.ownership(session.owner_of(entry), sid) == "own"
        if own or (repository is not None and repo_key(entry.get("repository_url")) == repository):
            kept.append({**entry, "this_session": own})
    return {**listing, key: kept, "count": len(kept)}


def _started_clause(entry: dict[str, Any]) -> str:
    """`; another session's` for a project entry this session did not start."""
    return "; started by another Claude Code session" if entry.get("this_session") is False else ""


def own_only(listing: dict[str, Any] | None, key: str, sid: str | None) -> dict[str, Any] | None:
    """`listing` (`running_workflows` or `running_tasks`) with only the
    entries under `key` tagged with session `sid`: none when `sid` is None."""
    if listing is None:
        return None
    kept = [e for e in listing.get(key) or []
            if isinstance(e, dict) and session.ownership(session.owner_of(e), sid) == "own"]
    return {**listing, key: kept, "count": len(kept)}


def mark_session(entries: list[dict[str, Any]], sid: str | None) -> list[dict[str, Any]]:
    """`/sc workflows` lists the whole tenant and marks which entries are this
    session's (`this_session`), in place."""
    for entry in entries:
        entry["this_session"] = session.ownership(session.owner_of(entry), sid) == "own"
    return entries


def _session_mark(entry: dict[str, Any]) -> str:
    return "  [this session]" if entry.get("this_session") else ""


def _workspace_clause(entry: dict[str, Any]) -> str:
    """`; workspace <workspace>` for an entry the SessionStart notice names,
    "" when the API named none. Inside the entry's parentheses, never
    `[<workspace>]` in front of its id (SC-DESC-DETAILS, owner 2026-10-14:
    no workspace prefix in front of a workflow's or task's name)."""
    from .workflows import _flat

    workspace = _flat(entry.get("workspace"))
    return f"; workspace {workspace}" if workspace else ""


def _workspace_tag(entry: dict[str, Any]) -> str:
    """`[<workspace>] ` for a listed workflow or task, "" when the API named
    none (SC-ROWS: every line starts with the workspace it runs in)."""
    from .workflows import workspace_tag

    return workspace_tag(entry.get("workspace"))


def _quietly(read: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return read()
    except Exception:  # noqa: BLE001 - the hook's mode is silent on any failure
        return None


def cmd_build_board(client: SwarmClient, args, out) -> int:
    """`sc build-board [--no-open]`: refresh this project's build board (#1773),
    print its path and open it in the default browser. Read-only towards
    SwarmCloud; a failed read leaves the previous page and exits 1."""
    from . import buildboard

    got = buildboard.refresh(client)
    if "error" in got:
        print(f"sc: {got['error']}", file=sys.stderr)
        return EXIT_FAIL
    out.write(f"{got['path']}\n")
    if not getattr(args, "no_open", False):
        why = buildboard.open_page(got["path"])
        if why:
            print(f"sc: {why}", file=sys.stderr)
    return EXIT_OK


def cmd_workflows(client: SwarmClient, args, out) -> int:
    if getattr(args, "session_start", False):
        # THE HOOK'S MODE: silent on everything but a running workflow. A
        # session must start the same whether or not SwarmCloud answered, so a
        # failure prints nothing and exits 0 -- the hook script discards
        # stderr anyway, and this keeps the CLI from relying on that.
        # The two reads at once, inside the hook's ten seconds; each fails
        # alone, so a deployment that answers one still gets that one named.
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                workflows_read = pool.submit(_quietly, lambda: running_workflows(client))
                tasks_read = pool.submit(_quietly, lambda: running_tasks(client))
                from .watch import handed_off, rearm_minutes

                # ONLY THIS SESSION'S (SC-SESSION-SCOPE, owner 2026-10-14): the
                # hook hands over the session id it read on stdin, and work
                # another session started -- or nobody tagged -- is not named.
                sid = session.current()
                # AND THIS PROJECT'S RUNNING WORK (SC-BROWSER-FOLLOWUP, BGD
                # 2026-10-25), at session start only: whatever clones this
                # checkout's origin repository, whichever session started it.
                # `/sc check` and the watch stay this session's.
                from . import layout

                project = project_key()
                context = session_start_context(
                    own_or_project(workflows_read.result(), "workflows", sid, project),
                    own_or_project(tasks_read.result(), "tasks", sid, project),
                    watch_minutes=rearm_minutes(),
                    watch_off=handed_off(),
                    project=project,
                    layout=layout.current()["layout"],
                )
        except Exception:  # noqa: BLE001 - nothing may stop a session starting
            return EXIT_OK
        if context:
            out.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": context,
                        }
                    }
                )
                + "\n"
            )
        return EXIT_OK

    from .launch import named_workspaces
    from .workflows import workspace_names

    listing, failure = _attempt(lambda: running_workflows(client))
    # The caller's running single tasks beside them (#830): `/sc attach --all`
    # gives each a row too, so the list names what it would follow.
    singles, singles_failure = _attempt(lambda: running_tasks(client))
    # Each workspace by its display name where the API gives one (SC-ROWS).
    names = workspace_names(client) if listing is not None or singles is not None else {}
    sid = session.current()
    if listing is not None:
        named_workspaces(listing["workflows"], names)
        mark_session(listing["workflows"], sid)
    if singles is not None:
        named_workspaces(singles["tasks"], names)
        mark_session(singles["tasks"], sid)
    if args.json:
        payload = dict(listing) if listing is not None else {"error": str(failure)}
        payload["session"] = session.describe(sid)
        if singles is not None:
            payload["single_tasks"] = singles
        else:
            payload["single_tasks_error"] = str(singles_failure)
        out.write(json.dumps(payload, indent=2, default=str) + "\n")
        return EXIT_OK if listing is not None and singles is not None else EXIT_FAIL
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    if listing is None:
        _emit([render.section("workflows", "", style), f"  could not be read: {failure}"], out)
        return EXIT_FAIL
    running = listing["workflows"]
    lines = [render.section("workflows", f"{len(running)} running", style)]
    if not running:
        lines.append("  none running")
    # Closest to finish first, in workspace blocks (BOARD-ORDER): the order
    # `running_workflows` answers in, printed as it came.
    for entry in running:
        label = f"  {entry['label']}" if entry.get("label") else ""
        lines.append(
            f"  {_workspace_tag(entry)}{entry.get('workflow_id')}{label}  {entry.get('state')}"
            f"{_board_text(entry)}  {_age_text(entry.get('age_seconds'))}{_session_mark(entry)}"
        )
        lines.append(f"    now: {_steps_text(entry)}")
        if entry.get("waiting_for"):
            lines.append(f"    {entry['waiting_for']}")
        if entry.get("console"):
            lines.append(f"    console: {entry['console']}")
        # Each unfinished step on its own line, with the link the API served
        # for it (owner request 2026-10-04): a PARKED or QUEUED step is the one
        # a person opens the console to look at. No link served, none printed.
        for step in entry.get("current_steps") or []:
            link = f"  console: {step['console']}" if step.get("console") else ""
            lines.append(
                f"      {step.get('step_id')}  {step.get('task_id') or 'no task'}  "
                f"{step.get('state') or 'state not read'}{link}"
            )
    if not listing.get("complete"):
        lines.append(f"  {listing.get('incomplete_because')}")
    if (listing.get("medians") or {}).get("unavailable_because"):
        lines.append(f"  {listing['medians']['unavailable_because']}; p50 and eta are left out")
    if singles is None:
        lines.append(render.section("single tasks", "", style))
        lines.append(f"  could not be read: {singles_failure}")
    else:
        mine = singles["tasks"]
        lines.append(render.section("single tasks", f"{len(mine)} of yours running, in no workflow", style))
        if not mine:
            lines.append("  none running")
        for entry in mine:
            label = f"  {entry['label']}" if entry.get("label") else ""
            link = f"  console: {entry['console']}" if entry.get("console") else ""
            lines.append(
                f"  {_workspace_tag(entry)}{entry.get('task_id')}{label}  {entry.get('state')}  "
                f"{_age_text(entry.get('age_seconds'))}{_session_mark(entry)}{link}"
            )
        if not singles.get("complete"):
            lines.append(f"  {singles.get('incomplete_because')}")
    if running or (singles and singles["tasks"]):
        lines.append("  show them as live rows in Claude Code: /sc attach --all")
        lines.append("  [this session] marks what this Claude Code session started: only those are "
                     "attached by /sc attach --all; /sc attach <wf_id> attaches any other by hand")
    _emit(lines, out)
    return EXIT_OK if singles is not None else EXIT_FAIL


def _dump(snap: Snapshot, out) -> None:
    """The snapshot as fetched, for a script that wants the numbers.

    `null` where a fetch failed, matching the renderer: a consumer of this JSON
    must be able to tell "unreadable" from "empty" as easily as a reader of the
    screen can.

    ACCOUNTS GO THROUGH THE SAME ALLOW-LIST AS THE SCREEN. The table can only
    show the columns it names; this dumps a payload, so without
    `public_account` the two surfaces would have different rules about the
    same account and the file would be the one with none. The bar is that not
    even a token's LENGTH may reach either.

    The exit code is the caller's: this function only writes.
    """
    payload = {
        "api_url": snap.api_url,
        "tier": snap.tier,
        "generated_at": snap.now.isoformat(),
        "tenant": snap.tenant,
        "tenant_error": snap.tenant_error,
        "stats": snap.stats,
        "stats_error": snap.stats_error,
        "capacity": snap.capacity,
        "capacity_error": snap.capacity_error,
        "accounts": (
            None if snap.accounts is None
            else [
                render.public_account(a, render.listing_tenant(snap.accounts))
                for a in snap.accounts
            ]
        ),
        "accounts_error": snap.accounts_error,
        "accounts_absent": snap.accounts_absent,
        "tasks": snap.tasks,
        "tasks_error": snap.tasks_error,
        "leases": snap.leases,
        "leases_error": snap.leases_error,
        "leases_refused": snap.leases_refused,
        # What the task listing did NOT read, as it says on screen: `[]` is a
        # whole listing, a sentence is a floor (#88, SC-F11).
        "tasks_incomplete": render.listing_gaps(snap.tasks),
        # The newest-N page the failed and dead-lettered counts are taken over
        # (#190): its size, whether it held every task, and its oldest task's
        # creation time. None when no task listing was read.
        "tasks_window": (
            dataclasses.asdict(render.listing_window(snap.tasks))
            if render.listing_window(snap.tasks) is not None
            else None
        ),
        # Whose the tenant-scoped figures are (#88, SC-F5).
        "scope_tenant": render.scope_tenant(snap),
    }
    out.write(json.dumps(payload, indent=2, default=str) + "\n")


# --------------------------------------------------------------------------
# The account verbs (S10, BUILD_PROMPT_V2 2.6.1)
# --------------------------------------------------------------------------
#
# THE ONE PLACE `sc` WRITES TO THE CLUSTER, by owner decision (2026-10-01): the
# API already served `PUT /v1/accounts/{id}/state` and `DELETE
# /v1/accounts/{id}` and nothing in the CLI, MCP or plugin reached them, so
# pausing an account meant the console or curl. They live under `sc account`
# (singular), apart from the `sc accounts` VIEW, so a grant for the view can
# never match them; `test_plugin_commands.py` holds every handler below out of
# every skill's grant, as it holds `sc login`.
#
# NO CREDENTIAL CROSSES HERE. The state and removal routes take an id and a
# word; `account add` drives the API's own browser sign-in and never sees a
# token -- only the one-time code the callback page shows, which is sent once
# and printed never.

#: What each verb asks the broker for. The broker owns the state machine
#: (`quota_broker.accounts.AccountState`) and answers an unknown word with a
#: 422, so these are the three words the owner decided the CLI says, not a
#: copy of that machine.
ACCOUNT_VERBS = {"pause": "PAUSED", "resume": "AVAILABLE", "drain": "DRAINING"}

#: WHAT DRAINING DOES, said in ONE place: printed by `sc account drain`, and
#: returned by `swarm_account_drain` as `what_draining_does`. The broker
#: defines DRAINING as "running agents are being moved off"
#: (quota_broker/accounts.py); the move itself is the hot swap built by lane
#: B11. Change the wording here and both surfaces change with it.
DRAINING_MEANS = (
    "DRAINING stops new assignments to this account, and agents already running "
    "on it move to another account at their next turn boundary (hot swap). An "
    "agent with no other account available checkpoints and parks until one is."
)


def resolve_account(client: SwarmClient, ref: str) -> dict[str, Any]:
    """The caller's OWN account named `ref`, by id or by label, from `GET /v1/accounts`.

    OWN ONLY. The listing also carries accounts lent TO the caller, which it
    may run on and may not change: the API answers 404 for them (`_owned`),
    and a label that matched one would send a request bound to fail -- or,
    worse, pause the caller's own account of the same name instead of the one
    they were looking at. So a lent match is refused with whose it is.

    AN AMBIGUOUS LABEL IS REFUSED, never resolved to the first: two accounts
    may share a label, and the one an operator meant is not decidable from the
    word. The refusal names every id it could be, which is what to pass.
    """
    text = (ref or "").strip()
    if not text:
        raise SwarmError(
            f"name the account by its label or id, as `{terminal_command('sc accounts')}` lists it"
        )
    listing = fetch_accounts(client)
    tenant = getattr(listing, "tenant_id", None)
    if not tenant:
        raise SwarmError(
            "the accounts listing did not say which tenant it answered for, so your own "
            "accounts cannot be told from accounts lent to you; nothing was sent"
        )
    own = [a for a in listing if a.get("owner_tenant") == tenant]
    by_id = [a for a in own if a.get("account_id") == text]
    if by_id:
        return by_id[0]
    by_label = [a for a in own if a.get("label") == text]
    if len(by_label) == 1:
        return by_label[0]
    if len(by_label) > 1:
        ids = ", ".join(str(a.get("account_id")) for a in by_label)
        raise SwarmError(
            f"{len(by_label)} of your accounts are labelled {text!r} ({ids}); name the one "
            "you mean by its id. Nothing was sent"
        )
    lent = [a for a in listing if text in (a.get("account_id"), a.get("label")) and a not in own]
    if lent:
        raise SwarmError(
            f"{text!r} is lent to you by tenant {lent[0].get('owner_tenant')!r}; only its owner "
            "can change it. Nothing was sent"
        )
    labels = ", ".join(sorted({str(a.get("label")) for a in own})) or "none"
    raise SwarmError(f"no account of yours is labelled or numbered {text!r} (yours: {labels})")


def set_account_state(client: SwarmClient, account: dict[str, Any], state: str, reason: str = "") -> Any:
    """`PUT /v1/accounts/{id}/state`: the broker's answer, as it gave it."""
    account_id = urllib.parse.quote(str(account["account_id"]), safe="")
    return client.request(
        "PUT", f"/v1/accounts/{account_id}/state", payload={"state": state, "reason": reason or ""}
    )


def remove_account(client: SwarmClient, account: dict[str, Any]) -> Any:
    """`DELETE /v1/accounts/{id}`: the broker's answer, which says the secret is kept."""
    account_id = urllib.parse.quote(str(account["account_id"]), safe="")
    return client.request("DELETE", f"/v1/accounts/{account_id}")


def set_account_max_agents(client: SwarmClient, account: dict[str, Any], max_agents: int) -> Any:
    """`PUT /v1/accounts/{id}/max-agents`: the API's answer, as it gave it."""
    account_id = urllib.parse.quote(str(account["account_id"]), safe="")
    return client.request(
        "PUT", f"/v1/accounts/{account_id}/max-agents", payload={"max_agents": max_agents}
    )


def change_account_state(client: SwarmClient, ref: str, verb: str, reason: str = "") -> dict[str, Any]:
    """One state verb, end to end: resolve, one PUT, and what came back.

    The MCP tools and `sc account <verb>` both answer with this, so the two
    cannot describe the same change differently.
    """
    state = ACCOUNT_VERBS[verb]
    account = resolve_account(client, ref)
    answer = set_account_state(client, account, state, reason)
    out: dict[str, Any] = {
        "account_id": account.get("account_id"),
        "label": account.get("label"),
        "requested_state": state,
        # VERBATIM: the broker owns the state machine, and its answer is the
        # fact; a summary of it here would be a second opinion.
        "broker_answer": answer,
    }
    if state == "DRAINING":
        out["what_draining_does"] = DRAINING_MEANS
    return out


def _ask(prompt: str) -> str:
    """One line typed at the terminal, the prompt on stderr so stdout stays data."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return (sys.stdin.readline() if sys.stdin is not None else "").rstrip("\n")


def cmd_account_state(client: SwarmClient, args, out) -> int:
    done = change_account_state(client, args.account, args.verb, args.reason or "")
    if args.json:
        out.write(json.dumps(done, indent=2, default=str) + "\n")
        return EXIT_OK
    out.write(
        f"asked for {done['requested_state']} on {done['label']} ({done['account_id']}); "
        "the broker answered:\n"
    )
    out.write(json.dumps(done["broker_answer"], indent=2, default=str) + "\n")
    if "what_draining_does" in done:
        out.write(done["what_draining_does"] + "\n")
    return EXIT_OK


def cmd_account_max_agents(client: SwarmClient, args, out) -> int:
    """How many agents one of your accounts may hold at once (#2222), after its
    label is TYPED back, as `remove` is.

    SWARM_ASSUME_YES IS IGNORED. Lowering the cap is how an owner takes
    capacity out of a pool other workspaces may be running on, and raising it
    spends a shared subscription faster; either is one number away from a
    typo. The API refuses anything but 1 to 50 before the broker is asked, so
    this sends the number as typed and prints the refusal in its words.
    """
    text = str(args.max_agents).strip()
    if not text.lstrip("-").isdigit():
        raise SwarmError(f"an account's max_agents is a whole number, not {text!r}; nothing was sent")
    max_agents = int(text)
    account = resolve_account(client, args.account)
    label = str(account.get("label") or "")
    before = account.get("max_agents")
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; an account's cap is typed\n")
    was = f" (now {before})" if isinstance(before, int) else ""
    typed = _ask(
        f"let {label} ({account.get('account_id')}) hold at most {max_agents} agents{was}? "
        "Type its label to confirm: "
    )
    if not label or typed.strip() != label:
        raise SwarmError(f"the label typed was not {label!r}; nothing was changed")
    answer = set_account_max_agents(client, account, max_agents)
    if args.json:
        out.write(json.dumps({"account_id": account.get("account_id"), "label": label,
                              "requested_max_agents": max_agents,
                              "answer": answer}, indent=2, default=str) + "\n")
        return EXIT_OK
    served = answer.get("account") if isinstance(answer, dict) else None
    now = served.get("max_agents") if isinstance(served, dict) else None
    out.write(
        f"{label} ({account.get('account_id')}) may hold at most "
        f"{now if isinstance(now, int) else max_agents} agents at once; agents already "
        "on it stay, and the cap applies from the next assignment\n"
    )
    return EXIT_OK


def cmd_account_remove(client: SwarmClient, args, out) -> int:
    """Remove an account, after its label is TYPED back.

    SWARM_ASSUME_YES IS IGNORED (CLAUDE.md: anything destructive takes a typed
    confirmation). Removing an account drops it from the pool for every
    tenant it is lent to; the broker keeps its secret, so it is recoverable,
    but every agent that would have run on it now will not.
    """
    account = resolve_account(client, args.account)
    label = str(account.get("label") or "")
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; removing an account is typed\n")
    typed = _ask(
        f"remove {label} ({account.get('account_id')}) from the pool? Type its label to confirm: "
    )
    if not label or typed.strip() != label:
        raise SwarmError(f"the label typed was not {label!r}; nothing was removed")
    answer = remove_account(client, account)
    if args.json:
        out.write(json.dumps({"account_id": account.get("account_id"), "label": label,
                              "broker_answer": answer}, indent=2, default=str) + "\n")
        return EXIT_OK
    out.write(f"removed {label} ({account.get('account_id')}); the broker answered:\n")
    out.write(json.dumps(answer, indent=2, default=str) + "\n")
    return EXIT_OK


#: `POST /v1/admin/history:purge` (swarm_api/purge.py) and what it accepts.
PURGE_PATH = "/v1/admin/history:purge"
PURGE_STATES = ("FAILED", "CANCELLED", "DEAD_LETTERED")
PURGE_WORD = "purge"
#: Pages one invocation follows. The route bounds each page; this bounds the
#: command, so a history larger than expected stops and says so.
PURGE_MAX_PAGES = 200


def _purge_body(args, *, dry_run: bool, page_token: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "states": list(args.states or ["FAILED"]),
        "exclude_ids": list(args.exclude or []),
        "dry_run": dry_run,
        "limit": args.limit,
    }
    if args.before:
        body["before"] = args.before
    if args.tenant:
        body["tenant_id"] = args.tenant
    if page_token:
        body["page_token"] = page_token
    if not dry_run:
        body["confirm"] = PURGE_WORD
    return body


def _purge_pages(client: SwarmClient, args, *, dry_run: bool) -> dict[str, Any]:
    """Every page of one purge (or its dry run), merged."""
    merged: dict[str, Any] = {"workflows": [], "tasks": [], "skipped": [], "failed": [],
                              "complete": False}
    token: str | None = None
    for _ in range(PURGE_MAX_PAGES):
        page = client.request("POST", PURGE_PATH,
                              payload=_purge_body(args, dry_run=dry_run, page_token=token))
        if not isinstance(page, dict):
            raise SwarmError(f"POST {PURGE_PATH} answered with no body; nothing more was sent")
        for key in ("workflows", "tasks", "skipped", "failed"):
            merged[key] += list(page.get(key) or [])
        token = page.get("next_page_token")
        if not token:
            merged["complete"] = True
            break
    return merged


def _purge_lines(result: dict[str, Any], *, deleted: bool) -> list[str]:
    verb = "deleted" if deleted else "would delete"
    rows = result["workflows"] + result["tasks"]
    objects = sum(int(r.get("artifact_objects") or 0) for r in rows)
    lines = [
        f"{verb}: {len(result['workflows'])} workflow(s), {len(result['tasks'])} standalone "
        f"task(s), {sum(len(r.get('task_ids') or ()) for r in rows)} task document(s), "
        f"{objects} artifact object(s)"
    ]
    for kind, key in (("workflow", "workflows"), ("task", "tasks")):
        for r in result[key]:
            count = r.get("artifact_objects")
            lines.append(
                f"  {kind:<8} {r.get('id')}  {r.get('tenant_id')}  {r.get('state')}  "
                f"tasks={len(r.get('task_ids') or ())}  objects="
                f"{'unlisted' if count is None else count}"
            )
    if result["skipped"]:
        lines.append(f"kept: {len(result['skipped'])}")
        for r in result["skipped"]:
            lines.append(f"  {r.get('kind'):<8} {r.get('id')}  {r.get('reason')}: {r.get('detail')}")
    if result["failed"]:
        lines.append(f"failed (documents kept; run again to retry): {len(result['failed'])}")
        for r in result["failed"]:
            lines.append(f"  {r.get('kind'):<8} {r.get('id')}  {r.get('reason')}")
    if not result["complete"]:
        lines.append(f"stopped after {PURGE_MAX_PAGES} pages; run it again for the rest")
    return lines


def cmd_admin_purge(client: SwarmClient, args, out) -> int:
    """The audited purge of failed history; a dry run unless `--delete`.

    THE DRY RUN IS ALWAYS PRINTED FIRST, and with `--delete` the word `purge`
    must then be TYPED at a terminal. SWARM_ASSUME_YES IS IGNORED (CLAUDE.md:
    anything destructive takes a typed confirmation), and no terminal to type
    at means nothing is deleted. The delete re-selects with the same filters,
    so it prints what it actually deleted, which is the record to read.
    """
    plan = _purge_pages(client, args, dry_run=True)
    if args.json and not args.delete:
        out.write(json.dumps(plan, indent=2, default=str) + "\n")
        return EXIT_OK
    _emit(_purge_lines(plan, deleted=False), out)
    if not args.delete:
        out.write(f"dry run: nothing was deleted. Add --delete to delete, after typing {PURGE_WORD!r}\n")
        return EXIT_OK
    if not plan["workflows"] and not plan["tasks"]:
        out.write("nothing to delete\n")
        return EXIT_OK
    out.flush()
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; a purge is typed\n")
    stdin = sys.stdin
    if stdin is None or not getattr(stdin, "isatty", lambda: False)():
        raise SwarmError(f"a purge is confirmed by typing {PURGE_WORD!r} at a terminal, and "
                         "stdin is not one; nothing was deleted")
    typed = _ask(f"delete the history above, audited and for good? Type {PURGE_WORD!r} to confirm: ")
    if typed.strip() != PURGE_WORD:
        raise SwarmError(f"{PURGE_WORD!r} was not typed; nothing was deleted")
    done = _purge_pages(client, args, dry_run=False)
    if args.json:
        out.write(json.dumps(done, indent=2, default=str) + "\n")
    else:
        _emit(_purge_lines(done, deleted=True), out)
    return EXIT_FAIL if done["failed"] else EXIT_OK


def _open_browser(url: str) -> bool:
    """Open `url` in the default browser; False where that is not possible."""
    import webbrowser

    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False


def _read_code(prompt: str) -> str:
    """The pasted code, read with NO ECHO: it is a one-time credential, and a
    terminal that echoed it would leave it in a scrollback and a screen share."""
    import getpass

    return getpass.getpass(prompt)


def _masked(text: str, secrets: Sequence[str]) -> str:
    """`text` with every secret in `secrets` -- and each part of a pasted
    `code#state` -- replaced. Longest first, so a part inside a whole is not
    left half-shown."""
    parts: set[str] = set()
    for secret in secrets:
        if secret:
            parts.add(secret)
            parts.update(piece for piece in secret.split("#") if len(piece) >= 4)
    for secret in sorted(parts, key=len, reverse=True):
        text = text.replace(secret, "****")
    return text


#: The account kinds `sc account add` signs in to. `anthropic` is the Claude
#: subscription pool; `openai` a ChatGPT plan for the codex runner (#2688, BGD
#: 2026-11-01: codex runs on ChatGPT plans from a shared pool).
ACCOUNT_PROVIDERS = ("anthropic", "openai")


def _catch_callback(redirect_uri: str, state: str, timeout: float) -> str | None:
    """Listen where a ChatGPT plan's sign-in lands, on THIS machine, and return
    the authorization code its redirect carries -- or None when the port could
    not be opened or nothing arrived in time.

    The redirect is `http://localhost:1455/auth/callback`, the one the Codex
    client registered (measured: the 127.0.0.1 form failed live). Bound to the
    loopback interface only, it answers one request whose `state` matches and
    refuses any other, so a page that guesses the port cannot file a code. The
    code is returned, never printed; the page says only that the sign-in
    reached the terminal.
    """
    import http.server
    import time
    from urllib.parse import parse_qs, urlsplit

    target = urlsplit(redirect_uri)
    caught: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- http.server's name
            asked = urlsplit(self.path)
            query = parse_qs(asked.query)
            code = (query.get("code") or [""])[0]
            if asked.path != target.path or (query.get("state") or [""])[0] != state or not code:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"This is not the sign-in sc started. Nothing was sent.\n")
                return
            caught["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. Return to your terminal; you can close this tab.\n")

        def log_message(self, *args):  # the request line carries the code
            return

    try:
        server = http.server.HTTPServer(("127.0.0.1", target.port or 80), Callback)
    except OSError:
        return None
    server.timeout = 1.0
    deadline = time.monotonic() + max(1.0, timeout)
    try:
        while not caught and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return caught.get("code")


def _code_from_address(pasted: str, state: str) -> str:
    """The code from the address a browser landed on, pasted when the local
    port could not be opened. Refused when its `state` is another sign-in's."""
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(pasted.strip()).query)
    if not query:
        return pasted.strip()
    if (query.get("state") or [""])[0] != state:
        raise SwarmError("that address belongs to a different sign-in; nothing was registered")
    return (query.get("code") or [""])[0]


def _plan_sign_in_code(started: dict, state: str) -> str:
    """Wait for a ChatGPT plan's callback on this machine, or ask for the
    address the browser landed on when the port is taken."""
    redirect = started.get("redirect_uri")
    if not isinstance(redirect, str) or not redirect.startswith("http://localhost:"):
        raise SwarmError("the API named no local address for the ChatGPT sign-in; nothing was registered")
    expires = started.get("expires_in_seconds")
    timeout = float(expires) if isinstance(expires, int) and expires > 0 else 900.0
    sys.stderr.write("waiting for the sign-in to come back to this machine...\n")
    code = _catch_callback(redirect, state, timeout)
    if code is None:
        sys.stderr.write(
            f"could not receive the sign-in on {redirect} (the port is in use, or "
            "it timed out). If your browser shows a page that failed to load, "
            "paste its whole address.\n"
        )
        code = _code_from_address(_read_code("paste the address (it is not echoed): "), state)
    return code


def cmd_account_add(client: SwarmClient, args, out) -> int:
    """Add a subscription account through the API's OWN browser sign-in.

    THE FLOW IS THE ONE THE CONSOLE USES (owner decision 2026-10-01):
    `POST /v1/accounts/authorize` with the label returns the URL to open and
    the `state` that keys the pending sign-in; the operator signs in with
    Anthropic, the callback page shows a code, and `POST
    /v1/accounts/exchange` with the state and the pasted code registers the
    account into the caller's own tenant (the API takes the tenant from the
    verified token, never from here). It never calls `POST /v1/accounts`, the
    credential-upload route, and never reads or writes a credential file.

    THE CODE AND THE STATE ARE PRINTED NOWHERE. The code is read without
    echo, sent once in the exchange body, and masked out of any error the
    exchange raises -- a 422 can quote what it was given. The authorize URL
    is printed, for a machine whose browser cannot be opened, and it carries
    the OAuth `state` as a query parameter because Anthropic's page requires
    it there; it is not printed anywhere else.
    """
    label = (args.label or "").strip()
    if not label:
        raise SwarmError("`--label` names the account in the pool; give one. Nothing was sent")
    lend_to = [t.strip() for t in (args.lend_to or []) if t and t.strip()]
    provider = (getattr(args, "provider", None) or "anthropic").strip().lower()
    if provider not in ACCOUNT_PROVIDERS:
        raise SwarmError(f"--provider is one of {', '.join(ACCOUNT_PROVIDERS)}. Nothing was sent")
    payload: dict[str, Any] = {"label": label, "lend_to": lend_to}
    if provider != "anthropic":
        # Only named when it is not the default, so a Claude add sends exactly
        # what it always sent.
        payload["provider"] = provider
    started = client.request("POST", "/v1/accounts/authorize", payload=payload)
    url = started.get("authorize_url") if isinstance(started, dict) else None
    state = started.get("state") if isinstance(started, dict) else None
    if not url or not state:
        raise SwarmError("the API began no sign-in: its answer named no URL to open")
    if provider == "openai" and started.get("provider") != "openai":
        # An API that dropped the provider started a CLAUDE sign-in: opening
        # it would sign the person in to the wrong service and wait forever on
        # a callback that page never makes.
        raise SwarmError(
            "this deployment's API did not start a ChatGPT sign-in (its answer "
            "names no openai provider), so nothing was opened or registered"
        )
    opened = _open_browser(url)
    sys.stderr.write(
        ("opened your browser to sign in. " if opened else "could not open a browser. ")
        + f"If it did not open, visit:\n  {url}\n"
    )
    minutes = started.get("expires_in_seconds")
    if isinstance(minutes, int):
        sys.stderr.write(f"the sign-in expires in {minutes // 60} minutes\n")
    if provider == "openai":
        code = _plan_sign_in_code(started, state).strip()
    else:
        code = _read_code("paste the code the page shows (it is not echoed): ").strip()
    if not code:
        raise SwarmError("no code was pasted; nothing was registered. Run it again to start over")
    try:
        created = client.request(
            "POST", "/v1/accounts/exchange", payload={"state": state, "code": code}
        )
    except SwarmError as exc:
        raise SwarmError(_masked(str(exc), [code, state]), status=exc.status, edge=exc.edge) from None
    account = created.get("account") if isinstance(created, dict) else None
    account = account if isinstance(account, dict) else {}
    out.write(f"label       {account.get('label') or '(not reported)'}\n")
    out.write(f"account     {account.get('account_id') or '(not reported)'}\n")
    out.write(f"state       {account.get('state') or '(not reported)'}\n")
    # WHICH ORGANISATION THE SIGN-IN AUTHORISED (#2222): the selected one on
    # claude.ai, which may not be the one meant. Said here so a wrong pick
    # shows at once rather than in the week's figures two days later.
    registered = created.get("registered") if isinstance(created, dict) else None
    out.write(f"{registered or 'registered: (organisation not reported)'}\n")
    if isinstance(created, dict) and created.get("note"):
        out.write(f"note        {created['note']}\n")
    return EXIT_OK


# --------------------------------------------------------------------------
# Onboarding: `sc setup` and `sc access` (#780, lane OB9)
# --------------------------------------------------------------------------
#
# THE SAME STATE MACHINE AS THE CONSOLE (docs/onboarding.md §2.1, §4). Every
# step's state is read from `GET /v1/onboarding`, and every action goes
# through the routes the console's checklist uses, so a person who connects
# GitHub in the console and chooses repositories here sees one checklist.
# Nothing here decides that a step is done: it acts, then reads the checklist
# again, and prints what the API derived.
#
# THESE WRITE, so they are held out of every grant like `sc account`:
# connecting starts an authorisation, and enabling an owner, granting,
# revoking and verifying change what SwarmCloud may do as the person, or
# what it recorded about it. `/sc:setup` drives the same functions through
# the bridge tools, granted by name; `remove-org` and `disconnect` are typed
# by the person and have no tool.
#
# NO TOKEN, CODE OR STATE IS EVER PRINTED. No route used here returns a
# token. The authorize URL carries GitHub's `state` because GitHub requires
# it there; it is printed once, as the URL, for a machine whose browser
# cannot be opened, and nowhere else -- the state is never read out of it,
# kept, or sent back. The exchange happens in the browser, on the console's
# callback page, under the person's own sign-in; this side only waits for
# the checklist to say the connection exists.

#: The `surface` the authorize call names. swarm-api's `AuthorizeBody` takes
#: `console` or `plugin`, and `sc` is the plugin's own package (the bridge
#: and this command are one install), so the terminal is the plugin surface.
SETUP_SURFACE = "plugin"

#: How long `sc setup` waits for the browser half. The link itself lives ten
#: minutes (swarm-api's `STATE_TTL`), and the wait is cut to the expiry the
#: API states when that is shorter: waiting on a dead link helps nobody.
CONNECT_TIMEOUT_SECONDS = 600

#: How often the wait reads the checklist. One cheap read; a person takes
#: tens of seconds to approve in the browser.
CONNECT_POLL_SECONDS = 3.0

#: A step's state, as a checklist mark (the console draws the same five).
_SETUP_MARKS = {"done": "[x]", "stale": "[?]", "in_progress": "[~]", "failed": "[!]", "todo": "[ ]"}

#: A step whose evidence counts. `stale` is done once, its evidence old.
_SETUP_DONE = ("done", "stale")

#: swarm-api's `repositories.repo_id_for` recipe, restated as the scheduler
#: restates it (`scheduler/credentials.py`): the access routes take the id,
#: and the API refuses one that is not this tenant's id for the repository,
#: so a drift here is a loud 422 and never a grant on the wrong repository.
_FORGE_HOST = "github.com"


def setup_status(client: SwarmClient) -> dict[str, Any]:
    """`GET /v1/onboarding`: the caller's six steps, next step, recovery copy."""
    view = client.request("GET", "/v1/onboarding")
    if not isinstance(view, dict) or not isinstance(view.get("steps"), list):
        raise SwarmError("GET /v1/onboarding answered no checklist; nothing was read")
    return view


def _setup_step(view: dict[str, Any], name: str) -> dict[str, Any]:
    for step in view.get("steps") or []:
        if isinstance(step, dict) and step.get("step") == name:
            return step
    return {}


def connected_as_you(view: dict[str, Any]) -> str | None:
    """The GitHub login SwarmCloud acts as YOU through, or None.

    `github_connected` is also `done` when the caller acts through the
    TENANT's token (`evidence.via == "tenant"`): that is not connecting as
    yourself (D1), and the access routes refuse it, so only `via == "user"`
    counts here."""
    step = _setup_step(view, "github_connected")
    evidence = step.get("evidence") if isinstance(step.get("evidence"), dict) else {}
    if step.get("state") in _SETUP_DONE and evidence.get("via") == "user":
        return str(evidence.get("forge_login") or "your GitHub account")
    return None


def _setup_issues(step: dict[str, Any]) -> list[dict[str, Any]]:
    issues = [i for i in step.get("issues") or [] if isinstance(i, dict) and i.get("code")]
    if not issues and step.get("code"):
        issues = [{"code": step.get("code"), "copy": step.get("copy")}]
    return issues


def _setup_note(step: dict[str, Any]) -> str:
    state = str(step.get("state") or "unknown")
    evidence = step.get("evidence") if isinstance(step.get("evidence"), dict) else {}
    if step.get("step") == "workspace" and evidence.get("state"):
        note = workspace_line(evidence)
    elif step.get("step") == "claude_account":
        note = claude_line(evidence)
    elif step.get("step") == "github_connected" and state in _SETUP_DONE:
        login = evidence.get("forge_login") or "an account GitHub did not name"
        note = f"connected as {login}"
        if evidence.get("via") == "tenant":
            note += " (the tenant's token, not yours: connect GitHub as yourself)"
    elif state == "todo" and evidence.get("waiting_for"):
        note = f"waiting for {evidence['waiting_for']}"
    else:
        note = state.replace("_", " ")
    if state == "stale":
        note += " (its evidence is old; it is re-checked on the next pass)"
    return note


def _wrapped(text: str, indent: str) -> list[str]:
    import textwrap

    return textwrap.wrap(str(text), width=78, initial_indent=indent, subsequent_indent=indent)


def setup_checklist(view: dict[str, Any]) -> list[str]:
    """The checklist the console draws, as lines: one per step, each failure's
    code and its recovery copy word for word, then what is next."""
    lines = [
        f"SwarmCloud setup for {view.get('user') or '(not reported)'} "
        f"in tenant {view.get('tenant_id') or '(not reported)'}"
    ]
    nxt = view.get("next_step")
    for step in view.get("steps") or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("step") or "?")
        mark = _SETUP_MARKS.get(str(step.get("state")), "[?]")
        flag = "   <- next" if name == nxt else ""
        lines.append(f"  {mark} {name:<17} {_setup_note(step)}{flag}")
        for issue in _setup_issues(step):
            lines.append(f"        {issue['code']}")
            if issue.get("copy"):
                lines += _wrapped(issue["copy"], "          ")
    lines.append("Ready: every step is done." if nxt is None else f"Next: {nxt}")
    return lines


def start_github_connect(client: SwarmClient) -> dict[str, Any]:
    """`POST /v1/onboarding/github/authorize`: the URL to open and its expiry.

    ONLY THOSE TWO are kept. Anything else the answer carried is dropped here,
    so a field added to the route later cannot reach a terminal or a tool
    reply without someone reading it first."""
    started = client.request(
        "POST", "/v1/onboarding/github/authorize", payload={"surface": SETUP_SURFACE}
    )
    url = started.get("authorize_url") if isinstance(started, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise SwarmError(
            "the API began no GitHub authorisation: its answer named no https URL to open. "
            "Nothing was stored"
        )
    expires = started.get("expires_in_seconds")
    return {
        "authorize_url": url,
        "expires_in_seconds": expires if isinstance(expires, int) and expires > 0 else None,
    }


def wait_for_github(
    client: SwarmClient,
    *,
    timeout: float,
    interval: float = CONNECT_POLL_SECONDS,
    sleep: Callable[[float], Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Read the checklist until GitHub is connected as you, or `timeout`
    seconds pass. `(login, view)`; the login is None on a timeout, and the
    view is always the last one read."""
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max(0.0, float(timeout))
    while True:
        view = setup_status(client)
        login = connected_as_you(view)
        left = deadline - clock()
        if login is not None or left <= 0:
            return login, view
        sleep(min(max(interval, 0.1), left))


def not_connected_text(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    return (
        f"GitHub was not connected within {minutes} minute{'s' if minutes != 1 else ''}. "
        "If you approved SwarmCloud in the browser, its callback page finishes the "
        "connection only in a browser signed in to the SwarmCloud console: sign in there, "
        f"then run `{terminal_command('sc setup')}` again for a fresh link (a link lasts "
        "10 minutes and works once). Nothing was stored."
    )


def refusal_text(exc: SwarmError) -> str:
    """An access refusal as the person should read it: §2.3's code and its
    recovery copy, word for word, when the API served them; else the error."""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code, recovery = detail.get("failure_code"), detail.get("recovery")
    if code and recovery:
        return f"{code}: {recovery}"
    return str(exc)


# -- personal workspaces (#847, lane W8; docs/workspaces.md §6.2) ------------
#
# The person's own workspace and Claude account: the two checklist steps after
# `signed_in`. The routes are always the CALLER'S OWN (`GET`/`POST
# /v1/workspace`, `POST /v1/workspace/loan-request`): nothing here names a
# tenant, an email or a workspace id, so nobody can ask for anyone else's.
# Nothing here waits for an approval or a job: a request is recorded and
# answered, and the next `sc setup` or `/sc:setup` reads where it stands.

#: The submission gate's two refusals (§5.2). Every submission verb prints
#: them through `workspace_refusal_text`, from the one place each surface
#: prints an error (`main` here, `server._tool_error_text` for the tools).
WORKSPACE_REFUSALS = ("WORKSPACE_NOT_READY", "NO_CLAUDE_ACCOUNT")

#: The plugin's own spelling of setup, which a tool reply names.
PLUGIN_SETUP_COMMAND = "/sc:setup"

#: The record's fields a terminal or a tool reply carries. Not `tenant_id`:
#: it is derived from the person's email, so it is a name, and the opaque
#: workspace id is what identifies a workspace outside Firestore (§2.6).
WORKSPACE_FIELDS = ("state", "workspace_id", "requested_at", "decision", "steps", "failure",
                    "ready_at", "request_again_at", "setup_url", "setup_command")

#: §4.2's console labels, by the job's step id.
_WORKSPACE_STEP_LABELS = {
    "A1": "Approved", "A2": "Checking the name is free", "A3": "Identity", "A4": "Identity",
    "A5": "Access", "A6": "Access", "A7": "Namespace", "A8": "Limits", "A9": "Final check",
}


def api_message(exc: SwarmError) -> str:
    """The API's own sentence out of a refusal, word for word.

    `SwarmClient` words an API refusal `<METHOD> <path> -> <status>: <message>`
    and, when the body carried a detail object, appends it as ` (<json>)`
    (`client._explain_json`). Both are taken off again here, exactly, so what
    is left is the `message` the API wrote; anything not in that shape is
    returned whole rather than cut at a guess."""
    text = str(exc)
    if exc.status is not None:
        _head, sep, rest = text.partition(f" -> {exc.status}: ")
        if sep:
            text = rest
    if isinstance(exc.detail, dict) and exc.detail:
        tail = f" ({json.dumps(exc.detail, default=str)})"
        if text.endswith(tail):
            text = text[: -len(tail)]
    return text


def api_refusal(exc: SwarmError) -> str:
    """`CODE: the API's sentence`, or the error as it came when there is no code."""
    return f"{exc.code}: {api_message(exc)}" if exc.code else str(exc)


def workspace_refusal_text(exc: SwarmError, setup_command: str) -> str | None:
    """The gate's refusal as §6.2 prints it, or None for any other error:

        ✕ 403 WORKSPACE_NOT_READY: <the API's message, word for word>
          Finish setup with /sc:setup.
    """
    if exc.status != 403 or exc.code not in WORKSPACE_REFUSALS:
        return None
    return f"✕ 403 {exc.code}: {api_message(exc)}\n  Finish setup with {setup_command}."


def preferences_refusal_text(exc: SwarmError) -> str | None:
    """A 503 `preferences_unavailable` as `sc` prints it, or None for any other
    error (docs/design/user-profile.md §4.5, Q9):

        ✕ 503 preferences_unavailable: <the API's message, word for word>

    The API answered, so this is not a connection failure and `swarm doctor`
    is no answer to it; its message already says how to name a workspace.
    Nothing is retried in another workspace: the request named none, and
    picking one here is the guess the API refused to make.
    """
    if exc.status != 503 or exc.code != "preferences_unavailable":
        return None
    return f"✕ 503 {exc.code}: {api_message(exc)}"


def _workspace_shown(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or not isinstance(record.get("state"), str):
        raise SwarmError("the API answered no workspace record; nothing was read")
    return {key: record.get(key) for key in WORKSPACE_FIELDS if key in record}


def workspace_status(client: SwarmClient) -> dict[str, Any]:
    """`GET /v1/workspace`: the caller's own record, or `{state: "none"}`."""
    return _workspace_shown(client.request("GET", "/v1/workspace"))


def request_workspace(client: SwarmClient) -> dict[str, Any]:
    """`POST /v1/workspace`: ask for it. Idempotent: a standing request, an
    approval under way or a ready workspace is answered as it is (§1.3)."""
    return _workspace_shown(
        client.request("POST", "/v1/workspace", payload={"via": SETUP_SURFACE}))


def request_loan(client: SwarmClient) -> dict[str, Any]:
    """`POST /v1/workspace/loan-request`: ask an admin to lend a Claude
    account. Idempotent; refused until a workspace has been requested."""
    answer = client.request("POST", "/v1/workspace/loan-request",
                            payload={"via": SETUP_SURFACE})
    if not isinstance(answer, dict):
        raise SwarmError("the API answered no loan request; nothing was read")
    return {key: answer.get(key) for key in ("state", "workspace_id", "requested_at")}


def _steps_done(record: dict[str, Any]) -> str:
    steps = record.get("steps") if isinstance(record.get("steps"), dict) else {}
    done = sum(1 for step in _WORKSPACE_STEP_LABELS
               if isinstance(steps.get(step), dict) and steps[step].get("state") == "done")
    return f"{done} of {len(_WORKSPACE_STEP_LABELS)} steps done"


def workspace_line(record: dict[str, Any]) -> str:
    """The record's state in one line, with its workspace id."""
    state = str(record.get("state") or "none")
    wid = record.get("workspace_id")
    named = f" ({wid})" if wid else ""
    if state == "none":
        return "not requested"
    if state == "requested":
        return f"requested{named} — waiting for an admin"
    if state in ("approved", "applying"):
        return f"being set up{named} — {_steps_done(record)}"
    if state == "needs_owner":
        return (f"approved{named} — a change needs the platform owner's review before "
                "it can finish")
    if state == "ready":
        return f"ready{named}"
    if state == "denied":
        reason = str(((record.get("decision") or {}) if isinstance(record.get("decision"), dict)
                      else {}).get("reason") or "").strip()
        return f"not approved{named}: {reason}" if reason else f"not approved{named}"
    if state == "failed":
        failure = record.get("failure") if isinstance(record.get("failure"), dict) else {}
        step = _WORKSPACE_STEP_LABELS.get(str(failure.get("step") or ""), failure.get("step"))
        return f"stopped{named}" + (f" at {step}" if step else "")
    return state.replace("_", " ")


def claude_line(evidence: dict[str, Any]) -> str:
    """The `claude_account` step's evidence in one line (§5.1 (3)'s sources)."""
    own, lent = evidence.get("own") or 0, evidence.get("lent") or 0
    if own:
        return f"own ({own})"
    if lent:
        return f"lent ({lent})"
    if evidence.get("provider_key"):
        return "provider key"
    if evidence.get("loan_request") == "requested":
        return "loan requested"
    return "none"


def _may_ask_again(record: dict[str, Any]) -> bool:
    """A denied record may be requested again once its wait is over (§1.3)."""
    again = record.get("request_again_at")
    if not again:
        return True
    try:
        at = datetime.fromisoformat(str(again).replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at <= datetime.now(timezone.utc)


def _setup_workspace(client: SwarmClient, view: dict[str, Any], out,
                     ask: Callable[[str], str]) -> None:
    """§6.2's two steps, before GitHub: offer the request when there is none
    (or a denial's wait is over), say where a waiting or applying one stands
    and move on, then offer a loan when there is no Claude account. Nothing
    here waits for an admin or the job. An API without the steps skips this."""
    step = _setup_step(view, "workspace")
    if not step:
        return
    record = step.get("evidence") if isinstance(step.get("evidence"), dict) else {}
    state = str(record.get("state") or "none")
    if state == "none" or (state == "denied" and _may_ask_again(record)):
        question = ("\nRequest your workspace now? An admin approves it; it is ready a few "
                    "minutes after that. [Y/n] " if state == "none"
                    else "\nRequest your workspace again? [y/N] ")
        if _yes(ask(question), default=state == "none"):
            try:
                record = request_workspace(client)
            except SwarmError as exc:
                out.write(f"  workspace: {api_refusal(exc)}\n")
            else:
                state = str(record.get("state") or "none")
                out.write(f"  {_SETUP_MARKS['in_progress']} {'workspace':<17} "
                          f"{workspace_line(record)}\n")
    elif state != "ready":
        out.write(f"\nYour workspace is {workspace_line(record)}. Carrying on meanwhile.\n")

    claude = _setup_step(view, "claude_account")
    evidence = claude.get("evidence") if isinstance(claude.get("evidence"), dict) else {}
    if not claude or claude.get("state") in _SETUP_DONE or \
            evidence.get("loan_request") == "requested":
        return
    if state == "none":
        out.write("  claude account: an admin lends an account to a workspace; request the "
                  "workspace first\n")
        return
    answer = ask(f"  {_SETUP_MARKS['todo']} {'claude account':<17} none — add one in the "
                 "console, or ask for a loan? [loan/skip] ").strip().lower()
    if answer != "loan":
        out.write("  claude account: skipped. Add your own on Capacity › Accounts in the "
                  f"console, or with `{terminal_command('sc account add --label <name>')}`\n")
        return
    try:
        request_loan(client)
    except SwarmError as exc:
        out.write(f"  claude account: {api_refusal(exc)}\n")
        return
    out.write(f"  {_SETUP_MARKS['in_progress']} {'claude account':<17} loan requested\n")


def repository_name(text: str) -> str:
    """`owner/repo` from what a person types: `owner/repo`, or its GitHub URL."""
    value = (text or "").strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if len(parts) != 2 or not all(parts) or any(c.isspace() for c in value):
        raise SwarmError(f"{text!r} is not owner/repo, as GitHub spells it. Nothing was sent")
    return value


def repo_id_for(tenant_id: str, repository: str) -> str:
    """swarm-api's `repositories.repo_id_for`: tenant + `github.com/` + the
    lower-cased owner/repo, sha256, 16 hex, `repo_` in front."""
    import hashlib

    key = f"{tenant_id}{_FORGE_HOST}/{repository.lower()}"
    return "repo_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def access_overview(client: SwarmClient) -> dict[str, Any]:
    """`GET /v1/access`: your connection, enabled owners and grants."""
    view = client.request("GET", "/v1/access")
    if not isinstance(view, dict):
        raise SwarmError("GET /v1/access answered nothing readable; nothing was read")
    return view


def access_owners(client: SwarmClient) -> dict[str, Any]:
    """`GET /v1/access/orgs`: every owner you reach, its install state, sso."""
    listing = client.request("GET", "/v1/access/orgs")
    if not isinstance(listing, dict) or not isinstance(listing.get("owners"), list):
        raise SwarmError("GET /v1/access/orgs answered no owners; nothing was read")
    return listing


def enable_owner(client: SwarmClient, owner: str) -> dict[str, Any]:
    return client.request("POST", "/v1/access/orgs", payload={"owner": owner.strip()})


def disable_owner(client: SwarmClient, owner: str) -> dict[str, Any]:
    owner = urllib.parse.quote(owner.strip(), safe="")
    return client.request("DELETE", f"/v1/access/orgs/{owner}")


def repositories_page(
    client: SwarmClient, owner: str, *, page: int = 1, query: str | None = None
) -> dict[str, Any]:
    """`GET /v1/access/orgs/{owner}/repositories`: one page, searched by `query`."""
    params = [("page", str(max(1, int(page))))]
    if query:
        params.append(("q", query))
    named = owner.strip()
    owner, query_string = urllib.parse.quote(named, safe=""), urllib.parse.urlencode(params)
    listing = client.request("GET", f"/v1/access/orgs/{owner}/repositories?{query_string}")
    if not isinstance(listing, dict) or not isinstance(listing.get("repositories"), list):
        raise SwarmError(f"{named}'s repositories were not listed: the API answered no "
                         "repositories; nothing was read")
    return listing


def grant_repository(
    client: SwarmClient,
    repository: str,
    mode: str,
    *,
    tenant_id: str | None = None,
    repo_id: str | None = None,
) -> dict[str, Any]:
    """`PUT /v1/access/grants/{repo_id}`: grant `repository` read or write.

    The API reads the repository once as you and refuses what the mode cannot
    do (archived, no push) with §2.3's copy; nothing is granted then."""
    if mode not in ("read", "write"):
        raise SwarmError(f"a grant is read or write, not {mode!r}. Nothing was sent")
    name = repository_name(repository)
    if repo_id is None:
        tenant = tenant_id or str(access_overview(client).get("tenant_id") or "")
        if not tenant:
            raise SwarmError("the API named no tenant, so the repository's id is unknown. "
                             "Nothing was sent")
        repo_id = repo_id_for(tenant, name)
    return client.request(
        "PUT", f"/v1/access/grants/{repo_id}", payload={"repository": name, "mode": mode}
    )


def held_grant(overview: dict[str, Any], repository: str) -> dict[str, Any] | None:
    """Your grant on `repository` in an overview, matched as GitHub does: by
    owner/repo, case-insensitively."""
    wanted = repository_name(repository).lower()
    for grant in overview.get("grants") or []:
        if isinstance(grant, dict) and str(grant.get("repository") or "").lower() == wanted:
            return grant
    return None


def revoke_repository(client: SwarmClient, repository: str) -> dict[str, Any]:
    """`DELETE /v1/access/grants/{repo_id}` for a grant you hold."""
    grant = held_grant(access_overview(client), repository)
    if grant is None or not grant.get("repo_id"):
        raise SwarmError(
            f"you hold no grant on {repository_name(repository)}; nothing was revoked. "
            f"`{terminal_command('sc access list')}` shows the ones you hold"
        )
    repo_id = grant["repo_id"]
    return client.request("DELETE", f"/v1/access/grants/{repo_id}")


#: The checks a push test asks for: the reads, and D6's one write. Named in
#: full because a body that names `checks` replaces the API's default set.
PUSH_TEST_CHECKS = ("clone", "push", "pull_request", "push_test")


def verify_repositories(
    client: SwarmClient, repositories: Sequence[str] = (), *, push_test: bool = False
) -> list[dict[str, Any]]:
    """`POST /v1/access/grants/{repo_id}/verify` for each grant named, or for
    every grant you hold. Reads only (D6): clone, and for a write grant push
    and pull request. A refused call is that repository's answer, not the end
    of the sweep.

    `push_test` adds D6's opt-in write: on each NAMED write grant, the API
    creates and deletes one branch, `swarmcloud/onboarding-check-<nonce>`.
    It is never run over "every grant", and a read grant is refused here,
    before anything is sent: SwarmCloud enforces read (D9), so it does not
    push there, not even a test branch. The caller has had the person
    confirm it; this function does not ask."""
    if push_test and not repositories:
        raise SwarmError("the push test writes a branch, so it runs only on repositories named "
                         "one by one. Nothing was verified")
    grants = [g for g in access_overview(client).get("grants") or [] if isinstance(g, dict)]
    if repositories:
        by_name = {str(g.get("repository") or "").lower(): g for g in grants}
        picked: list[dict[str, Any]] = []
        for repository in repositories:
            grant = by_name.get(repository_name(repository).lower())
            if grant is None:
                raise SwarmError(
                    f"you hold no grant on {repository_name(repository)}; nothing was verified. "
                    "Grant it first"
                )
            if push_test and grant.get("mode") != "write":
                raise SwarmError(
                    f"your grant on {grant.get('repository')} is read, and the push test writes "
                    "a branch: SwarmCloud enforces read. Change the grant to write first. "
                    "Nothing was verified")
            picked.append(grant)
        grants = picked
    body: dict[str, Any] = {"checks": list(PUSH_TEST_CHECKS)} if push_test else {}
    results: list[dict[str, Any]] = []
    for grant in grants:
        row: dict[str, Any] = {"repository": grant.get("repository"), "mode": grant.get("mode")}
        repo_id = grant.get("repo_id")
        try:
            answer = client.request("POST", f"/v1/access/grants/{repo_id}/verify", payload=body)
        except SwarmError as exc:
            row.update(passed=False, checks={}, failures=[{"check": "verify",
                                                            "code": (exc.detail or {}).get("failure_code"),
                                                            "copy": refusal_text(exc)}])
            results.append(row)
            continue
        answer = answer if isinstance(answer, dict) else {}
        checked = (answer.get("grant") or {}).get("checks") if isinstance(answer.get("grant"), dict) else {}
        row.update(
            passed=bool(answer.get("passed")),
            checks={name: (check or {}).get("state") for name, check in (checked or {}).items()},
            failures=[f for f in answer.get("failures") or [] if isinstance(f, dict)],
        )
        if isinstance(answer.get("push_test"), dict):
            # The branch's name and whether it was left behind: never a token.
            row["push_test"] = {"branch": answer["push_test"].get("branch"),
                                "leftover": bool(answer["push_test"].get("leftover"))}
        results.append(row)
    return results


def verify_lines(results: Sequence[dict[str, Any]]) -> list[str]:
    if not results:
        return ["no grant to verify: choose a repository first"]
    lines: list[str] = []
    for row in results:
        lines.append(f"{row.get('repository')}  ({row.get('mode') or '?'})  "
                     + ("passed" if row.get("passed") else "NOT passed"))
        for name in PUSH_TEST_CHECKS:
            state = (row.get("checks") or {}).get(name)
            if state is not None:
                lines.append(f"    {name.replace('_', ' '):<14} {state}")
        pushed = row.get("push_test")
        if isinstance(pushed, dict) and pushed.get("branch"):
            lines.append(f"    test branch    {pushed['branch']} "
                         + ("was NOT deleted: delete it on GitHub" if pushed.get("leftover")
                            else "created and deleted"))
        for failure in row.get("failures") or []:
            lines.append(f"    {failure.get('check')}: {failure.get('code') or 'refused'}")
            if failure.get("copy"):
                lines += _wrapped(failure["copy"], "      ")
    return lines


def owners_lines(listing: dict[str, Any]) -> list[str]:
    rows = [o for o in listing.get("owners") or [] if isinstance(o, dict)]
    if not rows:
        return ["no owner reachable: GitHub listed no account or organisation for you"]
    lines = []
    for owner in rows:
        kind = "your account" if owner.get("owner_type") == "User" else "organisation"
        state = str(owner.get("install_state") or "unknown").replace("_", " ")
        enabled = ", enabled" if owner.get("enabled") else ""
        sso = ", SSO required" if owner.get("sso") == "required" else ""
        lines.append(f"  {owner.get('owner')!s:<24} {kind:<13} {state}{enabled}{sso}")
        if owner.get("code") and owner.get("copy"):
            # ORG_APPROVAL_PENDING (§2.3): the API's copy, word for word.
            lines += _wrapped(f"{owner['code']}: {owner['copy']}", "      ")
    return lines


def approval_pending(listing: dict[str, Any]) -> list[dict[str, Any]]:
    """The owners waiting on an org owner's approval of the App: each one's
    §2.3 code and copy as the API served them, and when it was asked for."""
    return [{"owner": o.get("owner"), "code": o.get("code"), "copy": o.get("copy"),
             "requested_at": o.get("requested_at")}
            for o in listing.get("owners") or []
            if isinstance(o, dict) and o.get("install_state") == "requested"]


def request_install(client: SwarmClient, owner: str) -> dict[str, Any]:
    """`POST /v1/access/orgs/{owner}/install-request`: record that you asked
    `owner`'s owners to install the App. GitHub shows a user token no
    pending request, so SwarmCloud records it, shows ORG_APPROVAL_PENDING and
    re-checks it every 15 minutes. No GitHub call, no body."""
    named = owner.strip()
    if not named:
        raise SwarmError("name the owner whose owners you asked. Nothing was sent")
    owner = urllib.parse.quote(named, safe="")
    answer = client.request("POST", f"/v1/access/orgs/{owner}/install-request")
    if not isinstance(answer, dict):
        raise SwarmError(f"the install request for {named} answered nothing readable")
    return answer


def repositories_lines(listing: dict[str, Any]) -> list[str]:
    rows = [r for r in listing.get("repositories") or [] if isinstance(r, dict)]
    lines = [f"{listing.get('owner')}, page {listing.get('page') or 1}"
             + (f", search {listing['q']!r}" if listing.get("q") else "")]
    if not rows:
        lines.append("  no repository on this page")
    for index, row in enumerate(rows, 1):
        push = "push" if row.get("can_push") else "read only"
        held = f"  granted {row.get('mode')}" if row.get("granted") else ""
        archived = "  archived" if row.get("archived") else ""
        lines.append(f"  {index:>3}  {row.get('repository')!s:<40} "
                     f"{row.get('visibility') or '?':<8} {push}{archived}{held}")
    if listing.get("next_page"):
        lines.append(f"  more on page {listing['next_page']}")
    if listing.get("capped"):
        lines.append("  the listing stops here (capped): search, or type owner/repo")
    return lines


def access_lines(overview: dict[str, Any]) -> list[str]:
    conn = overview.get("connection") if isinstance(overview.get("connection"), dict) else None
    if conn is None:
        lines = ["GitHub: not connected as you. "
                 f"`{terminal_command('sc setup')}` connects it."]
    else:
        lines = [f"GitHub: {conn.get('state') or 'unknown'} as {conn.get('forge_login') or '?'}"]
        if conn.get("failure"):
            lines.append(f"  {conn['failure']}")
    orgs = [o for o in overview.get("orgs") or [] if isinstance(o, dict)]
    lines.append("Enabled owners: " + (", ".join(str(o.get("owner")) for o in orgs) or "none"))
    grants = [g for g in overview.get("grants") or [] if isinstance(g, dict)]
    lines.append("Repositories:" if grants else "Repositories: none chosen")
    for grant in grants:
        checks = grant.get("checks") or {}
        states = " ".join(f"{name}={(checks.get(name) or {}).get('state')}"
                          for name in ("clone", "push", "pull_request") if name in checks)
        lines.append(f"  {grant.get('repository')!s:<40} {grant.get('mode')!s:<6} "
                     + (states or "not verified"))
    return lines


def _yes(answer: str, *, default: bool) -> bool:
    text = answer.strip().lower()
    if not text:
        return default
    return text in ("y", "yes")


def _choose_in(client: SwarmClient, out, ask: Callable[[str], str], owner: str,
               tenant_id: str) -> int:
    """The chooser for one owner: a page at a time, searched, each pick read
    or write. Returns how many grants were made. Blank ends it."""
    page, query, made = 1, None, 0
    while True:
        try:
            listing = repositories_page(client, owner, page=page, query=query)
        except SwarmError as exc:
            out.write(refusal_text(exc) + "\n")
            return made
        _emit(repositories_lines(listing), out)
        rows = [r for r in listing.get("repositories") or [] if isinstance(r, dict)]
        answer = ask(
            "Grant which? numbers or owner/repo, each with read or write (`1 write, 3 read`); "
            "n next page, p previous, /text to search, blank when done: "
        ).strip()
        if not answer:
            return made
        if answer.lower() == "n":
            if listing.get("next_page"):
                page = int(listing["next_page"])
            else:
                out.write("that is the last page\n")
            continue
        if answer.lower() == "p":
            page = max(1, page - 1)
            continue
        if answer.startswith("/"):
            query, page = answer[1:].strip() or None, 1
            continue
        for pick in (p.strip() for p in answer.split(",")):
            words = pick.split()
            if len(words) != 2 or words[1].lower() not in ("read", "write"):
                out.write(f"{pick!r}: say which, then read or write (`2 write`)\n")
                continue
            which, mode = words[0], words[1].lower()
            repo_id = None
            if which.isdigit():
                if not 1 <= int(which) <= len(rows):
                    out.write(f"{which}: not a number on this page\n")
                    continue
                row = rows[int(which) - 1]
                which, repo_id = str(row.get("repository")), row.get("repo_id")
            try:
                done = grant_repository(client, which, mode, tenant_id=tenant_id, repo_id=repo_id)
            except SwarmError as exc:
                out.write(f"{which}: {refusal_text(exc)}\n")
                continue
            grant = done.get("grant") if isinstance(done, dict) else None
            grant = grant if isinstance(grant, dict) else {}
            made += 1
            out.write(f"granted {grant.get('repository') or which} {grant.get('mode') or mode}"
                      + (" (registered for the tenant)" if isinstance(done, dict)
                         and done.get("registered") else "") + "\n")


def _setup_owners(client: SwarmClient, out, ask: Callable[[str], str],
                  open_browser: Callable[[str], bool], browser: bool) -> list[str]:
    """Enable the owners SwarmCloud's App is installed on; say where to
    install it on the rest. Returns the owners enabled afterwards."""
    while True:
        listing = access_owners(client)
        owners = [o for o in listing["owners"] if isinstance(o, dict) and o.get("owner")]
        out.write("\nOwners you can reach:\n")
        _emit(owners_lines(listing), out)
        enabled = [str(o["owner"]) for o in owners if o.get("enabled")]
        choosable = [str(o["owner"]) for o in owners
                     if o.get("install_state") == "installed" and not o.get("enabled")]
        if choosable:
            answer = ask(f"Enable which? [{', '.join(choosable)}] "
                         "(blank for all of them, `none` to skip): ").strip()
            if not answer or answer.lower() == "all":
                picked = choosable
            elif answer.lower() == "none":
                picked = []
            else:
                typed = [w for w in answer.replace(",", " ").split() if w]
                known = {c.lower(): c for c in choosable}
                picked = [known[w.lower()] for w in typed if w.lower() in known]
                for word in typed:
                    if word.lower() not in known:
                        out.write(f"{word}: not one of {', '.join(choosable)}\n")
            for owner in picked:
                try:
                    enable_owner(client, owner)
                except SwarmError as exc:
                    out.write(f"{owner}: {refusal_text(exc)}\n")
                    continue
                enabled.append(owner)
                out.write(f"enabled {owner}\n")
        missing = [str(o["owner"]) for o in owners if o.get("install_state") != "installed"]
        url = listing.get("install_url")
        if not missing:
            return enabled
        out.write(f"SwarmCloud's GitHub App is not installed on: {', '.join(missing)}.\n")
        if not isinstance(url, str) or not url.startswith("https://"):
            out.write("This deployment named no install page; ask its operator for it.\n")
            return enabled
        out.write(
            f"Install it there, choosing \"Only select repositories\":\n  {url}\n"
            "On an organisation you do not own, GitHub sends its owners a request; it can "
            "be enabled once one of them approves. Record that you asked with "
            f"`{terminal_command('sc access request-install <owner>')}`, and setup shows "
            "ORG_APPROVAL_PENDING and re-checks it every 15 minutes.\n"
            "If the org will not install it, store a fine-grained token for it instead: "
            f"`{terminal_command('sc setup token --owner <owner>')}` (read from stdin).\n"
        )
        if not _yes(ask("Open the install page now? [y/N]: "), default=False):
            return enabled
        if browser and not open_browser(url):
            out.write("could not open a browser; use the address above\n")
        if not _yes(ask("Re-check the owners once it is installed? [y/N]: "), default=False):
            return enabled


def run_setup(
    client: SwarmClient,
    out,
    *,
    ask: Callable[[str], str] | None = None,
    open_browser: Callable[[str], bool] | None = None,
    browser: bool = True,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
    interval: float = CONNECT_POLL_SECONDS,
    sleep: Callable[[float], Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """The wizard, from wherever the checklist stands (§4.3): the workspace
    and the Claude account (docs/workspaces.md §6.2), connect GitHub, enable
    owners, choose repositories, verify, then the checklist again.

    Exit 0 when the checklist says ready, 3 when it stopped short of that (a
    step failed, the browser half timed out, or nothing is installed yet);
    a read that failed raises, which `main` turns into 1."""
    ask = ask or _ask
    open_browser = open_browser or _open_browser
    view = setup_status(client)
    _emit(setup_checklist(view), out)
    _setup_workspace(client, view, out, ask)

    login = connected_as_you(view)
    if login is None:
        out.write(
            "\nNext: connect GitHub. SwarmCloud acts as you, through its GitHub App; the "
            "token goes from GitHub to Secret Manager and you never see it.\n"
        )
        started = start_github_connect(client)
        url, expires = started["authorize_url"], started["expires_in_seconds"]
        opened = browser and open_browser(url)
        out.write(
            ("Opened your browser to authorise SwarmCloud on GitHub.\n" if opened
             else "Open this in a browser to authorise SwarmCloud on GitHub.\n")
            + f"If it did not open, visit:\n  {url}\n"
        )
        limit = min(float(timeout), float(expires)) if expires else float(timeout)
        if expires:
            out.write(f"The link expires in {max(1, expires // 60)} minutes and works once.\n")
        out.write("Waiting for GitHub to say you approved it...\n")
        if hasattr(out, "flush"):
            out.flush()
        login, view = wait_for_github(client, timeout=limit, interval=interval,
                                      sleep=sleep, clock=clock)
        if login is None:
            out.write(not_connected_text(limit) + "\n")
            return EXIT_TROUBLE
        out.write(f"Connected as {login}.\n")

    enabled = _setup_owners(client, out, ask, open_browser, browser)
    if not enabled:
        out.write(
            "\nNo owner is enabled yet, so there is nothing to choose from. Run "
            f"`{terminal_command('sc setup')}` again once SwarmCloud is installed on one.\n"
        )
        return EXIT_TROUBLE

    overview = access_overview(client)
    tenant_id = str(overview.get("tenant_id") or view.get("tenant_id") or "")
    held = [g for g in overview.get("grants") or [] if isinstance(g, dict)]
    out.write("\n")
    for owner in enabled:
        question = (f"Choose repositories in {owner}? [Y/n]: " if not held
                    else f"Change the repositories chosen in {owner}? [y/N]: ")
        if _yes(ask(question), default=not held):
            _choose_in(client, out, ask, owner, tenant_id)

    out.write("\nVerifying (reads only: nothing is pushed):\n")
    results = verify_repositories(client)
    _emit(verify_lines(results), out)
    while any(not r.get("passed") for r in results) and \
            _yes(ask("Re-check the ones that did not pass? [y/N]: "), default=False):
        again = [str(r["repository"]) for r in results if not r.get("passed")]
        results = verify_repositories(client, again)
        _emit(verify_lines(results), out)

    view = setup_status(client)
    out.write("\n")
    _emit(setup_checklist(view), out)
    if view.get("next_step") is None:
        out.write(
            f"Change this any time with `{terminal_command('sc access')}` or Config › GitHub access.\n"
        )
        return EXIT_OK
    out.write(f"Setup paused at {view.get('next_step')}. Run "
              f"`{terminal_command('sc setup')}` again to resume.\n")
    return EXIT_TROUBLE


def cmd_setup(client: SwarmClient, args, out) -> int:
    return run_setup(client, out, browser=not args.no_browser,
                     timeout=max(1, int(args.timeout)))


# -- D5: a fallback token for one owner (`sc setup token --owner <org>`) ------
#
# For an org that will not install the App, or that refuses classic tokens:
# a fine-grained personal access token whose resource owner is that org.
# THE VALUE IS READ FROM STDIN AND NOWHERE ELSE. There is no `--token`: an
# argument lands in shell history and in `ps`, and a model granted a shell
# could be handed one. At a terminal it is read through getpass, with no
# echo; piped (`... | uv run sc setup token --owner o`), one line. It is
# posted ONCE, and every line this prints -- the answer, a refusal, an error
# -- has the value taken out first, in case anything ever echoed it.

#: D5's route: probes the token against the owner, stores it once as a
#: version of the person's per-owner slot, answers the org and the token's
#: names, never the value.
OWNER_TOKEN_PATH = "/v1/onboarding/github/token"


def _read_owner_token(owner: str) -> str:
    stdin = sys.stdin
    if stdin is not None and getattr(stdin, "isatty", lambda: False)():
        import getpass

        return getpass.getpass(f"Personal access token for {owner} (input hidden): ").strip()
    return (stdin.readline() if stdin is not None else "").strip()


def _without(text: str, value: str) -> str:
    return text.replace(value, "[redacted]") if value else text


def cmd_setup_token(client: SwarmClient, args, out) -> int:
    """Exit 0 with the org record, 3 when the API refused the token (§2.3's
    code and copy), 1 through `main` when the API could not be read."""
    owner = args.owner.strip()
    value = _read_owner_token(owner)
    if not value:
        raise SwarmError(f"no token was read from stdin for {owner}; nothing was sent")
    try:
        answer = client.request("POST", OWNER_TOKEN_PATH,
                                payload={"owner": owner, "token": value})
    except SwarmError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        answered = (bool(detail.get("failure_code"))
                    or (exc.status is not None and 400 <= exc.status < 500
                        and exc.status != 401 and not exc.edge))
        if not answered:
            raise SwarmError(_without(str(exc), value), status=exc.status, edge=exc.edge,
                             code=exc.code) from None
        out.write(_without(f"✕ {refusal_text(exc)}", value) + "\n")
        out.write(f"Nothing was stored for {owner}.\n")
        return EXIT_TROUBLE
    answer = answer if isinstance(answer, dict) else {}
    if args.json:
        out.write(_without(json.dumps(answer, indent=2, default=str), value) + "\n")
        return EXIT_OK
    org = answer.get("org") if isinstance(answer.get("org"), dict) else {}
    token = answer.get("token") if isinstance(answer.get("token"), dict) else {}
    kind = str(token.get("kind") or "personal access token").replace("_", " ")
    lines = [f"stored a {kind} for {org.get('owner') or owner}"
             f" (GitHub login {token.get('forge_login') or '?'}, "
             f"expires {token.get('expires_at') or 'not stated'})"]
    state = str(org.get("install_state") or "unknown").replace("_", " ")
    lines.append(f"{org.get('owner') or owner}: {state}"
                 + (", enabled through your token" if org.get("enabled") else ", not enabled"))
    lines.append("SwarmCloud uses it as you, for this owner only; it is never shown again. Run "
                 "this again to replace it; "
                 f"`{terminal_command('sc access remove-org ' + owner)}` revokes it.")
    lines.append(f"Next: `{terminal_command('sc access repos ' + owner)}`, then "
                 f"`{terminal_command('sc access grant <owner/repo> --read')}` (or --write).")
    _emit([_without(line, value) for line in lines], out)
    return EXIT_OK


# -- provider keys (KEYS-PLUGIN, docs/provider-keys.md) ----------------------
#
# `sc key set typesafe [--tenant] [--stdin]` is the ONE place outside the
# console a TypeSafe key is typed, and it is a person's terminal, never a
# model's: no skill is granted it, and no MCP tool sets a key. The value is
# read with getpass (no echo) at a terminal, or one line from stdin with
# --stdin; it is never an argument -- argv is what shell history and `ps`
# keep -- so a line that carries anything beyond the provider and the two
# switches is refused BEFORE anything is sent, without repeating what it
# carried (`key_set_refusal`, from `main`).

#: What `sc key set` refuses with, whatever extra the line carried.
KEY_ON_ARGV = (
    "a key is never taken as a command-line argument: it would land in your shell history "
    "and in `ps`. Nothing was sent. Run `{command}` and paste it at the hidden prompt, "
    "or pipe it in with --stdin"
)

#: The KEYS-IAM bootstrap role is the owner's to apply; until then a deployed
#: PUT answers 503 (docs/provider-keys.md §6), and it is said as exactly that.
KEYS_NOT_SAVABLE = "keys can't be saved yet: {reason}"


class KeySetLineRefused(Exception):
    """`sc key set`'s line did not parse. Carries nothing from the line."""


class _KeySetParser(argparse.ArgumentParser):
    """`sc key set`'s parser, which neither guesses at nor repeats a word.

    A key may start with "-" (base64url's alphabet has it), and argparse then
    reads it as an option. Two of its readings are closed here (#1499):

      * NO ABBREVIATIONS AND NO BUNDLED SHORT FLAGS. "-h<rest>" was the help
        switch with the rest as more flags: it printed help and exited 0, as
        if the line were fine. Only an option's full spelling matches now, so
        any other "-" word is an extra word, which `main` refuses unsent;
      * NO ERROR MESSAGE OF ITS OWN. argparse's ("ambiguous option: <word>",
        "ignored explicit argument <word>") can quote the word, and a "-"-led
        key in the provider's place left `provider` missing, which exited
        from inside the parse. `main` refuses instead, with a fixed text."""

    def _get_option_tuples(self, option_string):
        return []

    def error(self, message):
        raise KeySetLineRefused()


def key_set_refusal(extra: Sequence[str]) -> str | None:
    """Why this `sc key set` line is refused before anything is read or sent,
    or None. Never names what it refuses: a stray word on this line is most
    likely the key itself."""
    if extra:
        return KEY_ON_ARGV.format(command=terminal_command("sc key set typesafe"))
    return None


def _unknown_provider(args) -> str | None:
    """The same, for the provider word, which may be the key typed in its place."""
    if getattr(args, "provider", None) not in keys.PROVIDERS:
        return (f"sc key set takes a provider: one of {list(keys.PROVIDERS)}. What was "
                "given is not repeated, in case it was the key -- a key is never an "
                "argument, since it would land in your shell history. Nothing was sent")
    return None


def _read_key(stdin_flag: bool) -> str | None:
    """The key, or None when there is no terminal to ask at and no --stdin."""
    stdin = sys.stdin
    if stdin_flag:
        return (stdin.readline() if stdin is not None else "").strip()
    if stdin is not None and getattr(stdin, "isatty", lambda: False)():
        import getpass

        return getpass.getpass("TypeSafe API key (input hidden): ").strip()
    return None


def cmd_key_set(client: SwarmClient, args, out) -> int:
    """Exit 0 "stored: version N"; 3 the API refused (a 503 is "keys can't be
    saved yet"); 2 the line or the input was refused and nothing was sent;
    1 through `main` when the API could not be reached."""
    refused = (key_set_refusal(getattr(args, "key_extra", None) or [])
               or _unknown_provider(args))
    if refused is not None:
        print(f"sc: {refused}", file=sys.stderr)
        return EXIT_USAGE
    scope = "tenant" if args.tenant else "user"
    value = _read_key(args.stdin)
    if value is None:
        print("sc: there is no terminal to read the key at without echo; pipe it in with "
              f"`{terminal_command('sc key set typesafe --stdin')}`. Nothing was sent",
              file=sys.stderr)
        return EXIT_USAGE
    if not value:
        print("sc: no key was read; nothing was sent", file=sys.stderr)
        return EXIT_USAGE
    try:
        version = keys.store_key(client, args.provider, scope, value)
    except SwarmError as exc:
        answered = exc.status is not None and not exc.edge and exc.status != 401
        if not answered:
            raise SwarmError(_without(str(exc), value), status=exc.status, edge=exc.edge,
                             code=exc.code) from None
        if exc.status == 503:
            line = KEYS_NOT_SAVABLE.format(reason=api_message(exc))
        else:
            line = f"✕ {api_refusal(exc)}"
        out.write(_without(line, value) + "\n")
        out.write("Nothing was stored.\n")
        return EXIT_TROUBLE
    out.write(f"stored: version {version if version is not None else '?'}\n")
    return EXIT_OK


def cmd_setup_status(client: SwarmClient, args, out) -> int:
    """The checklist. Exit 0 when ready, 3 when a step failed or is still to
    do; a checklist that could not be read is 1, through `main`."""
    view = setup_status(client)
    if args.json:
        out.write(json.dumps(view, indent=2, default=str) + "\n")
    else:
        _emit(setup_checklist(view), out)
    return EXIT_OK if view.get("next_step") is None else EXIT_TROUBLE


def cmd_access(client: SwarmClient, args, out) -> int:
    """The Access page, then grant, revoke or verify until a blank answer."""
    while True:
        overview = access_overview(client)
        _emit(access_lines(overview), out)
        answer = _ask("[g]rant, [r]evoke owner/repo, [v]erify, blank to finish: ").strip()
        if not answer:
            return EXIT_OK
        verb, _, rest = answer.partition(" ")
        verb = verb.lower()
        try:
            if verb in ("g", "grant"):
                owners = [str(o.get("owner")) for o in overview.get("orgs") or []
                          if isinstance(o, dict) and o.get("owner")]
                if not owners:
                    out.write("no owner is enabled: `"
                              f"{terminal_command('sc access add-org <owner>')}` first\n")
                    continue
                owner = owners[0]
                if len(owners) > 1:
                    typed = _ask(f"which owner? [{', '.join(owners)}]: ").strip()
                    owner = next((o for o in owners if o.lower() == typed.lower()), "")
                    if not owner:
                        out.write(f"{typed!r} is not one of {', '.join(owners)}\n")
                        continue
                _choose_in(client, out, _ask, owner, str(overview.get("tenant_id") or ""))
            elif verb in ("r", "revoke"):
                target = rest.strip() or _ask("revoke which owner/repo? ").strip()
                revoke_repository(client, target)
                out.write(f"revoked {repository_name(target)}\n")
            elif verb in ("v", "verify"):
                _emit(verify_lines(verify_repositories(client)), out)
            else:
                out.write(f"{verb!r}: g, r or v\n")
        except SwarmError as exc:
            out.write(refusal_text(exc) + "\n")


def cmd_access_list(client: SwarmClient, args, out) -> int:
    overview = access_overview(client)
    if args.json:
        out.write(json.dumps(overview, indent=2, default=str) + "\n")
    else:
        _emit(access_lines(overview), out)
    return EXIT_OK


def cmd_access_orgs(client: SwarmClient, args, out) -> int:
    listing = access_owners(client)
    if args.json:
        out.write(json.dumps(listing, indent=2, default=str) + "\n")
    else:
        _emit(owners_lines(listing), out)
        if listing.get("install_url"):
            out.write(f"install SwarmCloud's GitHub App: {listing['install_url']}\n")
    return EXIT_OK


def _refused(exc: SwarmError) -> SwarmError:
    """The refusal with §2.3's copy in front, as `main` prints it."""
    return SwarmError(refusal_text(exc), status=exc.status, edge=exc.edge, code=exc.code,
                      detail=exc.detail)


def cmd_access_add_org(client: SwarmClient, args, out) -> int:
    try:
        done = enable_owner(client, args.owner)
    except SwarmError as exc:
        raise _refused(exc) from None
    org = done.get("org") if isinstance(done, dict) else None
    out.write(f"enabled {(org or {}).get('owner') or args.owner}\n")
    return EXIT_OK


def cmd_access_remove_org(client: SwarmClient, args, out) -> int:
    """Disable an owner and delete every grant under it (§2.4), after the
    owner is TYPED back. SWARM_ASSUME_YES is ignored, as `sc account remove`
    ignores it: tasks naming its repositories are refused from that moment."""
    owner = args.owner.strip()
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; removing an owner is typed\n")
    typed = _ask(f"disable {owner} and delete every grant you hold under it? "
                 "Type the owner to confirm: ")
    if typed.strip().lower() != owner.lower():
        raise SwarmError(f"the owner typed was not {owner!r}; nothing was removed")
    try:
        done = disable_owner(client, owner)
    except SwarmError as exc:
        raise _refused(exc) from None
    done = done if isinstance(done, dict) else {}
    out.write(f"disabled {owner}; grants deleted: {done.get('grants_deleted', 0)}\n")
    if done.get("installation_settings_url"):
        out.write("The App stays installed until an owner of it removes it there: "
                  f"{done['installation_settings_url']}\n")
    return EXIT_OK


def cmd_access_repos(client: SwarmClient, args, out) -> int:
    try:
        listing = repositories_page(client, args.owner, page=args.page, query=args.search)
    except SwarmError as exc:
        raise _refused(exc) from None
    if args.json:
        out.write(json.dumps(listing, indent=2, default=str) + "\n")
    else:
        _emit(repositories_lines(listing), out)
    return EXIT_OK


def cmd_access_grant(client: SwarmClient, args, out) -> int:
    if bool(args.read) == bool(args.write):
        raise SwarmError("say --read or --write (one of them). Nothing was sent")
    mode = "write" if args.write else "read"
    try:
        done = grant_repository(client, args.repository, mode)
    except SwarmError as exc:
        raise _refused(exc) from None
    done = done if isinstance(done, dict) else {}
    grant = done.get("grant") if isinstance(done.get("grant"), dict) else {}
    if args.json:
        out.write(json.dumps(done, indent=2, default=str) + "\n")
        return EXIT_OK
    out.write(f"granted {grant.get('repository') or repository_name(args.repository)} "
              f"{grant.get('mode') or mode}"
              + (" (registered for the tenant)" if done.get("registered") else "") + "\n")
    out.write(f"verify it: `{terminal_command('sc access verify ' + repository_name(args.repository))}`\n")
    return EXIT_OK


def cmd_access_revoke(client: SwarmClient, args, out) -> int:
    try:
        done = revoke_repository(client, args.repository)
    except SwarmError as exc:
        raise _refused(exc) from None
    done = done if isinstance(done, dict) else {}
    out.write(f"revoked {repository_name(args.repository)}"
              + ("; the tenant's registration went with your last grant"
                 if done.get("unregistered") else "") + "\n")
    return EXIT_OK


def cmd_access_verify(client: SwarmClient, args, out) -> int:
    """Exit 0 when every grant verified passed, 3 when one did not.

    `--push-test` is D6's opt-in write, for ONE repository: the API creates
    and deletes `swarmcloud/onboarding-check-<nonce>` there as you. It runs
    only after the repository's name is TYPED back, and SWARM_ASSUME_YES is
    ignored, as for every other write here that a person must mean."""
    push_test = bool(getattr(args, "push_test", False))
    if push_test:
        named = list(args.repositories or ())
        if len(named) != 1:
            raise SwarmError("--push-test takes exactly one owner/repo: it creates and deletes "
                             "a branch there, one repository at a time. Nothing was sent")
        repository = repository_name(named[0])
        if os.environ.get("SWARM_ASSUME_YES", "").strip():
            sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; a push test is typed\n")
        typed = _ask(f"create and delete the branch swarmcloud/onboarding-check-<nonce> in "
                     f"{repository}, as you? Type {repository} to confirm: ")
        if typed.strip().lower() != repository.lower():
            raise SwarmError(f"the repository typed was not {repository!r}; nothing was pushed "
                             "and nothing was sent")
    results = verify_repositories(client, args.repositories or (), push_test=push_test)
    if args.json:
        out.write(json.dumps(results, indent=2, default=str) + "\n")
    else:
        _emit(verify_lines(results), out)
    return EXIT_OK if results and all(r.get("passed") for r in results) else EXIT_TROUBLE


def cmd_access_request_install(client: SwarmClient, args, out) -> int:
    """Record that you asked an org's owners to install the App, and print
    the install page to send them. The owner shows ORG_APPROVAL_PENDING until
    GitHub lists the installation."""
    owner = args.owner.strip()
    try:
        done = request_install(client, owner)
    except SwarmError as exc:
        raise _refused(exc) from None
    if args.json:
        out.write(json.dumps(done, indent=2, default=str) + "\n")
        return EXIT_OK
    org = done.get("org") if isinstance(done.get("org"), dict) else {}
    if done.get("recorded") is False:
        out.write(f"{org.get('owner') or owner} is already enabled; nothing was recorded\n")
    else:
        out.write(f"recorded: you asked {org.get('owner') or owner}'s owners to install "
                  "SwarmCloud\n")
    if org.get("code") and org.get("copy"):
        _emit(_wrapped(f"{org['code']}: {org['copy']}", "  "), out)
    url = done.get("install_url")
    if isinstance(url, str) and url.startswith("https://"):
        out.write(f"The install page, to open or to send to one of them:\n  {url}\n")
    else:
        out.write("This deployment named no install page; ask its operator for it.\n")
    return EXIT_OK


def cmd_access_disconnect(client: SwarmClient, args, out) -> int:
    """Revoke SwarmCloud's access as you at GitHub and delete your grants,
    after `disconnect` is TYPED (SWARM_ASSUME_YES ignored)."""
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; disconnecting is typed\n")
    typed = _ask("revoke SwarmCloud's access as you at GitHub and delete every grant you "
                 "hold? Type `disconnect` to confirm: ")
    if typed.strip() != "disconnect":
        raise SwarmError("`disconnect` was not typed; nothing was changed")
    done = client.request("DELETE", "/v1/onboarding/github")
    done = done if isinstance(done, dict) else {}
    out.write("disconnected from GitHub"
              + ("" if done.get("github_revoked") is not False
                 else " (GitHub did not confirm the revocation: remove SwarmCloud under your GitHub "
                    "Settings › Applications to be sure)")
              + (f"; grants deleted: {done['grants_deleted']}" if "grants_deleted" in done else "")
              + "\n")
    return EXIT_OK


# --------------------------------------------------------------------------
# One task's diagnosis (`sc debug`)
# --------------------------------------------------------------------------

#: How many newest events, and how many lines of the newest attempt's agent
#: log, `sc debug` shows. Bounded so one command's answer fits on a screen and
#: in a tool reply; the whole log stays readable with `swarm tail`.
DEBUG_EVENTS = 10
DEBUG_LOG_LINES = 40

#: How far back from the end of the log the tail read starts. A log line is
#: rarely longer than a few hundred bytes; this holds DEBUG_LOG_LINES of them
#: with room, without reading a whole log to show its last lines.
_DEBUG_TAIL_BYTES = 16_000

#: The agent's own streams first, then the runner's, for a runner with no
#: agent CLI (the logs route answers `not_applicable` for those).
_DEBUG_STREAMS = ("agent_stderr", "agent_stdout", "stderr", "stdout")

_WITHHELD = "withheld: the API did not say it masked this"


def _section(value: Any = None, error: Exception | str | None = None) -> dict[str, Any]:
    if error is not None:
        return {"value": None, "not_read_because": _unread_reason(error)}
    return {"value": value}


def _unread_reason(error: Exception | str) -> str:
    """Why a section was not read, WITHOUT the body of a response (#453).

    A `SwarmError` raised for an HTTP answer quotes the API's or the edge's
    error body in its message (`client._explain`), and that body comes with
    no redaction count -- so, by `_shown`'s rule, it is withheld, and the
    section says what the response WAS from the fields the client carries as
    data: its status, whether Google's edge sent it, and the API's error
    code. A reason with no status is a sentence the bridge wrote itself (a
    transport failure, a log with no stream), and is printed.
    """
    status = getattr(error, "status", None)
    if not isinstance(error, SwarmError) or status is None:
        return str(error)
    who = "Google's edge" if error.edge else "the API"
    code = f" (code {error.code})" if error.code else ""
    return f"{who} answered HTTP {status}{code}; its body is {_WITHHELD}"


def _shown(text: Any, count: Any) -> Any:
    """A free-text string the API served, ONLY if the API counted its masking.

    Every free-text field the API serves comes with a `*_redaction_count`
    beside it -- `last_error`, an attempt's `error`, an event's `detail` --
    and a deployment that did not mask it sends no count. The bridge has no
    masker of its own (`patches.masked_counts` reports the API's counts), so
    a string the API did not say it masked is WITHHELD rather than printed:
    printing it would trust that a deployment old enough not to count also
    masked, which is exactly the deployment that did not.
    """
    if text is None:
        return None
    if isinstance(count, int) and not isinstance(count, bool):
        return text
    return _WITHHELD


def _log_tail(client: SwarmClient, task_id: str, attempt_id: str | None, lines: int) -> dict[str, Any]:
    """The last `lines` lines of the newest attempt's log, through the logs route.

    `attempt_id` None asks the route for its own choice, the newest attempt --
    what is left when the attempts could not be listed, so a failed attempts
    read does not also cost the log.

    Two reads per stream: one to learn the object's size, one window ending
    at it. The route masks every window at read time and says so in
    `redaction.applied_at_read_time`; a window without that statement is
    withheld. Streams are tried agent-first; one that is absent or not
    applicable is skipped, one that is unreadable is reported.
    """
    problems: list[str] = []
    for stream in _DEBUG_STREAMS:
        probe = client.logs(task_id, attempt_id=attempt_id, stream=stream, limit_bytes=1)
        entry = next((s for s in probe.get("streams") or [] if isinstance(s, dict)), None)
        if entry is None or entry.get("status") != "ok":
            status = entry.get("status") if entry else "absent"
            if status == "unreadable":
                # The route's `detail` is printed only under its count, like
                # every other free-text field it serves (`_shown`, #453).
                detail = _shown(entry.get("detail"), entry.get("detail_redaction_count"))
                problems.append(f"{stream}: unreadable" + (f": {detail}" if detail else ""))
            continue
        total = entry.get("total_bytes")
        offset = max(0, int(total) - _DEBUG_TAIL_BYTES) if isinstance(total, int) else 0
        window = client.logs(task_id, attempt_id=attempt_id, stream=stream, offset=offset)
        entry = next((s for s in window.get("streams") or [] if isinstance(s, dict)), {})
        attempt_id = window.get("attempt_id") or attempt_id
        statement = window.get("redaction") if isinstance(window.get("redaction"), dict) else {}
        if statement.get("applied_at_read_time") is not True:
            return {"stream": stream, "attempt_id": attempt_id, "lines": [], "withheld": _WITHHELD,
                    "masked": None}
        text = entry.get("content") or ""
        kept = text.splitlines()
        if offset > 0 and kept:
            kept = kept[1:]  # the first line of a window that starts mid-log is partial
        return {
            "stream": stream,
            "attempt_id": attempt_id,
            "lines": kept[-lines:] if lines > 0 else [],
            "total_bytes": total,
            "masked": entry.get("redaction_count"),
        }
    reason = "; ".join(problems) or "the attempt has no log on any stream yet"
    raise SwarmError(reason)


def debug_report(
    client: SwarmClient, task_id: str, *, events: int = DEBUG_EVENTS, log_lines: int = DEBUG_LOG_LINES
) -> dict[str, Any]:
    """One task's diagnosis from the API's own reads, and nothing else.

    `SwarmClient.task`, `attempts`, `events_page` and `logs` -- every one
    served masked by the API. Never GCS, never Cloud Logging: a direct read
    would skip the tenant check and the read-time masking those routes apply.
    EACH READ IS ITS OWN SECTION, so a failed one is printed as not read with
    its reason and the others still print: a diagnosis that died on its
    first failing read would fail exactly when one is needed.
    """
    report: dict[str, Any] = {"task_id": task_id}
    task, failure = _attempt(lambda: client.task(task_id))
    if task is None:
        report["task"] = _section(error=failure)
    else:
        # `console`: the API's link to this task, as served; no key when the
        # API served none (`follow.console_link`).
        report["task"] = _section(with_console({
            "state": task.get("state"),
            "runner_profile": task.get("runner_profile"),
            "last_error": _shown(task.get("last_error"), task.get("last_error_redaction_count")),
            "end_cause": task.get("end_cause"),
        }, task))

    attempts, failure = _attempt(lambda: client.attempts(task_id))
    if attempts is None:
        report["attempts"] = _section(error=failure)
    else:
        report["attempts"] = _section([
            {
                "attempt_id": a.get("attempt_id"),
                "generation": a.get("generation"),
                "started_at": a.get("started_at"),
                "completed_at": a.get("completed_at"),
                # NULL IS NOT RECORDED, never 0 -- see `swarm_result`.
                "exit_code": a.get("exit_code"),
                "error": _shown(a.get("error"), a.get("error_redaction_count")),
            }
            for a in attempts
        ])

    page, failure = _attempt(lambda: client.events_page(task_id, limit=max(1, events), newest_first=True))
    if page is None:
        report["events"] = _section(error=failure)
    else:
        rows, _, paged = page
        report["events"] = _section([
            {
                "type": e.get("type"),
                "at": e.get("at"),
                "attempt_id": e.get("attempt_id"),
                "detail": _shown(e.get("detail"), e.get("detail_redaction_count")),
            }
            for e in rows[: max(1, events)]
        ])
        if not paged:
            report["events"]["note"] = (
                "this API does not page events, so these are its OLDEST, not its newest"
            )

    newest = next((a for a in attempts or [] if a.get("attempt_id")), None)
    if attempts is not None and newest is None:
        report["log_tail"] = _section(error="the task has no attempt yet, so it has no log")
    else:
        # The attempts list is newest first; unread, the route picks the newest.
        chosen = str(newest["attempt_id"]) if newest is not None else None
        tail, failure = _attempt(lambda: _log_tail(client, task_id, chosen, log_lines))
        report["log_tail"] = _section(tail, failure) if tail is None else _section(tail)

    # WHAT WAS MASKED, from the API's own counts: the task's input and
    # metadata (`masked_counts`, as every other surface prints them), and each
    # section's. None is "this deployment sent no count", never 0.
    def _count(rows: Any, key: str) -> int | None:
        known = [r.get(key) for r in rows or [] if isinstance(r.get(key), int)]
        return sum(known) if known else None

    report["masked"] = {
        **(masked_counts(task) if task is not None else {"input": None, "metadata": None}),
        "last_error": task.get("last_error_redaction_count") if task is not None else None,
        "attempt_errors": _count(attempts, "error_redaction_count"),
        "event_details": _count(page[0] if page is not None else None, "detail_redaction_count"),
        "log_tail": (report["log_tail"]["value"] or {}).get("masked"),
    }
    return report


def _debug_lines(report: dict[str, Any]) -> list[str]:
    def problem(section: dict[str, Any]) -> str:
        return f"  not read: {section['not_read_because']}"

    lines = [f"task {report['task_id']}"]
    task = report["task"]
    if task["value"] is None:
        lines.append(problem(task))
    else:
        t = task["value"]
        lines.append(f"  state       {t['state']}")
        lines.append(f"  profile     {t['runner_profile']}")
        lines.append(f"  last error  {t['last_error'] if t['last_error'] is not None else '(none)'}")
        if t.get("end_cause"):
            lines.append(f"  end cause   {t['end_cause']}")
        if t.get("console"):
            lines.append(f"  console: {t['console']}")
    masked = report["masked"]
    lines.append(
        "  masked      "
        + "  ".join(f"{k.replace('_', ' ')} {'—' if v is None else v}" for k, v in masked.items())
    )
    lines.append("attempts")
    attempts = report["attempts"]
    if attempts["value"] is None:
        lines.append(problem(attempts))
    elif not attempts["value"]:
        lines.append("  none yet")
    for a in attempts["value"] or []:
        exit_text = "exit not recorded" if a["exit_code"] is None else f"exit {a['exit_code']}"
        lines.append(
            f"  {a['attempt_id']}  generation {a['generation']}  "
            f"{a['started_at'] or '—'} → {a['completed_at'] or '—'}  {exit_text}"
        )
        if a["error"]:
            lines.append(f"    error: {a['error']}")
    lines.append("events (newest first)")
    events = report["events"]
    if events["value"] is None:
        lines.append(problem(events))
    for e in events["value"] or []:
        detail = e["detail"] if isinstance(e["detail"], str) else json.dumps(e["detail"], default=str)
        lines.append(f"  {e['at']}  {e['type']}  {detail}")
    if events.get("note"):
        lines.append(f"  note: {events['note']}")
    tail = report["log_tail"]
    if tail["value"] is None:
        lines.append("log tail")
        lines.append(problem(tail))
    else:
        v = tail["value"]
        lines.append(f"log tail ({v['stream']}, attempt {v['attempt_id']}, last {len(v['lines'])} lines)")
        if v.get("withheld"):
            lines.append(f"  {v['withheld']}")
        lines.extend(f"  {line}" for line in v["lines"])
    return lines


def cmd_debug(client: SwarmClient, args, out) -> int:
    report = debug_report(client, args.task_id, events=args.events, log_lines=args.log_lines)
    unread = any(
        isinstance(report.get(key), dict) and report[key].get("value") is None
        for key in ("task", "attempts", "events", "log_tail")
    )
    if args.json:
        out.write(json.dumps(report, indent=2, default=str) + "\n")
    else:
        out.write("\n".join(_debug_lines(report)) + "\n")
    return EXIT_FAIL if unread else EXIT_OK


# --------------------------------------------------------------------------
# Which deployment, and who you are on it
# --------------------------------------------------------------------------


def _resolve(args) -> Any:
    """The deployment this command is about, or a SwarmError saying how to add one."""
    from . import config

    deployment = config.resolve(context=getattr(args, "context", None))
    if deployment is None:
        add = terminal_command(
            "sc context add <name> --url https://<your deployment> --client-id <Desktop OAuth client id>"
        )
        raise SwarmError(
            f"no SwarmCloud deployment is configured on this machine. Add one: `{add}` "
            "-- or install the sc plugin, which asks for both"
        )
    return deployment


def _read_secret_line() -> str:
    """One line from stdin: the only way a secret enters this CLI.

    Never an argument -- argv is what `ps` and shell history keep.
    """
    return (sys.stdin.readline() if sys.stdin is not None else "").strip()


def _outranked(detection: Any, email: str) -> str:
    """The warning for a machine where something ranks above the sign-in.

    `auth.detect` puts SWARM_ID_TOKEN, the metadata server and
    SWARM_IMPERSONATE_SA ahead of a sign-in, on purpose: CI sets them and
    means them. But a developer who has one set and has just signed in will
    otherwise believe every tool now acts as them, and nothing says it does
    not until they happen to run `sc whoami`.
    """
    from . import auth

    tier = detection.tier
    if tier is auth.Tier.EXPLICIT:
        who, remedy = "the token in SWARM_ID_TOKEN", "unset SWARM_ID_TOKEN"
    elif tier is auth.Tier.METADATA:
        who = "this machine's own service account (the GCP metadata server)"
        remedy = "run `sc` from a machine outside GCP"
    elif tier in (auth.Tier.IMPERSONATE, auth.Tier.IAP):
        who = f"the service account {os.environ.get('SWARM_IMPERSONATE_SA', '').strip()}"
        remedy = "unset SWARM_IMPERSONATE_SA"
    else:
        # Through `terminal_command`, like `sc whoami` below: typed bare, this
        # was `command not found` on a plugin-only install (review of #201).
        who, remedy = f"the {tier.value} tier", f"see `{terminal_command('swarm doctor')}`"
    return (
        f"warning     {detection.detail}, which ranks above a sign-in: the plugin's "
        f"tools and every other `sc` and `swarm` command on this machine will act as "
        f"{who}, not as you ({email}). To act as yourself, {remedy}; "
        f"`{terminal_command('sc whoami')}` shows which identity is in use.\n"
    )


def cmd_login(_client, args, out) -> int:
    from . import auth, credentials, signin

    deployment = _resolve(args)
    store = credentials.store()
    # `--pkce-only` is GATE 0b's Q1 probe (plan §6.3): the Desktop client's
    # sign-in with PKCE and NO client secret -- none read, asked for or sent.
    # `python -m swarm_mcp.signin --pkce-only` runs the same `login`.
    pkce_only = bool(getattr(args, "pkce_only", False))
    wants_secret = getattr(args, "client_secret_stdin", False) and not pkce_only
    secret = _read_secret_line() if wants_secret else None
    claims = signin.login(
        deployment,
        store=store,
        client_secret=secret or None,
        notify=lambda line: print(line, file=sys.stderr),
        pkce_only=pkce_only,
    )
    email = claims.get("email") or "(the token carried no email)"
    out.write(f"signed in to {deployment.context} ({deployment.url}) as {email}\n")
    out.write(f"your sign-in is kept in {store.describe()}\n")
    detection = auth.detect(deployment)
    if detection.tier is not auth.Tier.SIGNED_IN:
        out.write(_outranked(detection, email))
    # ONE CALL NOW, so the developer learns at sign-in -- not at their first
    # dispatch -- whether this deployment admits the token. A 401 here is the
    # deployment not having allowlisted its own Desktop client, and the
    # message SwarmClient raises says so.
    #
    # WITH THE SIGN-IN IT IS CHECKING, whatever `detect` would pick. Left to
    # detect, a machine with SWARM_IMPERSONATE_SA, SWARM_ID_TOKEN or a
    # metadata server sent THAT credential, and "signed in" was then printed
    # over the service account's tenant with exit 0 -- even where IAP had not
    # allowlisted the Desktop client, which is the one thing this call exists
    # to find out (review of PR #61, 2026-09-25).
    chosen, chosen_by = tenant_choice()
    try:
        with SwarmClient(deployment=deployment, tier=auth.Tier.SIGNED_IN, tenant=chosen) as api:
            me = api.request("GET", "/v1/tenants/me") or {}
    except SwarmError as exc:
        out.write(f"but {deployment.url} did not accept it: {exc}\n")
        return EXIT_FAIL
    # `tenant_of`: the route nests the tenant, and the flat read printed
    # "(not reported)" for one it had just served (#88, SC-F3).
    out.write(
        f"tenant      {render.tenant_of(me) or '(not reported)'}  ({chosen_by})\n"
    )
    return EXIT_OK


#: Said when no SWARM_TENANT is set and `GET /v1/tenants/mine` did not say
#: which default applied (an API older than PROFILE-DEFAULT, or the listing
#: could not be read): the API picks, by the rule of
#: docs/design/user-profile.md §4.5.
_DEFAULT_TENANT_SOURCE = (
    "SWARM_TENANT is not set, so the API chose your default: "
    "the one in Settings › Profile, else your personal workspace"
)

#: What each `default_source` the API reports on `tenants/mine`'s default
#: entry is said as when no SWARM_TENANT is set (user-profile.md §4.5).
#: "unavailable" is not a default at all: a headerless request is then 503
#: `preferences_unavailable`, so it says to name a workspace.
_DEFAULT_SOURCES = {
    "profile": "SWARM_TENANT is not set, so the API used your default, from Settings › Profile",
    "personal": (
        "SWARM_TENANT is not set, so the API chose your default: your personal "
        "workspace (no default is set in Settings › Profile)"
    ),
    "first_group": (
        "SWARM_TENANT is not set, so the API chose your default: your first "
        "group (no default is set in Settings › Profile)"
    ),
    "unavailable": (
        "SWARM_TENANT is not set and your preferences could not be read; name a "
        "workspace: set SWARM_TENANT or the plugin's tenant option"
    ),
}


def default_source(client: SwarmClient) -> str | None:
    """The `default_source` of `GET /v1/tenants/mine`'s default entry, or None.

    None when the listing could not be read or names none -- an API older
    than PROFILE-DEFAULT -- and the caller then says the general rule
    (`_DEFAULT_TENANT_SOURCE`) rather than guessing which part of it applied.
    """
    return _default_source_of(_tenants_mine_or_none(client))


def _tenants_mine_or_none(client: SwarmClient) -> list[dict[str, Any]] | None:
    """`GET /v1/tenants/mine`, or None when it could not be read."""
    try:
        return client.tenants_mine()
    except SwarmError:
        return None


def _default_source_of(mine: list[dict[str, Any]] | None) -> str | None:
    for entry in mine or []:
        if entry.get("is_default") and isinstance(entry.get("default_source"), str):
            return entry["default_source"]
    return None


def _via_of(mine: list[dict[str, Any]] | None, tenant: str | None) -> str | None:
    """How the caller reaches `tenant`, from its `tenants/mine` entry
    (workspace-roles.md §4.3): `member`, `personal` or `platform_admin`.
    None when the listing was not read or names none, never a guess."""
    for entry in mine or []:
        if tenant and entry.get("tenant_id") == tenant and isinstance(entry.get("via"), str):
            return entry["via"]
    return None


def tenant_choice() -> tuple[str | None, str]:
    """`(tenant, which setting chose it)` for this invocation.

    THROUGH `chosen_tenant`, the bridge's and `swarm`'s own helper, with no
    flag: the rule (blank is no choice) lives once. The plugin's `tenant`
    option reaches this process as SWARM_TENANT too, so that one name is
    the whole answer to "which setting".
    """
    tenant = chosen_tenant(None)
    if tenant is None:
        return None, _DEFAULT_TENANT_SOURCE
    return tenant, "chosen by SWARM_TENANT"


def cmd_logout(_client, args, out) -> int:
    from . import signin

    deployment = _resolve(args)
    if signin.logout(deployment):
        out.write(
            f"signed out of {deployment.context}; the refresh token was revoked and forgotten\n"
        )
    else:
        out.write(f"not signed in to {deployment.context}; nothing to do\n")
    return EXIT_OK


def read_whoami(args) -> tuple[dict[str, Any], int]:
    """What `sc whoami` reads, and its exit code -- read once, here, for
    `sc whoami` and `sc config` alike, so the two cannot disagree about which
    deployment this is or whose tenant it answered for."""
    from . import auth, credentials, signin

    chosen, chosen_by = tenant_choice()
    shown: dict[str, Any] = {
        "context": None, "url": None, "source": None, "current": None,
        "tier": None, "credential_store": None, "principal": None,
        "tenant": None, "tenant_chosen": chosen, "tenant_source": chosen_by,
        "default_source": None, "role": None, "role_source": None, "via": None,
        "features": features_of(None), "error": None,
    }
    code = EXIT_OK
    try:
        deployment = _resolve(args)
        shown.update(
            context=deployment.context, url=deployment.url,
            source=deployment.source, current=deployment.current,
        )
        tier = auth.detect(deployment).tier
        shown["tier"] = tier.value
        if tier is auth.Tier.SIGNED_IN:
            shown["credential_store"] = credentials.store().describe()
            shown["principal"] = signin.SignedIn(deployment).claims().get("email")
        with SwarmClient(deployment=deployment, tenant=chosen) as api:
            me = api.request("GET", "/v1/tenants/me") or {}
            # One listing read, for which default applied and for how this
            # caller reaches the workspace (`via`, workspace-roles.md §4.8).
            mine = _tenants_mine_or_none(api)
            if chosen is None:
                # WHICH DEFAULT APPLIED (PROFILE-SC): said only when nothing was
                # chosen, because a SWARM_TENANT wins whatever the default is.
                source = _default_source_of(mine)
                shown["default_source"] = source
                shown["tenant_source"] = _DEFAULT_SOURCES.get(source or "", chosen_by)
        # Nested under `tenant` and `principal`, which the flat reads missed
        # (#88, SC-F3) -- through the one reader `render` keeps for it.
        shown["tenant"] = render.tenant_of(me)
        shown["principal"] = shown["principal"] or render.principal_of(me).get("email")
        # THE STORED ROLE, as `/v1/tenants/me` sent it; absent from an API older
        # than roles, or when the API could not read it, and then not guessed.
        role = me.get("role") if isinstance(me, dict) else None
        if isinstance(role, dict):
            shown["role"], shown["role_source"] = role.get("name"), role.get("source")
        shown["via"] = _via_of(mine, shown["tenant"])
        shown["features"] = features_of(me)
    except SwarmError as exc:
        shown["error"] = str(exc)
        code = EXIT_FAIL
    return shown, code


#: The features `/v1/tenants/me` reports for the workspace (FEATURES-READ,
#: part of #1748), in the order `sc whoami` prints them.
FEATURES = ("caller_mcp", "await_input", "service_answer")

#: What a feature the API did not report as `true` or `false` reads as: its
#: "unknown", or an API older than the field. Never "off" -- a caller that
#: reads "off" withdraws the feature, the misreading #1748 was filed over.
NOT_REPORTED = "not reported"


def features_of(me: Any) -> dict[str, str]:
    """`{feature: "on" | "off" | "not reported"}` from a `/v1/tenants/me` answer.

    Only a boolean is a state; anything else, `"unknown"` included, is "not
    reported" rather than a guess."""
    served = me.get("features") if isinstance(me, dict) else None
    served = served if isinstance(served, dict) else {}
    shown = {}
    for name in FEATURES:
        value = served.get(name)
        shown[name] = NOT_REPORTED if not isinstance(value, bool) else ("on" if value else "off")
    return shown


def cmd_whoami(_client, args, out) -> int:
    """Context, URL, where that came from, the tier, the principal, the tenant,
    and the workspace's three features (`features_of`).

    Every field is filled from a READ: the principal from the signed-in ID
    token (or from the API, on a tier without one), the tenant from
    `/v1/tenants/me`. A field that could not be read is left out and the
    reason printed, because a blank reads as "none".
    """
    shown, code = read_whoami(args)
    # The plugin/deployment version skew (plan §6.5), read beside the rest
    # and never deciding the exit code: whoami answers who, doctor whether.
    versions = None
    if shown["url"]:
        try:
            versions = version_check(_resolve(args))
        except SwarmError as exc:
            versions = {"verdict": f"not checked: {exc}", "status": "unknown", "compatible": None}
    if getattr(args, "json", False):
        out.write(json.dumps({**shown, "versions": versions}, indent=2) + "\n")
        return code
    labels = (
        ("context", "context"), ("url", "url"), ("source", "from"), ("tier", "tier"),
        ("credential_store", "sign-in in"), ("principal", "principal"), ("tenant", "tenant"),
    )
    for field, label in labels:
        if shown[field] is None:
            continue
        value = shown[field]
        if field == "context" and shown["current"]:
            value = f"{value}  (current)"
        if field == "tenant":
            value = f"{value}  ({shown['tenant_source']})"
        out.write(f"{label:<11} {value}\n")
        if field == "tenant" and shown.get("role"):
            role = f"{shown['role']}  (source {shown.get('role_source') or '-'}"
            role += f", via {shown['via']})" if shown.get("via") else ")"
            out.write(f"{'role':<11} {role}\n")
        if field == "tenant":
            states = ", ".join(f"{name} {state}" for name, state in shown["features"].items())
            out.write(f"{'features':<11} {states}\n")
    if versions is not None:
        out.write(version_text(versions))
    if shown["error"]:
        out.write(f"{'problem':<11} {shown['error']}\n")
    return code


def cmd_doctor(_client, args, out) -> int:
    """Whether this plugin and the deployment it talks to agree on version
    (plan §6.5): both versions, the range each accepts, and, when they do not
    agree, which of the two to upgrade and the command that does it.

    Exit 0 when they agree; 3 when they do not, or when the deployment's
    version could not be read -- an unread version is never "fine".
    """
    deployment = _resolve(args)
    versions = version_check(deployment)
    if getattr(args, "json", False):
        shown = {"context": deployment.context, "url": deployment.url, "versions": versions}
        out.write(json.dumps(shown, indent=2) + "\n")
    else:
        out.write(f"{'context':<11} {deployment.context}\n")
        out.write(f"{'url':<11} {deployment.url}\n")
        out.write(version_text(versions))
    return EXIT_OK if versions["compatible"] is True else EXIT_TROUBLE


def version_check(deployment: Any) -> dict[str, Any]:
    """The plugin's version against the deployment's, in plain words (plan §6.5).

    `compatible` is True, False, or None when the deployment's discovery
    document could not be read (never a guessed version). `upgrade` names the
    side to move -- "plugin" or "deployment" -- and `verdict` says so, with
    both versions and the command. `status` is `discovery.Checker.status()`.
    """
    from . import discovery

    checker = discovery.Checker(deployment.url, required=False)
    plugin, span = checker.plugin, checker.span
    shown: dict[str, Any] = {
        "plugin": plugin, "deployment": None, "compatible": None, "upgrade": None,
        "status": checker.status(), "verdict": "",
    }
    try:
        document = checker.cache.get(deployment.url)
    except discovery.DiscoveryUnreachable as exc:
        shown["verdict"] = f"not checked: the deployment's version could not be read ({exc})"
        return shown
    version = document.version
    shown["deployment"] = version
    supports = f"plugin {plugin} supports deployments {discovery._range_text(span)}" if span else ""
    if document.plugin_min_version and discovery.older(plugin, document.plugin_min_version):
        shown.update(compatible=False, upgrade="plugin", verdict=(
            f"no: plugin {plugin} is older than deployment {version} accepts "
            f"(it needs plugin {document.plugin_min_version} or later). "
            f"Upgrade the plugin: {discovery.UPDATE_PLUGIN}"
        ))
    elif span is not None and span.min and discovery.older(version, span.min):
        shown.update(compatible=False, upgrade="deployment", verdict=(
            f"no: deployment {version} is older than {supports}. Upgrade the deployment: "
            f"ask your admin to run `swarm upgrade --to {plugin}`, or pin the plugin to "
            "your deployment: claude plugin marketplace add "
            + discovery.RELEASE_MARKETPLACE.format(version=version)
        ))
    elif span is not None and span.max_exclusive and not discovery.older(version, span.max_exclusive):
        shown.update(compatible=False, upgrade="plugin", verdict=(
            f"no: deployment {version} is newer than {supports}. "
            f"Upgrade the plugin: {discovery.UPDATE_PLUGIN}"
        ))
    else:
        unranged = "" if span else (
            "; this plugin publishes no supported range, so only the deployment's "
            "oldest accepted plugin was checked"
        )
        shown.update(compatible=True, verdict=(
            f"yes: plugin {plugin} works with deployment {version}{unranged}"
        ))
    return shown


def version_text(versions: dict[str, Any]) -> str:
    """The two lines `sc doctor` and `sc whoami` print for `version_check`."""
    return f"{'versions':<11} {versions['status']}\n{'compatible':<11} {versions['verdict']}\n"


def plugin_manifest() -> tuple[Any, str | None]:
    """The sc plugin's `plugin.json`: `(path, None)`, or `(None, why not)`.

    READ FROM THE FILE, never restated: the version a person runs is the one
    in the manifest Claude Code loaded. Claude Code hands a plugin's processes
    CLAUDE_PLUGIN_ROOT; a terminal in a checkout has `plugin/` beside the
    bridge (`client._repo_root`). A bridge installed from git has neither,
    and that is said, not guessed.
    """
    from pathlib import Path

    from .client import _repo_root

    looked: list[str] = []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    candidates = [Path(root) / ".claude-plugin" / "plugin.json"] if root else []
    candidates.append(Path(_repo_root()) / "plugin" / ".claude-plugin" / "plugin.json")
    for candidate in candidates:
        looked.append(str(candidate))
        if candidate.is_file():
            return candidate, None
    return None, "no plugin manifest found (looked at " + ", ".join(looked) + ")"


def _plugin_version() -> dict[str, Any]:
    path, why = plugin_manifest()
    if path is None:
        return {"value": None, "not_read_because": why}
    try:
        version = json.loads(path.read_text()).get("version")
    except (OSError, ValueError, AttributeError) as exc:
        return {"value": None, "not_read_because": f"{path} could not be read: {exc}"}
    if not isinstance(version, str) or not version.strip():
        return {"value": None, "not_read_because": f"{path} carries no version"}
    return {"value": version, "path": str(path)}


def _bridge_version() -> dict[str, Any]:
    from importlib import metadata

    try:
        return {"value": metadata.version("swarm-mcp")}
    except metadata.PackageNotFoundError as exc:
        return {"value": None, "not_read_because": f"swarm-mcp is not installed as a package: {exc}"}


def cmd_config(_client, args, out) -> int:
    """One read-only view: endpoint, tenant, session target, and both versions.

    BUILT OVER `sc whoami` and `sc context` rather than beside them: the
    endpoint and tenant are `read_whoami`'s, the target is
    `config.session_target`'s -- the reads the MCP server makes too -- and
    the versions are read from their files. Every field is filled or says
    why not, never left blank: a blank tenant reads as "no tenant".

    NO SECRET IS PRINTED. Nothing here reads the client secret or a token
    into a field; `read_whoami` carries the store's DESCRIPTION, which this
    view does not show either.
    """
    from . import config

    shown, code = read_whoami(args)
    reason = shown.get("error") or "not reported"

    def field(value: Any, **beside: Any) -> dict[str, Any]:
        if value is None:
            return {"value": None, "not_read_because": reason}
        return {"value": value, **beside}

    try:
        target = config.session_target(override=getattr(args, "target", None))
        target_field: dict[str, Any] = {"value": target.value, "source": target.source}
    except SwarmError as exc:
        target_field, code = {"value": None, "not_read_because": str(exc)}, EXIT_FAIL
    view = {
        "endpoint": field(shown.get("url"), context=shown.get("context"), source=shown.get("source")),
        "tenant": field(shown.get("tenant"), source=shown.get("tenant_source")),
        "target": target_field,
        "plugin_version": _plugin_version(),
        "bridge_version": _bridge_version(),
    }
    if getattr(args, "json", False):
        out.write(json.dumps(view, indent=2) + "\n")
        return code

    def text(entry: dict[str, Any], beside: str = "") -> str:
        if entry.get("value") is None:
            return f"not read: {entry.get('not_read_because')}"
        return f"{entry['value']}{beside}"

    endpoint = view["endpoint"]
    rows = (
        ("endpoint", text(endpoint, f"  (context {endpoint.get('context')}, from {endpoint.get('source')})")),
        ("tenant", text(view["tenant"], f"  ({view['tenant'].get('source')})")),
        ("target", text(target_field, f"  (from {target_field.get('source')})")),
        ("plugin", text(view["plugin_version"], f"  ({view['plugin_version'].get('path')})")),
        ("bridge", text(view["bridge_version"])),
    )
    for label, value in rows:
        out.write(f"{label:<11} {value}\n")
    return code


def cmd_context_add(_client, args, out) -> int:
    from . import config, credentials

    context, created = config.add_context(args.name, args.url, client_id=args.client_id)
    if args.client_secret_stdin:
        secret = _read_secret_line()
        if not secret:
            raise SwarmError("--client-secret-stdin was given and stdin held no secret")
        store = credentials.store()
        store.set(credentials.key(context.name, credentials.CLIENT_SECRET), secret)
        out.write(f"client secret kept in {store.describe()}\n")
    if args.use:
        config.use_context(context.name)
    current = config.load().current == context.name
    out.write(
        f"{'added' if created else 'updated'} context {context.name} -> {context.url}"
        + ("  (current)" if current else "")
        + "\n"
    )
    if context.oauth_client_id:
        login = "sc login" if current else f"sc login --context {context.name}"
        out.write(f"next: `{terminal_command(login)}`\n")
    return EXIT_OK


def cmd_context_use(_client, args, out) -> int:
    from . import config

    context = config.use_context(args.name)
    out.write(f"current context: {context.name} -> {context.url}\n")
    return EXIT_OK


def cmd_context_list(_client, args, out) -> int:
    from . import config, credentials

    loaded = config.load()
    try:
        store = credentials.store()
    except SwarmError:
        store = None

    def _signed_in(name: str) -> bool | None:
        """WHETHER, never with what: the value read here is not kept or shown."""
        if store is None:
            return None
        try:
            return bool(store.get(credentials.key(name, credentials.REFRESH_TOKEN)))
        except SwarmError:
            return None

    rows = [
        {
            "name": c.name,
            "url": c.url,
            "oauth_client_id": c.oauth_client_id or None,
            "current": c.name == loaded.current,
            "signed_in": _signed_in(c.name) if c.oauth_client_id else False,
        }
        for c in sorted(loaded.contexts.values(), key=lambda c: c.name)
    ]
    if getattr(args, "json", False):
        out.write(json.dumps({"path": str(loaded.path), "contexts": rows}, indent=2) + "\n")
        return EXIT_OK
    if not rows:
        add = terminal_command("sc context add <name> --url https://<deployment>")
        out.write(f"no contexts in {loaded.path}. Add one: `{add}`\n")
        return EXIT_OK
    width = max(len(r["name"]) for r in rows)
    out.write(f"  {'NAME':<{width}}  {'SIGNED IN':<9}  URL\n")
    for r in rows:
        signed = {True: "yes", False: "no", None: "?"}[r["signed_in"]]
        if not r["oauth_client_id"]:
            signed = "-"
        mark = "*" if r["current"] else " "
        out.write(f"{mark} {r['name']:<{width}}  {signed:<9}  {r['url']}\n")
    return EXIT_OK


def cmd_context_remove(_client, args, out) -> int:
    from . import config

    removed = config.remove_context(args.name)
    out.write(
        f"removed context {removed.name} ({removed.url}) and the credentials kept for it\n"
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Issue runs (#454): `sc run --issue`, `sc runs`, `sc run show`, `sc plan ...`
# --------------------------------------------------------------------------
#
# THE SECOND PART OF `sc` THAT WRITES, after `sc account ...`: `sc run --issue`
# creates a run (one planner task), and `sc plan approve|edit|reject` moves
# its plan. Like the account verbs they are held out of every skill's grant by
# `test_plugin_commands.py`; `sc runs`, `sc run show` and `sc plan show` read.
#
# APPROVAL SENDS THE DIGEST OF THE PLAN IT PRINTED. `sc plan approve` reads the
# run, prints the plan and its digest, and asks for `approve` typed back; the
# digest sent is the one printed, so an edit in between is refused with
# `plan_changed` rather than approved unseen. `--digest` skips the prompt for
# a plan already shown (by `sc plan show`), and is then the evidence itself.


def _run_style(args, out) -> Style:
    return style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)


def _print_run(client: SwarmClient, run: dict[str, Any], args, out, *, plan: bool) -> None:
    from . import runs

    if args.json:
        out.write(json.dumps(runs.summary(client, run), indent=2, default=str) + "\n")
        return
    style = _run_style(args, out)
    lines = runs.run_lines(run, style)
    if plan:
        lines += [""] + runs.plan_lines(run, style)
    report, failure = _attempt(lambda: runs.step_report(client, run))
    if failure is not None:
        lines += ["", f"  steps of {runs.active_workflow_id(run)} could not be read: {failure}"]
    elif report:
        lines += [""] + runs.step_lines(report, style)
    _emit(lines, out)


def _run_exit(run: dict[str, Any]) -> int:
    """0 unless the run ENDED some way other than DONE."""
    if run.get("terminal") and run.get("state") != "DONE":
        return EXIT_FAIL
    return EXIT_OK


def cmd_run(client: SwarmClient, args, out) -> int:
    """`sc run --issue owner/repo#N`: plan the issue, and follow it with `--follow`."""
    from . import runs

    if not args.issue:
        raise SwarmError(
            f"sc run needs --issue owner/repo#N (or the issue's URL); "
            f"`{terminal_command('sc run show <run>')}` reads one run. Nothing was created"
        )
    if args.auto_merge:
        # Sent anyway: the API is the authority, and it says why it refuses.
        sys.stderr.write(
            "sc: --auto-merge is visible but disabled until #295; the API decides, and "
            "refuses it today\n"
        )
    run = client.create_run(
        issue=args.issue,
        plan_approval=args.plan_approval,
        auto_merge=args.auto_merge,
        fix_rounds=args.fix_rounds,
    )
    if args.follow and not args.json:
        style = _run_style(args, out)
        out.write(f"created run {run.get('id')} for {args.issue}: one planner task, no capacity "
                  "held by the plan until it is approved\n")
        run = runs.follow(client, str(run["id"]), out, style, interval=args.interval)
        _emit([""] + runs.run_lines(run, style), out)
        return _run_exit(run)
    _print_run(client, run, args, out, plan=False)
    return EXIT_OK


def cmd_run_show(client: SwarmClient, args, out) -> int:
    run = client.run(args.run_id)
    _print_run(client, run, args, out, plan=True)
    return _run_exit(run)


def cmd_runs(client: SwarmClient, args, out) -> int:
    from . import runs

    listing = client.runs(limit=args.limit, page_token=args.page_token)
    if args.json:
        out.write(json.dumps(listing, indent=2, default=str) + "\n")
        return EXIT_OK
    _emit(runs.runs_lines(listing, _run_style(args, out)), out)
    return EXIT_OK


def cmd_plan_show(client: SwarmClient, args, out) -> int:
    from . import runs

    run = client.run(args.run_id)
    if args.json:
        out.write(json.dumps({"run_id": run.get("id"), "state": run.get("state"),
                              "plan": run.get("plan"), "plan_digest": run.get("plan_digest")},
                             indent=2, default=str) + "\n")
        return EXIT_OK
    style = _run_style(args, out)
    lines = runs.plan_lines(run, style)
    if runs.waits_for_a_person(run):
        approve = f"sc plan approve {run.get('id')} --digest {run.get('plan_digest')}"
        lines.append(f"  approve this plan: {terminal_command(approve)}")
    _emit(lines, out)
    return EXIT_OK


def _shown_digest(client: SwarmClient, args, out, verb: str) -> str:
    """The digest of the plan this command PRINTS, after it is typed `verb` back.

    With `--digest`, that digest, unprinted: the plan was shown already. The
    prompt goes to stderr and SWARM_ASSUME_YES is ignored, as for every
    typed confirmation here; no terminal to type at means nothing is sent.
    """
    from . import runs

    if args.digest:
        return args.digest
    run = client.run(args.run_id)
    if run.get("state") != "PLANNED" or not run.get("plan_digest"):
        raise SwarmError(
            f"run {args.run_id} is {run.get('state')}; only a PLANNED run's plan can be "
            f"{verb}ed. Nothing was sent"
        )
    _emit(runs.plan_lines(run, _run_style(args, out)), out)
    out.flush()
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write(f"sc: SWARM_ASSUME_YES is ignored here; a plan is {verb}ed by typing\n")
    typed = _ask(f"{verb} plan {run['plan_digest']} of run {args.run_id}? Type {verb!r} to confirm: ")
    if typed.strip() != verb:
        raise SwarmError(f"{verb!r} was not typed; nothing was sent")
    return str(run["plan_digest"])


def cmd_plan_approve(client: SwarmClient, args, out) -> int:
    digest = _shown_digest(client, args, out, "approve")
    run = client.approve_plan(args.run_id, plan_digest=digest)
    _print_run(client, run, args, out, plan=False)
    return _run_exit(run)


def cmd_plan_reject(client: SwarmClient, args, out) -> int:
    digest = _shown_digest(client, args, out, "reject")
    run = client.reject_plan(args.run_id, plan_digest=digest, reason=args.reason)
    _print_run(client, run, args, out, plan=False)
    return EXIT_OK


# --------------------------------------------------------------------------
# Schedules and approvals (docs/schedules.md §7.3, lane S8)
# --------------------------------------------------------------------------
#
# `sc schedules`, `show` and `preview` read; `new`, `pause`, `resume` and `run`
# write, and so do `sc approvals approve|reject`. An approval sends the digest
# of the item this command PRINTED, after `approve` is typed back, exactly as
# `sc plan approve` does -- or `--digest`, for an item already shown.


def _json_out(value: Any, out) -> int:
    out.write(json.dumps(value, indent=2, default=str) + "\n")
    return EXIT_OK


# -- registered MCP servers (docs/design/registered-mcp-servers.md §4.11) ---
#
# READ-ONLY, BY DESIGN. Registering a server is an admin act in the console,
# and a long-lived credential goes to the workspace secret store, which no
# tool or command here writes: a session that could register a URL could
# point the platform's relay anywhere, and one that took a store value would
# hold a secret in its transcript. So this is the one place the package names
# the registry's route, and only in a GET (tests/unit/mcp/test_mcp_servers_tool.py).

#: What one listing entry carries: the registration's NAMES and rules, never
#: its header or scheme, which only the worker's relay needs.
_MCP_SERVER_FIELDS = ("name", "url", "description", "allowed_profiles", "enabled", "last_attached_at")


def mcp_servers(client: SwarmClient) -> dict[str, Any]:
    """`GET /v1/mcp-servers`, shaped for a session: each server's name, URL,
    auth mode, whether a dispatch must bring a credential for it, the profiles
    and tools allowed, and whether it is enabled; and the egress addresses its
    operator allowlists. An empty `allowed_tools` means every tool (owner
    decision Q4), and says so rather than reading as none."""
    listing = client.request("GET", "/v1/mcp-servers")
    if not isinstance(listing, dict):
        raise SwarmError("GET /v1/mcp-servers answered no object: this deployment predates MCP servers")
    servers = []
    for entry in listing.get("servers") or []:
        if not isinstance(entry, dict):
            continue
        mode = (entry.get("auth") or {}).get("mode") if isinstance(entry.get("auth"), dict) else None
        tools = entry.get("allowed_tools")
        servers.append({
            **{key: entry.get(key) for key in _MCP_SERVER_FIELDS},
            "enabled": entry.get("enabled") is True,
            "auth_mode": mode or "none",
            "needs_credential": mode == "dispatch",
            "allowed_tools": list(tools) if tools else "every tool",
        })
    return {
        "tenant_id": listing.get("tenant_id"),
        "servers": servers,
        "egress_addresses": list(listing.get("egress_addresses") or []),
        "can_manage": listing.get("can_manage") is True,
        "use": (
            "name servers in a claude-code or codex step's `inputs.mcp_servers`; a "
            "`dispatch` server also needs that call's `mcp_credentials` entry, "
            "{\"<name>\": {\"value\": ..., \"expires_in\": seconds}}. Registering a server "
            "is done in the console (Settings > Workspace > MCP servers)"
        ),
    }


def mcp_servers_lines(view: dict[str, Any]) -> list[str]:
    """The listing as text: one line per server, then the addresses."""
    servers = view.get("servers") or []
    lines = [f"MCP SERVERS  {view.get('tenant_id') or ''}  {len(servers)} registered".rstrip()]
    if not servers:
        lines.append("  none registered for this workspace")
    for server in servers:
        tools = server["allowed_tools"]
        lines.append(
            f"  {server.get('name')}  {'enabled' if server['enabled'] else 'disabled'}  "
            f"auth {server['auth_mode']}"
            + ("  (needs mcp_credentials)" if server["needs_credential"] else "")
        )
        lines.append(f"    {server.get('url')}")
        lines.append(
            f"    profiles {', '.join(server.get('allowed_profiles') or []) or '-'}; tools "
            + (tools if isinstance(tools, str) else ", ".join(tools))
        )
    addresses = view.get("egress_addresses") or []
    lines.append(f"egress addresses to allowlist: {', '.join(addresses) if addresses else '- (not set)'}")
    lines.append(f"use: {view['use']}")
    return lines


def cmd_mcp_servers(client: SwarmClient, args, out) -> int:
    view = mcp_servers(client)
    if args.json:
        return _json_out(view, out)
    _emit(mcp_servers_lines(view), out)
    return EXIT_OK


# --------------------------------------------------------------------------
# Workspace roles (docs/design/workspace-roles.md §4.8, owner answer Q11)
# --------------------------------------------------------------------------

#: The roles a person may be given, lowest first, and the two a workspace's
#: default may be (an admin is named person by person). The API is the
#: authority on both; checked here only so a typo sends nothing.
ROLE_NAMES = ("viewer", "member", "admin")
DEFAULT_ROLE_NAMES = ("viewer", "member")

#: Where a role change's confirmation is read from: the person's terminal,
#: NEVER stdin and never an argument, so a script or an agent driving this
#: command cannot pre-supply it (§4.8). A process with no controlling terminal
#: cannot open it, and then nothing is sent. A module value so a test can
#: point it at a file.
TERMINAL = "/dev/tty"

#: The three role writes, by verb. NAMED ONCE, read by `_send_role_change`
#: only, and that by the three typed handlers only:
#: tests/unit/mcp/test_roles_bridge.py holds that no other module -- the MCP
#: server above all -- reaches them.
ROLE_WRITES = {
    "set": ("PUT", "/v1/workspace/roles/person"),
    "clear": ("DELETE", "/v1/workspace/roles/person"),
    "default": ("PUT", "/v1/workspace/roles/default"),
}

def roles_change() -> str:
    """How a person changes a role, said wherever the roster is shown, each
    command spelled for this install (`invocation.terminal_command`)."""
    from .invocation import terminal_command

    commands = ", ".join(
        f"`{terminal_command(words)}`"
        for words in ("sc roles set <email> <role>", "sc roles clear <email>",
                      "sc roles default <viewer|member>")
    )
    return (
        "a workspace admin changes roles in the console (Config › People and roles) or in "
        f"their own terminal: {commands}, each typed back to confirm. No MCP tool "
        "changes a role"
    )


def workspace_roles(client: SwarmClient) -> dict[str, Any]:
    """`GET /v1/workspace/roles`, shaped for a session: the default, the
    overrides, the caller's own role, what each role permits and the recent
    role events. One read; nothing else is sent, and it is the only role route
    this package reads (every role may, Q7)."""
    roster = client.request("GET", "/v1/workspace/roles")
    if not isinstance(roster, dict):
        raise SwarmError("GET /v1/workspace/roles answered no object: this deployment "
                         "predates workspace roles (#1518)")
    return {
        "tenant_id": roster.get("tenant_id"),
        "kind": roster.get("kind"),
        "default_role": roster.get("default_role"),
        "default_source": roster.get("default_source"),
        "me": roster.get("me") if isinstance(roster.get("me"), dict) else {},
        "people": [p for p in roster.get("people") or [] if isinstance(p, dict)],
        "roles": [r for r in roster.get("roles") or [] if isinstance(r, dict)],
        "can_manage": roster.get("can_manage") is True,
        "audit": [a for a in roster.get("audit") or [] if isinstance(a, dict)],
        "change": roles_change(),
    }


def workspace_roles_lines(view: dict[str, Any]) -> list[str]:
    """The roster as text: you, the default, the overrides, the roles, the events."""
    me = view.get("me") or {}
    lines = [f"ROLES  {view.get('tenant_id') or ''}  ({view.get('kind') or 'workspace'})".rstrip()]
    lines.append(
        f"  you      {me.get('email') or '-'}  {me.get('role') or '-'}  "
        f"(source {me.get('source') or '-'}, via {me.get('via') or '-'})"
    )
    if view.get("kind") == "user":
        lines.append("  a personal workspace has only you, and you are its admin")
    else:
        lines.append(
            f"  default  {view.get('default_role') or '-'}  "
            f"({view.get('default_source') or '-'}): everyone without an override holds it"
        )
        people = view.get("people") or []
        lines.append(f"  overrides  {len(people)}")
        for person in people:
            by = person.get("granted_by") or "-"
            via = person.get("granted_via")
            lines.append(
                f"    {person.get('email')}  {person.get('role')}  by {by}"
                + (f" via {via}" if via else "")
                + f"  {person.get('granted_at') or ''}".rstrip()
            )
    for role in view.get("roles") or []:
        lines.append(f"  {role.get('name'):<7} {role.get('summary') or ''}".rstrip())
    events = view.get("audit") or []
    if events:
        lines.append(f"  recent events  {len(events)}")
        for event in events:
            detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
            change = (f"  {detail.get('from')} -> {detail.get('to')}"
                      if "from" in detail or "to" in detail else "")
            lines.append(
                f"    {event.get('at') or ''}  {event.get('action')}  by {event.get('by') or '-'}"
                + (f"  {event.get('target_email')}" if event.get("target_email") else "")
                + change
            )
    lines.append(f"you {'may' if view.get('can_manage') else 'may not'} change roles here")
    lines.append(f"change: {view['change']}")
    return lines


def cmd_roles(client: SwarmClient, args, out) -> int:
    view = workspace_roles(client)
    if args.json:
        return _json_out(view, out)
    _emit(workspace_roles_lines(view), out)
    return EXIT_OK


def _ask_terminal(prompt: str) -> str:
    """One line typed at the person's TERMINAL, the prompt on stderr.

    Never stdin: a confirmation a pipe could carry is one a script or an
    agent could supply. No terminal to open means no confirmation, and the
    caller then sends nothing.
    """
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        with open(TERMINAL, encoding="utf-8") as terminal:
            return terminal.readline().rstrip("\r\n")
    except OSError as exc:
        raise SwarmError(
            f"a role change is confirmed by typing at a terminal, and {TERMINAL} could not be "
            f"opened ({exc.strerror or exc}); nothing was sent. Run it in your own terminal"
        ) from exc


def _send_role_change(client: SwarmClient, verb: str, body: dict[str, Any], phrase: str,
                      what: str) -> Any:
    """Ask for `phrase` at the terminal; send the `verb` write only if it was typed.

    SWARM_ASSUME_YES IS IGNORED: a role decides who may act in a workspace
    (tenant isolation), so it is typed every time. NOTHING IS SENT BEFORE THE
    CONFIRMATION, not even a read. A refusal carries the server's code and
    sentence, unchanged.
    """
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write("sc: SWARM_ASSUME_YES is ignored here; a role change is typed\n")
    typed = _ask_terminal(f"{what}? Type {phrase!r} to confirm: ")
    if typed.strip() != phrase:
        raise SwarmError(f"{phrase!r} was not typed; nothing was sent")
    method, path = ROLE_WRITES[verb]
    try:
        return client.request(method, path, payload={**body, "via": "cli"})
    except SwarmError as exc:
        if exc.code and exc.code not in str(exc):
            raise SwarmError(f"refused {exc.code}: {exc}", status=exc.status, edge=exc.edge,
                             code=exc.code, detail=exc.detail) from exc
        raise


def _role_body(args, **fields: Any) -> dict[str, Any]:
    body = dict(fields)
    if args.reason:
        body["reason"] = args.reason
    return body


def _checked_role(role: str, allowed: tuple[str, ...], what: str) -> str:
    if role not in allowed:
        raise SwarmError(f"{role!r} is not {what} ({', '.join(allowed)}); nothing was sent")
    return role


def _role_result(answer: Any, args, out) -> int:
    if args.json:
        return _json_out(answer, out)
    out.write(json.dumps(answer, indent=2, default=str) + "\n")
    return EXIT_OK


def cmd_roles_set(client: SwarmClient, args, out) -> int:
    """Give one person a role in the selected workspace, after the email and
    the role are TYPED back at the terminal."""
    role = _checked_role(args.role, ROLE_NAMES, "a role")
    email = args.email.strip()
    answer = _send_role_change(
        client, "set", _role_body(args, email=email, role=role), f"{email} {role}",
        f"give {email} the role {role} in this workspace",
    )
    return _role_result(answer, args, out)


def cmd_roles_clear(client: SwarmClient, args, out) -> int:
    """Return one person to the workspace's default, after the email and
    `default` are TYPED back at the terminal."""
    email = args.email.strip()
    answer = _send_role_change(
        client, "clear", _role_body(args, email=email), f"{email} default",
        f"return {email} to this workspace's default role",
    )
    return _role_result(answer, args, out)


def cmd_roles_default(client: SwarmClient, args, out) -> int:
    """Set the workspace's default role, after `default` and the role are
    TYPED back at the terminal."""
    role = _checked_role(args.role, DEFAULT_ROLE_NAMES, "a default role")
    answer = _send_role_change(
        client, "default", _role_body(args, role=role), f"default {role}",
        f"make {role} this workspace's default role, for everyone without an override",
    )
    return _role_result(answer, args, out)


def cmd_schedules(client: SwarmClient, args, out) -> int:
    from . import schedules

    data = schedules.listing(client)
    if args.json:
        return _json_out(data, out)
    _emit(schedules.schedules_lines(data, _run_style(args, out)), out)
    return EXIT_OK


def cmd_schedules_show(client: SwarmClient, args, out) -> int:
    from . import schedules

    data = schedules.schedule(client, schedules.resolve(client, args.schedule))
    if args.json:
        return _json_out(data, out)
    _emit(schedules.schedule_lines(data, _run_style(args, out)), out)
    return EXIT_OK


def cmd_schedules_new(client: SwarmClient, args, out) -> int:
    from . import schedules

    body: dict[str, Any] = {
        "name": args.name or f"{args.type} {', '.join(args.repo)}"[:80],
        "type": args.type,
        "cron": args.cron,
        "timezone": args.tz or "UTC",
    }
    params = schedules.parse_params(args.param)
    if params:
        body["params"] = params
    if args.dry_run:
        body["policy"] = {"dry_run": True}
    schedules.refuse_backend_fields(body, "sc schedules new")
    body["scope"] = schedules.scope(client, "repos", args.repo)
    data = schedules.create(client, body)
    if args.json:
        return _json_out(data, out)
    style = _run_style(args, out)
    verb = "created" if data.get("created", True) else "already created by this request"
    lines = [f"schedule {verb}" + (": dry run, every firing creates nothing" if args.dry_run else "")]
    _emit(lines + schedules.schedule_lines({"schedule": data.get("schedule"), "firings": []}, style), out)
    return EXIT_OK


def _schedule_verb(client: SwarmClient, args, out, call) -> int:
    from . import schedules

    data = call(schedules, schedules.resolve(client, args.schedule))
    if args.json:
        return _json_out(data, out)
    style = _run_style(args, out)
    if isinstance(data.get("firing"), dict):
        _emit([render.section("firing", "", style), schedules.firing_line(data["firing"], style)], out)
    else:
        _emit(schedules.schedule_lines({"schedule": data.get("schedule"), "firings": []}, style), out)
    return EXIT_OK


def cmd_schedules_pause(client: SwarmClient, args, out) -> int:
    return _schedule_verb(client, args, out, lambda s, sid: s.pause(client, sid, args.reason))


def cmd_schedules_resume(client: SwarmClient, args, out) -> int:
    if args.reason:
        # §7.3 spells `pause|resume ... [--reason]`; the resume route records
        # none, so the reason is said to be dropped rather than silently lost.
        sys.stderr.write("sc: the API records a reason on pause only; resuming without it\n")
    return _schedule_verb(client, args, out, lambda s, sid: s.resume(client, sid))


def cmd_schedules_run(client: SwarmClient, args, out) -> int:
    return _schedule_verb(client, args, out, lambda s, sid: s.run_now(client, sid, dry_run=args.dry_run))


def cmd_schedules_preview(client: SwarmClient, args, out) -> int:
    from . import schedules

    data = schedules.preview(client, args.cron, args.tz)
    if args.json:
        return _json_out(data, out)
    _emit(schedules.preview_lines(data, _run_style(args, out)), out)
    return EXIT_OK


def cmd_approvals(client: SwarmClient, args, out) -> int:
    from . import schedules

    data = schedules.inbox(client, args.kind)
    if args.json:
        return _json_out(data, out)
    _emit(schedules.inbox_lines(data, _run_style(args, out)), out)
    return EXIT_OK


def _shown_approval_digest(client: SwarmClient, args, out, verb: str) -> Any:
    """The digest of the item this command PRINTS, after `verb` is typed back.

    With `--digest`, that digest, unprinted. SWARM_ASSUME_YES is ignored: a
    decision is typed, and no terminal to type at means nothing is sent.
    """
    from . import schedules

    if args.digest:
        return args.digest
    item = schedules.approval(client, args.approval_id).get("approval") or {}
    if item.get("state") != "pending":
        raise SwarmError(f"approval {args.approval_id} is {item.get('state')}; only a pending "
                         f"item can be {verb}d. Nothing was sent")
    _emit(schedules.approval_lines(item, _run_style(args, out), how=False), out)
    out.flush()
    if os.environ.get("SWARM_ASSUME_YES", "").strip():
        sys.stderr.write(f"sc: SWARM_ASSUME_YES is ignored here; an approval is {verb}d by typing\n")
    shown = schedules._digest_text(item.get("digest"))
    typed = _ask(f"{verb} {args.approval_id} (digest {shown})? Type {verb!r} to confirm: ")
    if typed.strip() != verb:
        raise SwarmError(f"{verb!r} was not typed; nothing was sent")
    return item.get("digest")


def cmd_approvals_approve(client: SwarmClient, args, out) -> int:
    from . import schedules

    digest = _shown_approval_digest(client, args, out, "approve")
    data = schedules.approve(client, args.approval_id, digest=digest, confirm=args.confirm)
    if args.json:
        return _json_out(data, out)
    _emit(schedules.approval_lines(data.get("approval") or {}, _run_style(args, out), how=False), out)
    return EXIT_OK


def cmd_approvals_reject(client: SwarmClient, args, out) -> int:
    from . import schedules

    data = schedules.reject(client, args.approval_id, reason=args.reason)
    if args.json:
        return _json_out(data, out)
    _emit(schedules.approval_lines(data.get("approval") or {}, _run_style(args, out), how=False), out)
    return EXIT_OK


def _edit_in_editor(plan: dict[str, Any]) -> str:
    """The plan, as the developer left it in $VISUAL or $EDITOR."""
    import shlex
    import subprocess
    import tempfile

    editor = os.environ.get("VISUAL", "").strip() or os.environ.get("EDITOR", "").strip()
    if not editor:
        raise SwarmError("set $EDITOR (or $VISUAL), or pass --file plan.json; nothing was sent")
    with tempfile.TemporaryDirectory(prefix="sc-plan-") as scratch:
        path = os.path.join(scratch, "plan.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        finished = subprocess.run([*shlex.split(editor), path], check=False)
        if finished.returncode != 0:
            raise SwarmError(f"the editor exited {finished.returncode}; nothing was sent")
        with open(path, encoding="utf-8") as handle:
            return handle.read()


def cmd_plan_edit(client: SwarmClient, args, out) -> int:
    """Replace a PLANNED run's plan, sending the digest of the plan that was OPENED."""
    run = client.run(args.run_id)
    current = run.get("plan")
    if run.get("state") != "PLANNED" or not isinstance(current, dict):
        raise SwarmError(
            f"run {args.run_id} is {run.get('state')}; only a PLANNED run's plan can be "
            "edited. Nothing was sent"
        )
    opened = args.digest or str(run.get("plan_digest") or "")
    if args.file == "-":
        text = sys.stdin.read()
    elif args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = _edit_in_editor(current)
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SwarmError(f"the edited plan is not JSON ({exc}); nothing was sent") from exc
    if not isinstance(plan, dict):
        raise SwarmError("a plan is a JSON object with a summary and steps; nothing was sent")
    if plan == current:
        out.write(f"the plan of run {args.run_id} is unchanged; nothing was sent\n")
        return EXIT_OK
    run = client.edit_plan(args.run_id, plan_digest=opened, plan=plan)
    _print_run(client, run, args, out, plan=True)
    return EXIT_OK


def _common(parser: argparse.ArgumentParser, *, root: bool) -> None:
    """The same four flags, before OR after the subcommand.

    A SUBPARSER'S DEFAULTS OVERWRITE THE ROOT PARSER'S VALUES. argparse fills
    the namespace from the root parser first, then hands the same namespace to
    the subparser, which writes ITS defaults over the top -- so `sc --json
    trouble` parsed as `json=False` and printed a human table to a script that
    had asked for data, with no warning that the flag had been discarded.
    `argparse.SUPPRESS` is the fix: a subparser option that was not given
    leaves the attribute alone instead of resetting it, so whichever spelling
    the operator reaches for wins.

    Only the root may carry real defaults, and it must carry all of them: they
    are what guarantees every attribute exists whichever way the line was
    written.
    """
    def default(value: Any) -> dict[str, Any]:
        return {"default": value} if root else {"default": argparse.SUPPRESS}

    parser.add_argument("--width", type=int, help="force a column count", **default(None))
    parser.add_argument(
        "--no-color", dest="color", action="store_false",
        help="never emit colour (it is already off when stdout is not a terminal)",
        **default(None),
    )
    parser.add_argument(
        "--ascii", action="store_true",
        help="plain ASCII marks, for a terminal without the bar glyphs",
        **default(None),
    )
    parser.add_argument(
        "--json", action="store_true", help="the data, unformatted", **default(False)
    )
    parser.add_argument(
        "--context",
        help=f"which configured deployment to use (see `{help_command('sc context list')}`)",
        **default(None),
    )


def _targets() -> tuple[str, ...]:
    from . import config

    return config.TARGETS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sc",
        description="SwarmCloud at a glance: accounts, capacity, agents, trouble.",
        # Raw, so the legend keeps its lines. Reflowed, the marks and the exit
        # codes run into one paragraph -- and the marks are the contract.
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Marks:  12% measured  |  ~12% projected from an older reading  |\n"
            "        (em dash) not measured.  Never 0 for unknown.\n"
            "Exit:   0 nothing down  |  1 something could not be read  |\n"
            "        3 it was all read, and something is down."
        ),
    )
    _common(parser, root=True)
    parser.set_defaults(func=cmd_overview)
    sub = parser.add_subparsers(dest="command")

    # THE BARE COMMAND, UNDER A NAME. `sc` alone is the overview, and a flag
    # can only follow it at the root -- `sc --json` -- where a permission rule
    # that allows it (`sc --json *`) also allows `sc --json login`. A named
    # view takes its flags after the name, so it can be granted on its own.
    o = sub.add_parser("overview", help="everything at once (the same as `sc` alone)")
    _common(o, root=False)
    o.set_defaults(func=cmd_overview)

    a = sub.add_parser("accounts", help="the account pool: 5H, 7D, when it clears, state")
    _common(a, root=False)
    a.set_defaults(func=cmd_accounts)

    g = sub.add_parser("agents", help="agents running and queued")
    _common(g, root=False)
    g.set_defaults(func=cmd_agents)

    c = sub.add_parser("capacity", help="pool ceilings, and which pool binds each profile")
    _common(c, root=False)
    c.set_defaults(func=cmd_capacity)

    t = sub.add_parser("task", help="one agent: state, what it produced, commits, patch, pull request")
    t.add_argument("task_id")
    _common(t, root=False)
    t.set_defaults(func=cmd_task)

    r = sub.add_parser("trouble", help="what is wrong right now (exit 3 if anything is down)")
    _common(r, root=False)
    r.set_defaults(func=cmd_trouble)

    wf = sub.add_parser(
        "workflows",
        help="your tenant's running workflows and your running single tasks: label, state, age, console",
    )
    _common(wf, root=False)
    wf.add_argument(
        "--session-start", action="store_true",
        help="print the plugin's SessionStart hook output, and nothing when none run or on failure",
    )
    wf.set_defaults(func=cmd_workflows)

    bb = sub.add_parser(
        "build-board",
        help="refresh this project's build board (.claude/build-board/index.html) and open it",
    )
    _common(bb, root=False)
    bb.add_argument("--no-open", action="store_true", help="refresh and print the path; open nothing")
    bb.set_defaults(func=cmd_build_board)

    # -- which deployment, and who you are on it -------------------------
    li = sub.add_parser(
        "login", help="sign in to the current deployment as yourself (a browser window opens)"
    )
    _common(li, root=False)
    li.add_argument(
        "--client-secret-stdin", action="store_true",
        help="read the Desktop OAuth client secret from stdin instead of asking",
    )
    li.add_argument(
        "--pkce-only", action="store_true",
        help="sign in with PKCE and no client secret (the Desktop-client probe, plan §6.3)",
    )
    li.set_defaults(func=cmd_login, no_client=True)

    lo = sub.add_parser("logout", help="forget and revoke your sign-in to the current deployment")
    _common(lo, root=False)
    lo.set_defaults(func=cmd_logout, no_client=True)

    dr = sub.add_parser(
        "doctor", help="whether this plugin and the deployment agree on version (exit 3 if not)"
    )
    _common(dr, root=False)
    dr.set_defaults(func=cmd_doctor, no_client=True)

    wh = sub.add_parser("whoami", help="the context, URL, principal and tenant this machine uses")
    _common(wh, root=False)
    wh.set_defaults(func=cmd_whoami, no_client=True)

    cf = sub.add_parser(
        "config", help="the endpoint, tenant, session target and plugin version this machine uses"
    )
    _common(cf, root=False)
    cf.add_argument(
        "--target", choices=_targets(),
        help="show this target as the one in force, as a per-call target would be",
    )
    cf.set_defaults(func=cmd_config, no_client=True)

    db = sub.add_parser(
        "debug", help="one task: state, attempts, last error, newest events, the log's tail (masked)"
    )
    db.add_argument("task_id")
    db.add_argument("--events", type=int, default=DEBUG_EVENTS, help="how many newest events")
    db.add_argument("--log-lines", type=int, default=DEBUG_LOG_LINES, help="how many log lines")
    _common(db, root=False)
    db.set_defaults(func=cmd_debug)

    # -- the account verbs: WRITE to the pool (see `ACCOUNT_VERBS`) -------
    ac = sub.add_parser(
        "account", help="change one of your accounts: add, pause, resume, drain, remove, max-agents"
    )
    ac_sub = ac.add_subparsers(dest="account_command", required=True)
    for verb, state in ACCOUNT_VERBS.items():
        av = ac_sub.add_parser(verb, help=f"ask the broker to set your account {state}")
        av.add_argument("account", help="its label or id, as the accounts view lists it")
        av.add_argument("--reason", default="", help="recorded with the change")
        _common(av, root=False)
        av.set_defaults(func=cmd_account_state, verb=verb)
    ar = ac_sub.add_parser("remove", help="remove your account from the pool (type its label to confirm)")
    ar.add_argument("account", help="its label or id")
    _common(ar, root=False)
    ar.set_defaults(func=cmd_account_remove)
    am = ac_sub.add_parser(
        "max-agents", help="how many agents your account may hold at once (type its label to confirm)"
    )
    am.add_argument("account", help="its label or id")
    # A string, read by the handler: the number's refusal is a SwarmError in
    # this command's words, and nothing is resolved or asked before it.
    am.add_argument("max_agents", metavar="N", help="1 to 50")
    _common(am, root=False)
    am.set_defaults(func=cmd_account_max_agents)
    aa_help = "add an account by signing in to Claude (or a ChatGPT plan) in your browser"
    aa = ac_sub.add_parser("add", help=aa_help)
    aa.add_argument("--label", required=True, help="the account's name in the pool")
    aa.add_argument(
        "--provider", default="anthropic", choices=ACCOUNT_PROVIDERS,
        help="anthropic: a Claude subscription (default); openai: a ChatGPT plan for codex",
    )
    aa.add_argument(
        "--lend-to", action="append", default=[], metavar="TENANT",
        help="a tenant that may run on it too (repeat for several)",
    )
    _common(aa, root=False)
    aa.set_defaults(func=cmd_account_add)

    # -- platform admin: `sc admin purge` DELETES after a typed word --------
    adm = sub.add_parser("admin", help="platform-admin actions (full admin only)")
    adm_sub = adm.add_subparsers(dest="admin_command", required=True)
    ap = adm_sub.add_parser(
        "purge",
        help="delete FAILED/CANCELLED/DEAD_LETTERED history, audited (a dry run unless --delete)",
    )
    ap.add_argument("--state", dest="states", action="append", choices=PURGE_STATES,
                    help="a state to purge (repeat for several; default FAILED)")
    ap.add_argument("--before", help="only what last changed before this ISO time")
    ap.add_argument("--tenant", help="only this tenant's history")
    ap.add_argument("--exclude", action="append", default=[], metavar="ID",
                    help="a workflow or task id to keep (repeat for several)")
    ap.add_argument("--limit", type=int, default=50, help="items per API page (max 200)")
    ap.add_argument("--delete", action="store_true",
                    help=f"delete, after the dry run is printed and {PURGE_WORD!r} is typed")
    _common(ap, root=False)
    ap.set_defaults(func=cmd_admin_purge)

    # -- onboarding (#780, OB9): `setup` and `access` WRITE, bar the reads --
    st = sub.add_parser(
        "setup",
        help="request your workspace, connect GitHub as yourself, enable owners, choose "
             "repositories, verify",
        description=(
            "Walks the onboarding checklist from wherever it stands. Exit 0 when it "
            "ends ready, 3 when it stopped short (a step failed, the browser half "
            "timed out, nothing installed yet), 1 when something could not be read."
        ),
    )
    st.add_argument("--no-browser", action="store_true",
                    help="print the GitHub link instead of opening a browser")
    st.add_argument("--timeout", type=int, default=CONNECT_TIMEOUT_SECONDS,
                    help="seconds to wait for the browser half (default: the link's life)")
    _common(st, root=False)
    st.set_defaults(func=cmd_setup)
    st_sub = st.add_subparsers(dest="setup_command")
    ss = st_sub.add_parser("status", help="the checklist (exit 0 ready, 3 not ready, 1 unread)")
    _common(ss, root=False)
    ss.set_defaults(func=cmd_setup_status)
    # NO OPTION TAKES THE VALUE (D5): it is read from stdin, so it never
    # reaches shell history, `ps` or a model's command line.
    stk = st_sub.add_parser(
        "token",
        help="store a fallback personal access token for one owner, read from stdin "
             "(exit 0 stored, 3 refused, 1 unread)",
        description=(
            "For an org that will not install SwarmCloud's GitHub App, or refuses classic "
            "tokens: a fine-grained personal access token whose resource owner is that org. "
            "The token is read from stdin -- typed with no echo at a terminal, or one piped "
            "line -- and never from an argument; it is posted once and never printed."
        ),
    )
    stk.add_argument("--owner", required=True, help="the GitHub owner the token is for")
    _common(stk, root=False)
    stk.set_defaults(func=cmd_setup_token)

    # KEYS-PLUGIN. NO OPTION AND NO POSITIONAL TAKES THE KEY: anything past the
    # provider and the switches is refused unsent by `main`, unrepeated.
    key = sub.add_parser("key", help="provider keys you set: `key set typesafe`")
    key_sub = key.add_subparsers(dest="key_command", required=True,
                                 parser_class=_KeySetParser)
    kset = key_sub.add_parser(
        "set",
        help="store your TypeSafe key, typed at a hidden prompt or piped with --stdin "
             "(exit 0 stored, 3 refused, 2 nothing sent)",
        description=(
            "Stores YOUR TypeSafe key (it wins over the tenant's), or with --tenant the "
            "selected tenant's (admins). The key is read with no echo at a terminal, or one "
            "line from stdin with --stdin -- never from an argument -- sent once, and never "
            "printed: the answer is `stored: version N`."
        ),
    )
    kset.add_argument("provider", help=f"one of {', '.join(keys.PROVIDERS)}")
    kset.add_argument("--tenant", action="store_true",
                      help="the selected tenant's key, not your own (admins)")
    kset.add_argument("--stdin", action="store_true",
                      help="read one line from stdin instead of a hidden prompt")
    kset.add_argument("--context", default=argparse.SUPPRESS,
                      help="which configured deployment to use")
    kset.set_defaults(func=cmd_key_set, key_set=True)

    acc = sub.add_parser(
        "access",
        help="the repositories SwarmCloud may use as you: list, grant, revoke, verify",
        description="With no verb, the Access page and an interactive grant/revoke/verify.",
    )
    _common(acc, root=False)
    acc.set_defaults(func=cmd_access)
    acc_sub = acc.add_subparsers(dest="access_command")
    al = acc_sub.add_parser("list", help="your connection, enabled owners and grants")
    _common(al, root=False)
    al.set_defaults(func=cmd_access_list)
    ao = acc_sub.add_parser("orgs", help="every owner you reach and whether the App is installed")
    _common(ao, root=False)
    ao.set_defaults(func=cmd_access_orgs)
    aao = acc_sub.add_parser("add-org", help="enable an owner the App is installed on")
    aao.add_argument("owner")
    _common(aao, root=False)
    aao.set_defaults(func=cmd_access_add_org)
    aro = acc_sub.add_parser("remove-org", help="disable an owner and its grants (type it to confirm)")
    aro.add_argument("owner")
    _common(aro, root=False)
    aro.set_defaults(func=cmd_access_remove_org)
    ari = acc_sub.add_parser(
        "request-install",
        help="record that you asked an org's owners to install the App; print its install page")
    ari.add_argument("owner")
    _common(ari, root=False)
    ari.set_defaults(func=cmd_access_request_install)
    arp = acc_sub.add_parser("repos", help="one page of an enabled owner's repositories")
    arp.add_argument("owner")
    arp.add_argument("--page", type=int, default=1)
    arp.add_argument("--search", default=None, help="only names containing this")
    _common(arp, root=False)
    arp.set_defaults(func=cmd_access_repos)
    # --read/--write are checked by the handler, not as a required group:
    # argparse would refuse a bare `access grant x` before any handler ran,
    # and test_plugin_commands probes each verb by the handler it reaches.
    ag = acc_sub.add_parser("grant", help="grant owner/repo --read or --write")
    ag.add_argument("repository", help="owner/repo")
    ag.add_argument("--read", action="store_true")
    ag.add_argument("--write", action="store_true")
    _common(ag, root=False)
    ag.set_defaults(func=cmd_access_grant)
    arv = acc_sub.add_parser("revoke", help="revoke your grant on owner/repo")
    arv.add_argument("repository", help="owner/repo")
    _common(arv, root=False)
    arv.set_defaults(func=cmd_access_revoke)
    av_ = acc_sub.add_parser("verify", help="clone, push, pull request checks (exit 3 if one fails)")
    av_.add_argument("repositories", nargs="*", metavar="owner/repo",
                     help="the grants to verify (default: every one)")
    av_.add_argument("--push-test", action="store_true",
                     help="also create and delete swarmcloud/onboarding-check-<nonce> in ONE "
                          "write grant (type its name to confirm)")
    _common(av_, root=False)
    av_.set_defaults(func=cmd_access_verify)
    ad = acc_sub.add_parser("disconnect", help="revoke SwarmCloud's access as you (type to confirm)")
    _common(ad, root=False)
    ad.set_defaults(func=cmd_access_disconnect)

    # -- issue runs (#454): `run` and `plan` WRITE, `runs` and the shows read
    rn = sub.add_parser(
        "run", help="plan a GitHub issue and run the plan once approved: run --issue owner/repo#N"
    )
    rn.add_argument("--issue", help="owner/repo#N, or the issue's URL")
    rn.add_argument(
        "--plan", dest="plan_approval", choices=("required", "auto"), default="required",
        help="required (default): the plan waits for your approval; auto: approved when read",
    )
    rn.add_argument(
        "--auto-merge", action="store_true",
        help="end in a merge -- visible but disabled: the API refuses it until #295",
    )
    rn.add_argument("--fix-rounds", type=int, default=None, metavar="N",
                    help="CI fix rounds after the pull request opens (the API's default is 3)")
    rn.add_argument("--follow", action="store_true",
                    help="keep reading the run, one row per step, until it ends or waits for you")
    rn.add_argument("--interval", type=float, default=10.0, help="seconds between reads with --follow")
    _common(rn, root=False)
    rn.set_defaults(func=cmd_run)
    rn_sub = rn.add_subparsers(dest="run_command")
    rs = rn_sub.add_parser("show", help="one run: state, plan, pull request, CI, its steps")
    rs.add_argument("run_id")
    _common(rs, root=False)
    rs.set_defaults(func=cmd_run_show)

    rl = sub.add_parser("runs", help="your tenant's issue runs, newest first")
    rl.add_argument("--limit", type=int, default=None)
    rl.add_argument("--page-token", default=None)
    _common(rl, root=False)
    rl.set_defaults(func=cmd_runs)

    pl = sub.add_parser("plan", help="a run's plan: show, approve, edit, reject")
    pl_sub = pl.add_subparsers(dest="plan_command", required=True)
    ps = pl_sub.add_parser("show", help="the plan and the digest an approval must send")
    ps.add_argument("run_id")
    _common(ps, root=False)
    ps.set_defaults(func=cmd_plan_show)
    pa = pl_sub.add_parser("approve", help="approve the plan printed (type `approve`), or --digest")
    pa.add_argument("run_id")
    pa.add_argument("--digest", help="the plan_digest you were shown; skips the prompt")
    _common(pa, root=False)
    pa.set_defaults(func=cmd_plan_approve)
    pe = pl_sub.add_parser("edit", help="replace the plan: --file plan.json, or $EDITOR")
    pe.add_argument("run_id")
    pe.add_argument("--file", help="the whole replacement plan as JSON; - reads stdin")
    pe.add_argument("--digest", help="the digest of the plan you edited (default: the one read now)")
    _common(pe, root=False)
    pe.set_defaults(func=cmd_plan_edit)
    pr = pl_sub.add_parser("reject", help="reject the plan printed (type `reject`), or --digest")
    pr.add_argument("run_id")
    pr.add_argument("--reason", required=True, help="recorded on the run and the issue")
    pr.add_argument("--digest", help="the plan_digest you were shown; skips the prompt")
    _common(pr, root=False)
    pr.set_defaults(func=cmd_plan_reject)

    # -- schedules (docs/schedules.md §7.3): the list, `show` and `preview`
    # read; `new`, `pause`, `resume`, `run` and the approval verbs WRITE
    sh = sub.add_parser("schedules", help="your tenant's schedules, like the console's table")
    _common(sh, root=False)
    sh.set_defaults(func=cmd_schedules)
    sh_sub = sh.add_subparsers(dest="schedules_command")
    shs = sh_sub.add_parser("show", help="one schedule and its last 10 firings")
    shs.add_argument("schedule", metavar="name|id")
    _common(shs, root=False)
    shs.set_defaults(func=cmd_schedules_show)
    shn = sh_sub.add_parser("new", help="create a schedule: new <type> --repo owner/repo --cron \"...\"")
    shn.add_argument("type", help="a type from the catalogue (no image or command is accepted)")
    shn.add_argument("--repo", action="append", required=True, metavar="owner/repo",
                     help="a repository registered in your tenant; repeat for several")
    shn.add_argument("--cron", required=True, help='five fields, e.g. "0 9 * * 1-5"')
    shn.add_argument("--tz", default=None, help="an IANA zone (default UTC)")
    shn.add_argument("--param", action="append", default=None, metavar="k=v",
                     help="one of the type's params; a JSON value (5, true) is read as JSON")
    shn.add_argument("--name", default=None, help="default: '<type> <repos>'")
    shn.add_argument("--dry-run", action="store_true",
                     help="every firing lists what it would do and creates nothing")
    _common(shn, root=False)
    shn.set_defaults(func=cmd_schedules_new)
    for verb, handler, text in (
        ("pause", cmd_schedules_pause, "pause a schedule: no firing until resumed"),
        ("resume", cmd_schedules_resume, "resume a paused schedule from its next slot"),
    ):
        shv = sh_sub.add_parser(verb, help=text)
        shv.add_argument("schedule", metavar="name|id")
        shv.add_argument("--reason", default=None, help="recorded in the schedule's audit (pause)")
        _common(shv, root=False)
        shv.set_defaults(func=handler)
    shr = sh_sub.add_parser("run", help="fire a schedule now, under its gate's run point")
    shr.add_argument("schedule", metavar="name|id")
    shr.add_argument("--dry-run", action="store_true", help="list what it would do; create nothing")
    _common(shr, root=False)
    shr.set_defaults(func=cmd_schedules_run)
    shp = sh_sub.add_parser("preview", help="a cron in words and its next slots")
    shp.add_argument("cron")
    shp.add_argument("--tz", default=None, help="an IANA zone (default UTC)")
    _common(shp, root=False)
    shp.set_defaults(func=cmd_schedules_preview)

    ms = sub.add_parser("mcp-servers", help="your workspace's registered MCP servers (read-only)")
    _common(ms, root=False)
    ms.set_defaults(func=cmd_mcp_servers)

    # ROLES-MCP (workspace-roles.md §4.8): the roster is a view; `set`, `clear`
    # and `default` change who may act in the workspace, so each is typed back
    # at the terminal and no skill is granted them. There is no flag that
    # supplies the confirmation, by design.
    ro = sub.add_parser("roles", help="your workspace's roles: list, or set|clear|default (typed)")
    _common(ro, root=False)
    ro.set_defaults(func=cmd_roles)
    ro_sub = ro.add_subparsers(dest="roles_command")
    rol = ro_sub.add_parser("list", help="the default, the overrides, your role, recent events")
    _common(rol, root=False)
    rol.set_defaults(func=cmd_roles)
    ros = ro_sub.add_parser("set", help="give a person a role (type the email and role back)")
    ros.add_argument("email")
    ros.add_argument("role", help="viewer, member or admin")
    ros.add_argument("--reason", default=None, help="recorded with the change")
    _common(ros, root=False)
    ros.set_defaults(func=cmd_roles_set)
    roc = ro_sub.add_parser("clear", help="return a person to the default (type it back)")
    roc.add_argument("email")
    roc.add_argument("--reason", default=None, help="recorded with the change")
    _common(roc, root=False)
    roc.set_defaults(func=cmd_roles_clear)
    rod = ro_sub.add_parser("default", help="set the workspace's default role (type it back)")
    rod.add_argument("role", help="viewer or member")
    rod.add_argument("--reason", default=None, help="recorded with the change")
    _common(rod, root=False)
    rod.set_defaults(func=cmd_roles_default)

    ap_ = sub.add_parser("approvals", help="the approval inbox; then approve|reject <id>")
    ap_.add_argument("--kind", default=None, choices=("run", "plan", "merge", "proposal", "spec", "hold"))
    _common(ap_, root=False)
    ap_.set_defaults(func=cmd_approvals)
    ap_sub = ap_.add_subparsers(dest="approvals_command")
    apa = ap_sub.add_parser("approve", help="approve the item printed (type `approve`), or --digest")
    apa.add_argument("approval_id")
    apa.add_argument("--digest", help="the digest you were shown; skips the prompt")
    apa.add_argument("--confirm", default=None,
                     help="a one-person tenant's typed confirmation of its own hold")
    _common(apa, root=False)
    apa.set_defaults(func=cmd_approvals_approve)
    apr = ap_sub.add_parser("reject", help="reject an item, with a reason")
    apr.add_argument("approval_id")
    apr.add_argument("--reason", required=True, help="recorded on the item")
    _common(apr, root=False)
    apr.set_defaults(func=cmd_approvals_reject)

    cx = sub.add_parser(
        "context", help="the deployments this machine knows: add, use, list, remove"
    )
    cx_sub = cx.add_subparsers(dest="context_command", required=True)

    ca = cx_sub.add_parser("add", help="add or update a deployment")
    ca.add_argument("name")
    ca.add_argument(
        "--url", required=True, help="the deployment's address, e.g. https://swarm.example.com"
    )
    ca.add_argument(
        "--client-id", default="",
        help=(
            "its Desktop OAuth client id "
            f"(needed for `{help_command('sc login')}` at an IAP front door)"
        ),
    )
    ca.add_argument(
        "--client-secret-stdin", action="store_true",
        help="read that client's secret from stdin into the credential store (never an argument)",
    )
    ca.add_argument("--use", action="store_true", help="make it the current context")
    ca.set_defaults(func=cmd_context_add, no_client=True)

    cu = cx_sub.add_parser("use", help="make a context current")
    cu.add_argument("name")
    cu.set_defaults(func=cmd_context_use, no_client=True)

    cl = cx_sub.add_parser("list", help="every context, which is current, which are signed in")
    cl.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    cl.set_defaults(func=cmd_context_list, no_client=True)

    cr = cx_sub.add_parser("remove", help="remove a context and the credentials kept for it")
    cr.add_argument("name")
    cr.set_defaults(func=cmd_context_remove, no_client=True)

    return parser


def main(argv: list[str] | None = None, out=None) -> int:
    from . import config

    parser = build_parser()
    try:
        args, extra = parser.parse_known_args(argv)
    except KeySetLineRefused:
        # Nothing has been read or sent, and the line is not repeated.
        print("sc: sc key set takes a provider, --tenant and --stdin, nothing else; "
              + KEY_ON_ARGV.format(command=terminal_command("sc key set typesafe")),
              file=sys.stderr)
        return EXIT_USAGE
    if getattr(args, "key_set", False):
        # BEFORE any client: a key typed on the line is refused unsent, and
        # argparse's own "unrecognized arguments" would have repeated it.
        args.key_extra = extra
        refused = key_set_refusal(extra)
        if refused is not None:
            print(f"sc: {refused}", file=sys.stderr)
            return EXIT_USAGE
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    stream = out or sys.stdout
    # THE SESSION TARGET IS CHECKED AT START-UP (S8), as the MCP server checks
    # it: a config file or plugin setting naming no target stops here, with
    # the three it may name, rather than being read as some other target.
    # `sc config` is exempt: it is how an operator SEES the bad target, which
    # it reports as not read with this same reason (and exits non-zero).
    if args.func is not cmd_config:
        try:
            config.session_target(override=getattr(args, "target", None))
        except SwarmError as exc:
            print(f"sc: {exc}", file=sys.stderr)
            return EXIT_FAIL
    if getattr(args, "no_client", False):
        # Sign-in and contexts exist to fix a connection that cannot be made,
        # so making one first would stop them running exactly when needed.
        try:
            return args.func(None, args, stream)
        except SwarmError as exc:
            print(f"sc: {exc}", file=sys.stderr)
            return EXIT_FAIL
        except KeyboardInterrupt:  # pragma: no cover
            return 130
    try:
        with SwarmClient(context=args.context, tenant=tenant_choice()[0]) as client:
            return args.func(client, args, stream)
    except SwarmError as exc:
        # THE SUBMISSION GATE'S REFUSAL (#847), OR AN UNKNOWN DEFAULT (503
        # `preferences_unavailable`, PROFILE-SC): the API answered, so this is
        # not a connection failure and `swarm doctor` is no answer to it. Its
        # message, word for word, and the command that fixes it.
        refused = workspace_refusal_text(
            exc, f"`{terminal_command('sc setup')}`"
        ) or preferences_refusal_text(exc)
        if refused is not None:
            print(refused, file=sys.stderr)
            return EXIT_FAIL
        # The connection itself failed, so there is no snapshot to mark up.
        # Say so on stderr and leave stdout empty rather than printing a screen
        # of dashes that looks like a reading.
        print(f"sc: {exc}", file=sys.stderr)
        # SPELLED TO RUN. This line fires only when the connection failed, so
        # the reader is already deciding whether the platform is broken; a
        # `command not found` on top of that decides it for them, wrongly.
        print(
            f"sc: run `{terminal_command('swarm doctor')}` to see which auth tier "
            "this machine is on",
            file=sys.stderr,
        )
        return EXIT_FAIL
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
