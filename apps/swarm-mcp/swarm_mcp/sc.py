"""`sc` -- what the SwarmCloud cluster is doing right now, in one screen.

WHY THIS IS A SEPARATE COMMAND FROM `swarm`. `swarm` is imperative: dispatch,
tail, apply, integrate. `sc` is interrogative and read-only TOWARDS THE
CLUSTER -- it never writes to it, never refreshes an account's credential and
never cancels anything. Keeping the two apart means `sc` can be reached for
without reading the flags first, which is the only way a status command gets
used at the moment it is needed.

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
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from . import render
from .client import SwarmClient, SwarmError
from .follow import terminal_command
from .patches import explain_absence, patch_uri
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


def fetch_tasks(client: SwarmClient, *, limit: int = 100) -> list[dict[str, Any]]:
    data = client.request("GET", f"/v1/tasks?limit={limit}")
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if tasks is None:
        raise SwarmError("GET /v1/tasks answered without a `tasks` field")
    return list(tasks)


def fetch_task(client: SwarmClient, task_id: str) -> dict[str, Any]:
    """`GET /v1/tasks/{id}` answers `{"task": {...}}`.

    Unwrapped by the CLIENT, in one place, rather than by a second rule kept
    here. This file used to hold its own copy; the copy was right and the
    client's absence of one was wrong, so every `swarm` command read the
    envelope as the task while `sc` read it correctly.
    """
    return client.task(task_id)


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
    return [a for a in accounts if isinstance(a, dict)]


#: Which fetches each subcommand actually needs. Asking for everything on every
#: command would make `sc accounts` wait on a task listing it will not print --
#: and, worse, report a task-listing failure as though it were an account one.
#:
#: Each view fetches exactly what it PRINTS, which is also what its exit code
#: is about. The narrow views used to fetch `/v1/tenants/me` and then render
#: none of it: a round trip whose failure was invisible on screen and could
#: not honestly be allowed to change the exit code either.
NEEDS = {
    "overview": ("tenant", "stats", "capacity", "accounts", "tasks"),
    "accounts": ("accounts",),
    "agents": ("tasks",),
    "capacity": ("capacity",),
    "trouble": ("tenant", "stats", "capacity", "accounts", "tasks"),
}


def collect(client: SwarmClient, wanted: tuple[str, ...]) -> Snapshot:
    """Fetch what a command needs, concurrently, recording each failure.

    Concurrent because these are four independent round trips through IAP and a
    status command that takes four seconds gets replaced by a guess. The ID
    token is minted once, up front, rather than raced for by four threads that
    would each shell out to gcloud.
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
    else to show; the overview has four other areas and prints the reason.
    """
    unreadable = [
        error
        for error in (
            snap.tenant_error,
            snap.stats_error,
            snap.capacity_error,
            snap.tasks_error,
            None if snap.accounts_absent else snap.accounts_error,
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
    _emit(
        render.render_task(
            task,
            style,
            now,
            error=error,
            patch=uri,
            no_patch_because=None if uri or not task else explain_absence(task),
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
    }
    out.write(json.dumps(payload, indent=2, default=str) + "\n")


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
        who, remedy = f"the {tier.value} tier", "see `swarm doctor`"
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
    out.write(f"tenant      {me.get('tenant_id') or '(not reported)'}\n")
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


def cmd_whoami(_client, args, out) -> int:
    """Context, URL, where that came from, the tier, the principal, the tenant.

    Every field is filled from a READ: the principal from the signed-in ID
    token (or from the API, on a tier without one), the tenant from
    `/v1/tenants/me`. A field that could not be read is left out and the
    reason printed, because a blank reads as "none".
    """
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
        shown["tenant"] = me.get("tenant_id")
        shown["principal"] = shown["principal"] or me.get("email")
    except SwarmError as exc:
        shown["error"] = str(exc)
        code = EXIT_FAIL

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
        help="which configured deployment to use (see `sc context list`)",
        **default(None),
    )


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

    t = sub.add_parser("task", help="one agent: state, commits, patch, pull request")
    t.add_argument("task_id")
    _common(t, root=False)
    t.set_defaults(func=cmd_task)

    r = sub.add_parser("trouble", help="what is wrong right now (exit 3 if anything is down)")
    _common(r, root=False)
    r.set_defaults(func=cmd_trouble)

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
        help="its Desktop OAuth client id (needed for `sc login` at an IAP front door)",
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
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
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
