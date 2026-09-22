"""`swarm` -- dispatch, watch, apply, integrate. From a terminal or from Bash.

WHY A CLI AND AN MCP SERVER BOTH. An MCP tool call returns ONCE. It cannot
stream, so "watch this agent work" through a tool means blocking until the run
is over and then printing everything -- which is a transcript, not a tail. A
terminal command CAN stream, and Claude Code can run one in the background and
read the file as it grows. That is the only shape that actually feels like a
local agent, so it is the one `tail` takes. `server.py` wraps these same
primitives for everything that does not need to stream.

Exit codes are meant to be read by a script:
    0  the thing asked for happened
    1  it did not
    2  it happened, with conflicts a human has to resolve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

from . import workflows
from .auth import REACHES, WHY_NOT, Tier, detect
from .client import (
    SwarmClient,
    SwarmError,
    project_id,
    region,
    service_name,
    task_id_of,
)
from .patches import (
    apply_patch,
    describe_task,
    download,
    explain_absence,
    integrate,
    patch_uri,
)

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER", "DEAD_LETTERED"}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFLICT = 2


def _artifact_bucket() -> str:
    bucket = os.environ.get("SWARM_ARTIFACT_BUCKET", "").strip()
    if bucket:
        return bucket
    project = os.environ.get("PROJECT_ID", "").strip()
    if not project:
        raise SwarmError("set SWARM_ARTIFACT_BUCKET or PROJECT_ID to locate the logs")
    return f"swarm-artifacts-{project}"


def _live_log_uri(task: dict[str, Any], attempt_id: str, stream: str) -> str:
    # `task_id_of`, not `task["task_id"]`: the API names the field `id`, so
    # the subscript raised KeyError -- which is not a SwarmError and so escaped
    # every handler in this file as a traceback, mid-tail.
    return (
        f"gs://{_artifact_bucket()}/tenants/{task['tenant_id']}"
        f"/tasks/{task_id_of(task)}/attempts/{attempt_id}/logs/live/{stream}.tail.log"
    )


def _latest_attempt(
    client: SwarmClient, task_id: str
) -> tuple[str | None, SwarmError | None]:
    """`(attempt_id, None)`, `(None, None)` for "not started yet", or
    `(None, why)`.

    THREE OUTCOMES, NOT TWO. This swallowed the error and returned None, which
    `tail` reads as "no attempt has begun", so a route that 403s or times out
    produced a tail that printed events and NEVER printed a log line, with no
    indication that it had stopped being able to look. `sc` states this rule
    for its own fetchers -- a failure is never an empty result -- and this is
    the same rule one directory over.
    """
    try:
        data = client.request("GET", f"/v1/tasks/{task_id}/attempts?limit=1")
    except SwarmError as exc:
        return None, exc
    attempts = data.get("attempts") if isinstance(data, dict) else None
    if attempts is None:
        return None, SwarmError(
            f"GET /v1/tasks/{task_id}/attempts answered without an `attempts` field"
        )
    if not attempts:
        return None, None
    return attempts[0].get("attempt_id"), None


def _emit(prefix: str, text: str) -> None:
    for line in text.splitlines():
        print(f"[{prefix}] {line}", flush=True)


# -- commands --------------------------------------------------------------


def cmd_dispatch(client: SwarmClient, args) -> int:
    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        raise SwarmError("an empty prompt would dispatch an agent with nothing to do")
    task = client.dispatch(
        prompt=prompt,
        runner_profile=args.profile,
        repository_url=args.repo,
        repository_ref=args.ref,
        metadata={"unit": args.label} if args.label else None,
        timeout_seconds=args.timeout,
        model=args.model,
    )
    if args.json:
        print(json.dumps(task, indent=2))
    else:
        task_id = task_id_of(task)
        if not task_id:
            # A blank line here is the worst outcome: the shell pipes it into
            # `swarm tail` and gets an error about a task named "".
            raise SwarmError(
                "the API accepted the task but its response named no id: "
                f"{sorted(task)}"
            )
        print(task_id, flush=True)
    return EXIT_OK


def cmd_status(client: SwarmClient, args) -> int:
    for task_id in args.task_ids:
        task = client.task(task_id)
        print(f"{task_id}  {task.get('state')}  {task.get('runner_profile', '')}")
    return EXIT_OK


def cmd_tail(client: SwarmClient, args) -> int:
    """Follow several tasks at once until each reaches a terminal state.

    THE GAP HEADER IS NOT DECORATION. The worker publishes a bounded WINDOW of
    each stream, so a watcher that polls too slowly loses the middle. Each
    window carries the byte offset it starts at; when that offset jumps past
    what we have already shown, we say so. A tailer that quietly stitched two
    non-adjacent pieces of output together would print a transcript that never
    happened, which is worse than admitting the gap.
    """
    tasks = list(args.task_ids)
    seen_events: dict[str, set[str]] = {t: set() for t in tasks}
    shown_to: dict[tuple[str, str], int] = {}
    attempts: dict[str, str] = {}
    # Said ONCE per task, not once per poll: at three seconds an interval, a
    # repeated line would bury the agent's own output within a minute.
    warned: set[str] = set()
    done: set[str] = set()
    failed = False

    while len(done) < len(tasks):
        for task_id in tasks:
            if task_id in done:
                continue
            short = task_id[-8:]
            try:
                task = client.task(task_id)
            except SwarmError as exc:
                _emit(short, f"! {exc}")
                done.add(task_id)
                failed = True
                continue

            for event in client.events(task_id, limit=50):
                key = str(event.get("event_id") or f"{event.get('at')}{event.get('type')}")
                if key in seen_events[task_id]:
                    continue
                seen_events[task_id].add(key)
                kind = event.get("type", "?")
                if kind == "heartbeat" and not args.verbose:
                    continue
                _emit(short, f"· {kind}")

            attempt = attempts.get(task_id)
            if attempt is None:
                attempt, why = _latest_attempt(client, task_id)
                if why is not None and task_id not in warned:
                    # Not fatal -- the state polling above still works, so the
                    # tail can still report the outcome. But silence here is
                    # indistinguishable from an agent that has printed nothing,
                    # which is the reading an operator would take.
                    warned.add(task_id)
                    _emit(short, f"! logs unavailable: {why}")
            if attempt:
                attempts[task_id] = attempt
                for stream in ("stdout", "stderr"):
                    try:
                        blob = download(client, _live_log_uri(task, attempt, stream))
                    except SwarmError:
                        continue  # not published yet, or nothing written
                    header, _, body = blob.decode("utf-8", errors="replace").partition("\n")
                    offset = 0
                    if header.startswith("#swarm-tail "):
                        for field in header.split():
                            if field.startswith("offset="):
                                offset = int(field.split("=", 1)[1] or 0)
                    else:
                        body = header + "\n" + body
                    key = (task_id, stream)
                    already = shown_to.get(key, 0)
                    if offset > already and already > 0:
                        _emit(short, f"… {offset - already} bytes of {stream} were missed")
                        already = offset
                    fresh = body[max(0, already - offset):]
                    if fresh.strip():
                        _emit(short, fresh.rstrip("\n"))
                    shown_to[key] = offset + len(body)

            state = task.get("state")
            if state in TERMINAL:
                done.add(task_id)
                git = ((task.get("result_summary") or {}).get("git")) or {}
                summary = ""
                if git.get("commit_count"):
                    summary = (
                        f" · {git['commit_count']} commit(s) "
                        f"+{git.get('insertions', 0)}/-{git.get('deletions', 0)}"
                    )
                elif git.get("dirty_count"):
                    summary = f" · {git['dirty_count']} file(s) changed, uncommitted"
                pr = git.get("pull_request")
                if pr:
                    summary += f" · PR #{pr.get('number')}"
                _emit(short, f"{'OK' if state == 'SUCCEEDED' else state}{summary}")
                if state != "SUCCEEDED":
                    failed = True
        if len(done) < len(tasks):
            time.sleep(args.interval)
    return EXIT_FAIL if failed else EXIT_OK


def cmd_result(client: SwarmClient, args) -> int:
    task = client.task(args.task_id)
    if args.json:
        print(json.dumps(task.get("result_summary") or {}, indent=2))
        return EXIT_OK
    summary = task.get("result_summary") or {}
    git = summary.get("git") or {}
    print(f"{args.task_id}  {task.get('state')}")
    if not git:
        print(f"  code: {explain_absence(task)}")
        return EXIT_OK
    print(f"  base    {git.get('base') or '—'}")
    print(f"  commits {git.get('commit_count', 0)}  +{git.get('insertions', 0)}/-{git.get('deletions', 0)}")
    for commit in git.get("commits") or []:
        print(f"    {commit['sha'][:10]}  {commit['subject']}")
    if git.get("dirty_count"):
        print(f"  uncommitted {git['dirty_count']} file(s)")
    uri = patch_uri(task)
    print(f"  patch   {uri or explain_absence(task)}")
    pr = git.get("pull_request")
    print(f"  PR      {pr['url'] if pr else 'none — ' + str(git.get('publish_reason', 'no reason recorded'))}")
    return EXIT_OK


def cmd_apply(client: SwarmClient, args) -> int:
    repo = Path(args.into or ".").resolve()
    task = client.task(args.task_id)
    uri = patch_uri(task)
    if uri is None:
        print(f"nothing to apply: {explain_absence(task)}", file=sys.stderr)
        return EXIT_FAIL
    result = apply_patch(download(client, uri), repo, task_id=args.task_id)
    print(f"{args.task_id}: {result.detail}")
    for path in result.conflicted:
        print(f"  {path}")
    if not result.applied:
        return EXIT_FAIL
    return EXIT_CONFLICT if result.conflicted else EXIT_OK


def cmd_integrate(client: SwarmClient, args) -> int:
    repo = Path(args.into or ".").resolve()
    result = integrate(
        client, list(args.task_ids), repo, branch=args.branch, base=args.base
    )
    print(result.render())
    if any(not r.applied for r in result.results):
        return EXIT_FAIL
    return EXIT_CONFLICT if result.conflicts else EXIT_OK


def cmd_cancel(client: SwarmClient, args) -> int:
    for task_id in args.task_ids:
        client.cancel(task_id)
        print(f"{task_id} cancelled")
    return EXIT_OK


# -- workflows -------------------------------------------------------------
#
# The same three operations the MCP tools expose, through the same functions in
# `workflows.py`. A terminal command exists for each because a workflow is the
# thing most worth following live, and only a terminal command can stream: the
# ids printed by `swarm workflow` are what `swarm tail` takes.


def cmd_workflow(client: SwarmClient, args) -> int:
    """Submit a DAG read from a JSON file, or from stdin with `-`.

    A FILE RATHER THAN FLAGS. A DAG is a nested structure -- per-step prompts,
    dependency lists, an `input_from` map -- and every attempt to spell one in
    argv ends in a quoting bug that silently drops a dependency. The file is
    also the artifact a person edits and re-submits, which is what actually
    happens when the first run fails at one step.
    """
    raw = sys.stdin.read() if args.spec == "-" else Path(args.spec).read_text()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SwarmError(f"{args.spec} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SwarmError(f"{args.spec} must hold an object with a `steps` list")

    envelope = workflows.submit(
        client,
        steps=workflows.build_steps(document.get("steps")),
        strategy=args.strategy or document.get("strategy"),
        carrier=args.carrier or document.get("carrier"),
        repository_url=args.repo or document.get("repository_url"),
        repository_ref=args.ref or document.get("repository_ref"),
        on_step_failure=document.get("on_step_failure"),
        priority=document.get("priority"),
        label=args.label or document.get("label"),
    )
    workflow = envelope["workflow"]
    workflow_id = workflow.get("workflow_id")
    if not workflow_id:
        raise SwarmError(
            f"the API accepted the workflow but named no id: {sorted(workflow)}"
        )
    if args.json:
        print(json.dumps(envelope, indent=2))
        return EXIT_OK
    print(workflow_id)
    task_ids = []
    for step in workflow.get("steps") or []:
        task_id = step.get("task_id") or "—"
        task_ids.append(task_id)
        deps = ", ".join(step.get("depends_on") or []) or "—"
        print(f"  {step.get('step_id')}  {task_id}  after: {deps}")
    followable = [t for t in task_ids if t != "—"]
    if followable:
        print(f"  follow: swarm tail {' '.join(followable)}")
    return EXIT_OK


def cmd_workflow_status(client: SwarmClient, args) -> int:
    """Per-step state, and the DERIVED workflow state -- never the stored one.

    Exit 1 when no workflow state could be established, which is the case a
    script must be able to branch on: it means the server did not derive on this
    read and there is no answer to quote, not that the workflow is fine.
    """
    envelope = workflows.fetch(client, args.workflow_id)
    report = workflows.report(
        envelope, describe=describe_task if args.result else None
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return EXIT_FAIL if report.get("state") is None else EXIT_OK

    state = report.get("state") or "NO STATE"
    print(f"{report['workflow_id']}  {state}  (stored: {report.get('stored_state')})")
    if report.get("state_unavailable_because"):
        print(f"  ! {report['state_unavailable_because']}")
    if report.get("state_incomplete_because"):
        print(f"  ! {report['state_incomplete_because']}")
    for row in report["steps"]:
        print(f"  {row['step_id']}  {row.get('state') or '—'}  {row.get('task_id') or '—'}")
        for key in ("state_unavailable_because", "park_reason", "blocked_by", "error"):
            if row.get(key):
                print(f"      {key}: {row[key]}")
        produced = row.get("produced") or {}
        if produced:
            print(
                f"      commits {produced.get('commits')}  "
                f"patch {produced.get('patch') or produced.get('no_patch_because')}"
            )
    if report.get("follow_live_with"):
        print(f"  follow: {report['follow_live_with']}")
    return EXIT_FAIL if report.get("state") is None else EXIT_OK


def cmd_workflow_cancel(client: SwarmClient, args) -> int:
    result = workflows.cancel(client, args.workflow_id)
    print(f"{result.get('workflow_id')} cancel_requested")
    for task_id in result.get("tasks_cancelled") or []:
        print(f"  cancelled {task_id}")
    # Printed, not swallowed. A step that had already finished is not a failed
    # cancel, and an operator who cannot see the difference re-runs the cancel.
    for task_id in result.get("tasks_already_terminal") or []:
        print(f"  already terminal {task_id}")
    return EXIT_OK


# -- cluster state ---------------------------------------------------------
#
# `sc` is its own console script, because a status command wants a short name.
# It is ALSO reachable as `swarm sc ...` so that a session which already knows
# about `swarm` does not have to be told about a second binary. Both spellings
# parse with the same parser and run the same functions -- two implementations
# of "what is the cluster doing" is how a CLI and a tool start disagreeing.


#: Subcommands that belong to `sc`. They are routed on the first token, BEFORE
#: argparse sees the line, because `nargs=REMAINDER` is not enough on its own:
#: argparse still tries to match a LEADING option against the subparser, so
#: `swarm accounts` works while `swarm accounts --width 60` dies on
#: "unrecognized arguments". Routing on the token keeps both spellings honest
#: and leaves every pre-existing subcommand's path byte-for-byte unchanged.
SC_COMMANDS = ("sc", "accounts", "agents", "capacity", "trouble")


def _delegate_to_sc(raw: list[str]) -> int:
    from . import sc as _sc

    # `swarm sc accounts` and `swarm accounts` must mean the same thing, so the
    # bare `sc` word is dropped and everything else is passed through verbatim.
    return _sc.main(raw[1:] if raw and raw[0] == "sc" else raw)


def cmd_sc(_client, args) -> int:
    """The argparse route into `sc`, which `main()` normally gets to first.

    It registers the subcommands so `swarm --help` lists them, and it runs the
    SAME delegation rather than a second copy of it. An earlier version parsed
    the line again here and called the command function directly with the
    caller's client -- a second implementation of the routing, differing from
    the live one in which client it used and in whether `sc`'s exit codes
    survived, and unreachable, so whichever of the two a reader edited there
    was an even chance it was the dead one.

    It takes no client for the same reason: `sc` builds and closes its own.
    """
    return _delegate_to_sc([args.command] + list(args.argv))


# -- setup and diagnosis ---------------------------------------------------
#
# These two exist because of how this platform fails for a NEWCOMER. Every
# other command assumes a working connection; when there is not one, the error
# is "Invalid JWT audience" or a 403 with an HTML body, and neither says the
# true thing, which is usually "your laptop cannot mint that kind of token, and
# here is the one that it can".


def cmd_doctor(_client, args) -> int:
    """Say which tier this machine is on and what that tier can reach."""
    detection = detect()
    print(f"tier        {detection.tier.value}")
    print(f"            {detection.detail}")
    print(f"reaches     {', '.join(REACHES[detection.tier])} deployments")

    for profile in ("solo", "team"):
        why = WHY_NOT.get((detection.tier, profile))
        if why:
            # WRAPPED, because these explanations are paragraphs. Printed raw
            # they are one 400-column line, and the remedy -- which is the
            # last sentence -- is the part that scrolls off.
            print(
                textwrap.fill(
                    why,
                    width=78,
                    initial_indent=f"not {profile:<8}",
                    subsequent_indent=" " * 12,
                    # These sentences name hostnames, service names and
                    # environment variables. Split on a hyphen or mid-word,
                    # `swarm-api` becomes `swarm-\napi`, which is not a thing
                    # anyone can then search for.
                    break_on_hyphens=False,
                    break_long_words=False,
                )
            )

    for name, verdict in detection.considered:
        print(f"  checked   {name}: {verdict}")

    print()
    try:
        print(f"project     {project_id()}")
        print(f"service     {service_name()} / {region()}")
    except SwarmError as exc:
        print(f"project     UNKNOWN -- {exc}")
        return EXIT_FAIL

    # The reachability check is last and is allowed to fail: everything above
    # is still worth printing when the API is down, and is exactly what someone
    # needs in order to say WHY it is down.
    try:
        with SwarmClient() as client:
            endpoint = client.base_url
            me = client.request("GET", "/v1/tenants/me")
            print(f"api         {endpoint}")
            print(f"identity    {me.get('email') or '(not reported)'}")
            print(f"tenant      {me.get('tenant_id')}")
            groups = me.get("groups") or []
            print(f"groups      {', '.join(groups) if groups else 'none -- personal tenant'}")
            print(f"admin       {me.get('is_admin')}")
    except SwarmError as exc:
        print("api         UNREACHABLE")
        for line in str(exc).split(" -- "):
            print(f"            {line.strip()}")
        return EXIT_FAIL
    return EXIT_OK


def cmd_init(_client, args) -> int:
    """First run: check what is present, write .env, name what is missing."""
    ok = True

    account = ""
    try:
        account = _run_quiet(["gcloud", "config", "get-value", "account"])
    except SwarmError:
        pass
    if account and account != "(unset)":
        print(f"  ok   authenticated as {account}")
    else:
        print("  --   not authenticated. Run: gcloud auth login")
        ok = False

    project = ""
    try:
        project = project_id()
        print(f"  ok   project {project}")
    except SwarmError as exc:
        print(f"  --   {exc}")
        ok = False

    url = ""
    if project:
        try:
            from .client import resolve_api_url

            url = resolve_api_url()
            print(f"  ok   {service_name()} found at {url}")
        except SwarmError as exc:
            print(f"  --   {service_name()} not found in {region()}: {exc}")
            print("       deploy it first, then run `swarm init` again")
            ok = False

    detection = detect()
    print(f"  ok   tier {detection.tier.value} (reaches {', '.join(REACHES[detection.tier])})")

    if not ok:
        return EXIT_FAIL

    target = Path(args.write or ".env")
    lines = [
        "# Written by `swarm init`. Safe to commit? NO -- it names your project.",
        f"PROJECT_ID={project}",
        f"REGION={region()}",
        f"API_SERVICE={service_name()}",
    ]
    # SWARM_API_URL is deliberately NOT written on the proxy tier: the proxy
    # binds a fresh port every run, so a recorded URL would be wrong by the
    # next invocation and would silently take precedence over starting one.
    if detection.tier is not Tier.PROXY:
        lines.append(f"SWARM_API_URL={url}")
    if target.exists() and not args.force:
        print(f"  --   {target} exists; not overwriting (use --force)")
        print()
        print("\n".join(lines))
        return EXIT_OK
    target.write_text("\n".join(lines) + "\n")
    print(f"  ok   wrote {target}")
    print()
    print("Try:  swarm dispatch \"say hello\" --profile mock")
    return EXIT_OK


def _run_quiet(argv):
    from .client import _run

    return _run(argv)


# -- entry point -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("dispatch", help="submit one task")
    d.add_argument("prompt", help="the agent's instructions, or - to read stdin")
    d.add_argument("--profile", default="claude-code")
    d.add_argument("--repo", default=os.environ.get("SWARM_REPO") or None)
    d.add_argument("--ref", default=None)
    d.add_argument("--label", default=None, help="recorded as metadata.unit")
    d.add_argument("--model", default=None)
    d.add_argument("--timeout", type=int, default=None)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("status", help="one line per task")
    s.add_argument("task_ids", nargs="+")
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("tail", help="follow logs and events until every task finishes")
    t.add_argument("task_ids", nargs="+")
    t.add_argument("--interval", type=float, default=3.0)
    t.add_argument("--verbose", action="store_true", help="include heartbeat events")
    t.set_defaults(func=cmd_tail)

    r = sub.add_parser("result", help="what a finished task produced")
    r.add_argument("task_id")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_result)

    a = sub.add_parser("apply", help="apply one task's patch to a working tree")
    a.add_argument("task_id")
    a.add_argument("--into", default=None, help="repository to apply into (default: cwd)")
    a.set_defaults(func=cmd_apply)

    i = sub.add_parser("integrate", help="put several tasks' work on one branch, in order")
    i.add_argument("task_ids", nargs="+")
    i.add_argument("--branch", required=True)
    i.add_argument("--base", default=None, help="commit or branch to start from")
    i.add_argument("--into", default=None)
    i.set_defaults(func=cmd_integrate)

    c = sub.add_parser("cancel", help="cancel running tasks")
    c.add_argument("task_ids", nargs="+")
    c.set_defaults(func=cmd_cancel)

    w = sub.add_parser("workflow", help="submit a DAG of agents from a JSON spec")
    w.add_argument("spec", help="path to the workflow JSON, or - to read stdin")
    w.add_argument("--repo", default=os.environ.get("SWARM_REPO") or None)
    w.add_argument("--ref", default=None)
    w.add_argument("--strategy", default=None)
    w.add_argument("--carrier", default=None)
    w.add_argument("--label", default=None)
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_workflow)

    ws = sub.add_parser(
        "workflow-status", help="per-step state, and the DERIVED workflow state"
    )
    ws.add_argument("workflow_id")
    ws.add_argument(
        "--result", action="store_true", help="also show what each step produced"
    )
    ws.add_argument("--json", action="store_true")
    ws.set_defaults(func=cmd_workflow_status)

    wc = sub.add_parser("workflow-cancel", help="cancel a workflow and its steps")
    wc.add_argument("workflow_id")
    wc.set_defaults(func=cmd_workflow_cancel)

    sc_cmd = sub.add_parser(
        "sc", help="cluster state: accounts, capacity, agents, trouble (same as the `sc` command)"
    )
    sc_cmd.add_argument(
        "argv", nargs=argparse.REMAINDER, help="an `sc` subcommand and its flags"
    )
    sc_cmd.set_defaults(func=cmd_sc, no_client=True)

    for alias in ("accounts", "agents", "capacity", "trouble"):
        alias_parser = sub.add_parser(alias, help=f"shorthand for `sc {alias}`")
        alias_parser.add_argument("argv", nargs=argparse.REMAINDER)
        alias_parser.set_defaults(func=cmd_sc, no_client=True)

    doc = sub.add_parser("doctor", help="which auth tier this machine is on, and what it reaches")
    doc.set_defaults(func=cmd_doctor, no_client=True)

    ini = sub.add_parser("init", help="first-run setup: check, write .env, say what is missing")
    ini.add_argument("--write", default=None, help="where to write (default: .env)")
    ini.add_argument("--force", action="store_true")
    ini.set_defaults(func=cmd_init, no_client=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in SC_COMMANDS:
        # `sc` owns its own error handling and exit codes (3 means "something
        # is down"), so this hands the whole line over rather than wrapping it.
        return _delegate_to_sc(raw)

    args = build_parser().parse_args(argv)
    try:
        # `doctor` and `init` exist to explain why a connection cannot be made,
        # so making one first would be the one thing guaranteed to stop them
        # running when they are needed.
        if getattr(args, "no_client", False):
            return args.func(None, args)
        with SwarmClient() as client:
            return args.func(client, args)
    except SwarmError as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
