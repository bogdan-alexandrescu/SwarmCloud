"""`sc` -- what the SwarmCloud cluster is doing right now, in one screen.

WHY THIS IS A SEPARATE COMMAND FROM `swarm`. `swarm` is imperative: dispatch,
tail, apply, integrate. `sc` is interrogative and read-only TOWARDS THE
CLUSTER -- it never writes to it, never refreshes an account's credential and
never cancels anything. Keeping the two apart means `sc` can be reached for
without reading the flags first, which is the only way a status command gets
used at the moment it is needed.

THE ONE EXCEPTION IS `sc account ...` (S10, owner decision 2026-10-01): pause,
resume, drain and remove one of your own accounts, and add one through the
API's browser sign-in. The API served those routes and nothing reached them.
They are a separate subcommand, never a view, held out of every skill's grant
by `test_plugin_commands.py`, and `remove` takes the label typed back.

THE SECOND IS THE ISSUE RUN (#454): `sc run --issue owner/repo#N` creates a
run -- one planner task -- and `sc plan approve|edit|reject <run>` moves its
plan. A plan holds no capacity until it is approved, and an approval sends the
digest of the plan `sc` PRINTED, after `approve` is typed back. Held out of
every grant exactly as the account verbs are; `sc runs`, `sc run show` and
`sc plan show` are views.

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

from . import render
from .client import SwarmClient, SwarmError, outputs_of
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

#: Pages of `GET /v1/workflows` read before the list says it stopped short,
#: from a deployment that filters only after its rollup (no `filter` in its
#: answer): there a tenant with a long finished history can need several pages
#: to reach an old running workflow. A deployment that serves `active=true`
#: from its indexed stored-state query is read ONE page (owner decision
#: 2026-10-06, P4): measured that day, the old walk read 4 pages of 50 to list
#: 7 running workflows and still said it was incomplete.
WORKFLOW_LIST_PAGES = 4
WORKFLOW_PAGE_SIZE = 50

#: Running workflows read in detail (one `GET /v1/workflows/{id}` each, for the
#: label and the current steps). Beyond this many the list still names them,
#: says they were not read, and costs nothing more; `/sc attach --all` follows
#: at most 10 anyway.
WORKFLOW_DETAIL_READS = 20

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


def _running_entry(workflow: dict[str, Any], now: datetime) -> dict[str, Any]:
    created = render.parse_time(workflow.get("created_at"))
    entry: dict[str, Any] = {
        "workflow_id": workflow.get("workflow_id"),
        "label": None,
        "title": None,
        "state": workflow.get("state"),
        "created_at": workflow.get("created_at"),
        "age_seconds": max(0, int((now - created).total_seconds())) if created else None,
        "steps_total": len(workflow.get("steps") or []),
        "current_steps": None,
    }
    return with_console(entry, workflow)


def _read_detail(client: SwarmClient, entry: dict[str, Any]) -> dict[str, Any]:
    """The label and the unfinished steps, from one workflow read."""
    from . import workflows

    try:
        envelope = workflows.fetch(client, str(entry["workflow_id"]))
    except SwarmError as exc:
        entry["steps_unread_because"] = str(exc)
        return entry
    workflow = envelope["workflow"]
    if workflow.get("state"):
        entry["state"] = workflow["state"]
    entry["label"] = _label_of(envelope)
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
    return entry


def running_workflows(
    client: SwarmClient,
    *,
    now: datetime | None = None,
    pages: int = WORKFLOW_LIST_PAGES,
    detail_reads: int = WORKFLOW_DETAIL_READS,
) -> dict[str, Any]:
    """The caller's tenant's workflows that are not over, newest first.

    The tenant is the API's: `GET /v1/workflows` answers for the caller's own
    tenant (`tenant_scope`), and nothing here names one. The route is asked for
    `active=true` and `ACTIVE_WORKFLOW_STATES`, and the answer is filtered
    AGAIN, because a deployment older than those filters ignores the
    parameters and serves every workflow -- and attaching a finished one would
    start rows with nothing to watch. A workflow whose detail read says it has
    since finished is dropped for the same reason. Raises `SwarmError` when
    the list itself cannot be read: "could not ask" is never an empty list.

    ONE PAGE when the route says it filtered in its query
    (`filter.stored_states`): that page is the newest `WORKFLOW_PAGE_SIZE`
    workflows not finished by their stored state, so it is the running set
    unless a token says there are more -- and then that is reported, not
    walked. Otherwise up to `pages` pages, as before.
    """
    now = now or datetime.now(timezone.utc)
    found: list[dict[str, Any]] = []
    tenant: str | None = None
    token: str | None = None
    read = 0
    indexed = False
    for _ in range(max(1, pages)):
        page = client.workflows(
            states=ACTIVE_WORKFLOW_STATES,
            active=True,
            limit=WORKFLOW_PAGE_SIZE,
            page_token=token,
        )
        read += 1
        tenant = tenant or page.get("tenant_id")
        for workflow in page["workflows"]:
            if isinstance(workflow, dict) and workflow.get("state") not in _TERMINAL_VALUES:
                found.append(_running_entry(workflow, now))
        token = page.get("next_page_token") or None
        filtered = page.get("filter")
        indexed = isinstance(filtered, dict) and bool(filtered.get("stored_states"))
        if token is None or indexed:
            break

    detailed = found[: max(0, detail_reads)]
    if detailed:
        with ThreadPoolExecutor(max_workers=min(8, len(detailed))) as pool:
            list(pool.map(lambda entry: _read_detail(client, entry), detailed))
    for entry in found[len(detailed):]:
        entry["steps_unread_because"] = (
            f"only the newest {detail_reads} running workflows are read step by step"
        )
    listed = [entry for entry in found if entry.get("state") not in _TERMINAL_VALUES]
    out: dict[str, Any] = {
        "tenant_id": tenant,
        "count": len(listed),
        "complete": token is None,
        "workflows": listed,
    }
    if token is not None:
        out["incomplete_because"] = (
            f"more than {WORKFLOW_PAGE_SIZE} workflows are unfinished; read 1 page of "
            f"the newest {WORKFLOW_PAGE_SIZE}, and older running workflows are not listed"
            if indexed
            else f"stopped after {read} pages of {WORKFLOW_PAGE_SIZE} workflows; older "
            "running workflows, if any, are not listed"
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


def session_start_context(listing: dict[str, Any]) -> str | None:
    """What the SessionStart hook tells the session, or None when nothing runs.

    A hook cannot start a Workflow; the context makes the session's first
    action the attach, and says how to turn this off.
    """
    running = listing.get("workflows") or []
    if not running:
        return None
    count = len(running)
    noun = "workflow is" if count == 1 else "workflows are"
    named = []
    for entry in running[:SESSION_START_NAMED]:
        label = f' "{entry["label"]}"' if entry.get("label") else ""
        named.append(
            f"{entry.get('workflow_id')}{label} ({entry.get('state')}; {_steps_text(entry)}; "
            f"{_age_text(entry.get('age_seconds'))})"
        )
    if count > SESSION_START_NAMED:
        named.append(f"and {count - SESSION_START_NAMED} more")
    tenant = f" for tenant {listing['tenant_id']}" if listing.get("tenant_id") else ""
    return (
        f"{count} SwarmCloud {noun} running{tenant} and not shown in this session: "
        + "; ".join(named)
        + ". Before anything else, run `/sc attach --all` to show them as live "
        "[SwarmCloud] rows: it submits nothing, starts one slim row per unfinished "
        "step and follows at most 10 workflows. (This notice comes from the sc "
        "plugin's SessionStart hook; turn off its `auto_attach` option to stop it.)"
    )


def cmd_workflows(client: SwarmClient, args, out) -> int:
    if getattr(args, "session_start", False):
        # THE HOOK'S MODE: silent on everything but a running workflow. A
        # session must start the same whether or not SwarmCloud answered, so a
        # failure prints nothing and exits 0 -- the hook script discards
        # stderr anyway, and this keeps the CLI from relying on that.
        try:
            context = session_start_context(running_workflows(client))
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

    listing, failure = _attempt(lambda: running_workflows(client))
    if args.json:
        out.write(
            json.dumps(listing if listing is not None else {"error": str(failure)}, indent=2, default=str)
            + "\n"
        )
        return EXIT_OK if listing is not None else EXIT_FAIL
    style = style_for(out, width=args.width, color=args.color, ascii_only=args.ascii)
    if listing is None:
        _emit([render.section("workflows", "", style), f"  could not be read: {failure}"], out)
        return EXIT_FAIL
    running = listing["workflows"]
    lines = [render.section("workflows", f"{len(running)} running", style)]
    if not running:
        lines.append("  none running")
    for entry in running:
        label = f"  {entry['label']}" if entry.get("label") else ""
        lines.append(
            f"  {entry.get('workflow_id')}{label}  {entry.get('state')}  "
            f"{_age_text(entry.get('age_seconds'))}"
        )
        lines.append(f"    now: {_steps_text(entry)}")
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
    if running:
        lines.append("  show them as live rows in Claude Code: /sc attach --all")
    _emit(lines, out)
    return EXIT_OK


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
            else [render.public_account(a) for a in snap.accounts]
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
    started = client.request(
        "POST", "/v1/accounts/authorize", payload={"label": label, "lend_to": lend_to}
    )
    url = started.get("authorize_url") if isinstance(started, dict) else None
    state = started.get("state") if isinstance(started, dict) else None
    if not url or not state:
        raise SwarmError("the API began no sign-in: its answer named no URL to open")
    opened = _open_browser(url)
    sys.stderr.write(
        ("opened your browser to sign in. " if opened else "could not open a browser. ")
        + f"If it did not open, visit:\n  {url}\n"
    )
    minutes = started.get("expires_in_seconds")
    if isinstance(minutes, int):
        sys.stderr.write(f"the sign-in expires in {minutes // 60} minutes\n")
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
    if isinstance(created, dict) and created.get("note"):
        out.write(f"note        {created['note']}\n")
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
        return {"value": None, "not_read_because": str(error)}
    return {"value": value}


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
                problems.append(f"{stream}: unreadable: {entry.get('detail')}")
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
    secret = _read_secret_line() if getattr(args, "client_secret_stdin", False) else None
    claims = signin.login(
        deployment,
        store=store,
        client_secret=secret or None,
        notify=lambda line: print(line, file=sys.stderr),
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
    try:
        with SwarmClient(deployment=deployment, tier=auth.Tier.SIGNED_IN) as api:
            me = api.request("GET", "/v1/tenants/me") or {}
    except SwarmError as exc:
        out.write(f"but {deployment.url} did not accept it: {exc}\n")
        return EXIT_FAIL
    # `tenant_of`: the route nests the tenant, and the flat read printed
    # "(not reported)" for one it had just served (#88, SC-F3).
    out.write(f"tenant      {render.tenant_of(me) or '(not reported)'}\n")
    return EXIT_OK


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

    shown: dict[str, Any] = {
        "context": None, "url": None, "source": None, "current": None,
        "tier": None, "credential_store": None, "principal": None,
        "tenant": None, "error": None,
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
        with SwarmClient(deployment=deployment) as api:
            me = api.request("GET", "/v1/tenants/me") or {}
        # Nested under `tenant` and `principal`, which the flat reads missed
        # (#88, SC-F3) -- through the one reader `render` keeps for it.
        shown["tenant"] = render.tenant_of(me)
        shown["principal"] = shown["principal"] or render.principal_of(me).get("email")
    except SwarmError as exc:
        shown["error"] = str(exc)
        code = EXIT_FAIL
    return shown, code


def cmd_whoami(_client, args, out) -> int:
    """Context, URL, where that came from, the tier, the principal, the tenant.

    Every field is filled from a READ: the principal from the signed-in ID
    token (or from the API, on a tier without one), the tenant from
    `/v1/tenants/me`. A field that could not be read is left out and the
    reason printed, because a blank reads as "none".
    """
    shown, code = read_whoami(args)
    if getattr(args, "json", False):
        out.write(json.dumps(shown, indent=2) + "\n")
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
        out.write(f"{label:<11} {value}\n")
    if shown["error"]:
        out.write(f"{'problem':<11} {shown['error']}\n")
    return code


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
        "tenant": field(shown.get("tenant")),
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
        ("tenant", text(view["tenant"])),
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
        "workflows", help="your tenant's running workflows: label, current steps, age, console"
    )
    _common(wf, root=False)
    wf.add_argument(
        "--session-start", action="store_true",
        help="print the plugin's SessionStart hook output, and nothing when none run or on failure",
    )
    wf.set_defaults(func=cmd_workflows)

    # -- which deployment, and who you are on it -------------------------
    li = sub.add_parser(
        "login", help="sign in to the current deployment as yourself (a browser window opens)"
    )
    _common(li, root=False)
    li.add_argument(
        "--client-secret-stdin", action="store_true",
        help="read the Desktop OAuth client secret from stdin instead of asking",
    )
    li.set_defaults(func=cmd_login, no_client=True)

    lo = sub.add_parser("logout", help="forget and revoke your sign-in to the current deployment")
    _common(lo, root=False)
    lo.set_defaults(func=cmd_logout, no_client=True)

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
        "account", help="change one of your accounts: add, pause, resume, drain, remove"
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
    aa = ac_sub.add_parser("add", help="add an account by signing in to Claude in your browser")
    aa.add_argument("--label", required=True, help="the account's name in the pool")
    aa.add_argument(
        "--lend-to", action="append", default=[], metavar="TENANT",
        help="a tenant that may run on it too (repeat for several)",
    )
    _common(aa, root=False)
    aa.set_defaults(func=cmd_account_add)

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

    args = build_parser().parse_args(argv)
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
        with SwarmClient(context=args.context) as client:
            return args.func(client, args, stream)
    except SwarmError as exc:
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
