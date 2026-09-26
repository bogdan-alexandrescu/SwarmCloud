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

Anywhere a command takes task ids -- `status`, `tail`, `follow` -- a workflow
id (wf_...) stands for every step of that workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import profiles as catalogue
from . import workflows
from .auth import REACHES, WHY_NOT, Tier, detect
from .client import (
    TERMINAL,
    SwarmClient,
    SwarmError,
    project_id,
    region,
    service_name,
    task_id_of,
)
from .follow import (
    DEFAULT_LOG_BUDGET,
    NEVER_STARTED,
    NO_ATTEMPT_YET,
    NO_LOG_YET,
    STREAMS,
    empty_log_line,
    event_type,
    follow,
    follow_command,
    no_log_line,
    render,
    settled,
    terminal_command,
    terminal_line,
    time_order,
)
from .invocation import help_command
from .patches import (
    apply_patch,
    describe_task,
    download,
    explain_absence,
    explain_failure,
    integrate,
    masked_counts,
    masked_words,
    object_size,
    patch_uri,
)
from .render import (
    clock,
    describe_blockers,
    principal_of,
    task_label,
    tenant_of,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFLICT = 2


def _artifact_bucket() -> str:
    """The bucket the live logs are in, or a SwarmError that says how to name it.

    RESOLVED THE WAY `doctor` RESOLVES THE PROJECT: SWARM_ARTIFACT_BUCKET, else
    the bucket named for the project -- PROJECT_ID, or gcloud's configured
    project, through `client.project_id`. It read PROJECT_ID alone, and the
    error it raised when that was unset was swallowed by `tail` as "not
    published yet", so a machine with no project configured tailed events and
    never a log line, with no warning (#88, SC-F12).
    """
    bucket = os.environ.get("SWARM_ARTIFACT_BUCKET", "").strip()
    if bucket:
        return bucket
    try:
        project = project_id()
    except SwarmError as exc:
        raise SwarmError(
            "cannot locate the logs: set SWARM_ARTIFACT_BUCKET, or a project -- "
            "PROJECT_ID or `gcloud config set project` -- whose "
            f"swarm-artifacts-<project> bucket holds them ({exc})"
        ) from exc
    return f"swarm-artifacts-{project}"


def _logs_prefix(task: dict[str, Any], attempt_id: str, bucket: str | None) -> str:
    # `task_id_of`, not `task["task_id"]`: the API names the field `id`, so
    # the subscript raised KeyError -- which is not a SwarmError and so escaped
    # every handler in this file as a traceback, mid-tail.
    return (
        f"gs://{bucket or _artifact_bucket()}/tenants/{task['tenant_id']}"
        f"/tasks/{task_id_of(task)}/attempts/{attempt_id}/logs"
    )


def _live_log_uri(
    task: dict[str, Any], attempt_id: str, stream: str, *, bucket: str | None = None
) -> str:
    """The window `lifecycle._publish_live_logs` republishes while the agent runs."""
    return f"{_logs_prefix(task, attempt_id, bucket)}/live/{stream}.tail.log"


def _completed_log_uri(
    task: dict[str, Any], attempt_id: str, stream: str, *, bucket: str | None = None
) -> str:
    """The whole stream, which `lifecycle._upload_outputs` writes at exit."""
    return f"{_logs_prefix(task, attempt_id, bucket)}/{stream}.log"


def _finished_quietly(
    client: SwarmClient,
    task: dict[str, Any],
    attempt: str,
    bucket: str,
    *,
    live_found: bool,
    live_unreadable: bool,
) -> str:
    """The one line for a finished task whose live log showed nothing -- what
    was OBSERVED, and nothing more.

    NO LIVE LOG IS NOT MISSING OUTPUT. `lifecycle._publish_live_logs` publishes
    first one `LIVE_LOG_INTERVAL_SECONDS` (5 by default) after the agent
    starts, never at exit, and never for a stream with no bytes. Three kinds of
    run leave no live log: an agent that printed nothing, a run that fit inside
    the first interval, and an agent that printed only after its last flush.
    `tail` printed "its output is MISSING, not empty" for all three. The
    completed log (`logs/<stream>.log`, written by `_upload_outputs` at exit)
    tells the empty run from the other two. Its SIZE is read from the
    object's metadata, because it can be 32 MB and one number is the answer.

    Its contents are NOT printed here. `swarm follow` reads the completed log
    through the API, which redacts at read time. That is the path for output
    this tail never saw, and the line names it.

    EVERY LINE NAMES THE ATTEMPT IT DESCRIBES. What is read here is one
    attempt's objects, under its own `attempts/<id>/logs` prefix, and a task
    can have several. Only the no-object line named one, so the rest read as
    a verdict on the whole task -- and `tail` used to pass in the first
    attempt it saw, which after a quota park or a retry is not the one the
    task finished on. `tail` now passes the newest, read on the poll that saw
    the terminal state.
    """
    task_id = task_id_of(task)
    if live_unreadable:
        # The warning above has the status. A read that failed says nothing
        # about the output, so this line does not either.
        return (
            f"whether attempt {attempt} printed anything is unknown -- its logs could "
            "not be read"
        )
    if live_found:
        return f"attempt {attempt} printed nothing -- its live log is empty"
    held: dict[str, int] = {}
    for stream in STREAMS:
        try:
            held[stream] = object_size(
                client, _completed_log_uri(task, attempt, stream, bucket=bucket)
            )
        except SwarmError as exc:
            if exc.status == 404:
                continue
            return (
                f"whether attempt {attempt} printed anything is unknown -- its completed "
                f"log could not be read: {exc}"
            )
    if not held:
        return no_log_line(task_id, attempt)
    if not any(held.values()):
        # `follow`'s sentence for the same fact (#193). That an empty stream
        # gets no live log is why this branch reads the completed log at all,
        # and is said above; the reader needs the fact.
        return empty_log_line(attempt)
    sizes = " and ".join(f"{count} bytes of {stream}" for stream, count in held.items() if count)
    return (
        f"attempt {attempt} has no live log, but its completed log holds {sizes} -- the "
        "worker publishes the live log every 5 s by default, not at exit, so output "
        f"written after its last flush never reaches one. Read it with: "
        f"{terminal_command(f'swarm follow {task_id}')}"
    )


def _expand(client: SwarmClient, given: list[str]) -> list[str]:
    """Task ids, with each workflow id (`wf_...`) replaced by its steps' tasks.

    `status`, `tail` and `follow` took only task ids, and a workflow id -- the
    one id `swarm workflow` prints first -- answered a bare 404 from
    `/v1/tasks/wf_...` (#88, SC-F17). A workflow is the thing most worth
    following live, so its id now means "every step of it". Duplicates are
    dropped, in the order given.
    """
    out: list[str] = []
    for item in given:
        if str(item).startswith("wf_"):
            envelope = workflows.fetch(client, str(item))
            steps = envelope["workflow"].get("steps") or []
            found = [str(step["task_id"]) for step in steps if step.get("task_id")]
            if not found:
                raise SwarmError(f"{item} has no step with a task id to read")
            out += [task_id for task_id in found if task_id not in out]
        elif item not in out:
            out.append(item)
    return out


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
    # The SAME refusal the MCP tool makes, through the same function. A
    # terminal that accepted `--profile codex` while the tool refused it would
    # be two answers to one question, which is the defect this repository
    # keeps paying for.
    # The label is prefixed onto the refusal -- "swarm dispatch: there is no
    # runner profile called 'claude'", spelled by `terminal_command` for this
    # install -- so it is spelled to run, like every other command string this
    # package emits. The MCP side passes `swarm_dispatch`, the TOOL name, which
    # is right there for the same reason: a reader must be able to act on what
    # they are shown.
    where = terminal_command("swarm dispatch")
    profile = catalogue.check(args.profile, where=where)
    # `--input KEY=VALUE`: only what this profile declares, typed by the
    # declaration, and refused before anything travels (#142).
    inputs = catalogue.parse_input_flags(profile, getattr(args, "inputs", None), where=where)
    task = client.dispatch(
        prompt=prompt,
        runner_profile=profile,
        repository_url=args.repo,
        repository_ref=args.ref,
        metadata={"unit": args.label} if args.label else None,
        timeout_seconds=args.timeout,
        model=args.model,
        inputs=inputs or None,
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
    # `masked N` (owner decision, 2026-09-26): the API serves the task's input
    # and metadata masked, and this is how many credential-shaped strings it
    # masked in them; `masked —` from a deployment that sent no count.
    for task_id in _expand(client, list(args.task_ids)):
        task = client.task(task_id)
        step = f"  step {task['step_id']}" if task.get("step_id") else ""
        print(
            f"{task_id}  {task.get('state')}  {task.get('runner_profile', '')}{step}"
            f"  {masked_words(task)}"
        )
    return EXIT_OK


def _masked_line(task: dict) -> str:
    """`swarm result`'s masking line: the total, then the input's and the metadata's.

    The input and the metadata are served masked (owner decision, 2026-09-26);
    a count the API did not send is an em dash, never 0.
    """
    counts = masked_counts(task)
    parts = " · ".join(
        f"{name} {'—' if n is None else n}" for name, n in (("input", counts["input"]), ("metadata", counts["metadata"]))
    )
    return f"  {masked_words(task)}  ({parts})"


def cmd_tail(client: SwarmClient, args) -> int:
    """Follow several tasks at once until each reaches a terminal state.

    THE GAP HEADER IS NOT DECORATION. The worker publishes a bounded WINDOW of
    each stream, so a watcher that polls too slowly loses the middle. Each
    window carries the byte offset it starts at; when that offset jumps past
    what we have already shown, we say so. A tailer that quietly stitched two
    non-adjacent pieces of output together would print a transcript that never
    happened, which is worse than admitting the gap.
    """
    tasks = _expand(client, list(args.task_ids))
    seen_events: dict[str, set[str]] = {t: set() for t in tasks}
    shown_to: dict[tuple[str, str], int] = {}
    # Per task: the newest attempt, looked up again on EVERY poll (see below).
    # `shown_to`, `found_log` and `unreadable` all describe this attempt, and
    # are cleared when it changes.
    attempts: dict[str, str] = {}
    labels: dict[str, str] = {t: task_label(t) for t in tasks}
    # Said ONCE per task, not once per poll: at three seconds an interval, a
    # repeated line would bury the agent's own output within a minute. Keyed
    # by (task, what), so one warning cannot silence a different one.
    warned: set[tuple[str, str]] = set()
    # Per task: whether any live log object was FOUND. Beside `shown_to`, it is
    # what tells an agent that printed nothing from a log nobody published.
    found_log: set[str] = set()
    # Per task, as of its LATEST poll: a read that failed. A failed read says
    # nothing about the output, so the finished line must not either -- and a
    # read that failed once and then worked is not held against the task.
    unreadable: set[str] = set()
    # Per task, as of its LATEST poll: the attempts read failed. With no
    # attempt stored, whether the task started is unknown. With one stored,
    # whether a later attempt ran is unknown.
    attempts_failed: set[str] = set()
    # The bucket, resolved once per tail and only once an attempt exists --
    # `[value]` or `[error]` -- because resolving it may ask gcloud.
    bucket: list[Any] = []
    done: set[str] = set()
    failed = False

    def warn_once(task_id: str, what: str, text: str) -> None:
        if (task_id, what) not in warned:
            warned.add((task_id, what))
            _emit(labels[task_id], text)

    def resolve_bucket() -> str | None:
        if not bucket:
            try:
                bucket.append(_artifact_bucket())
            except SwarmError as exc:
                bucket.append(exc)
        return bucket[0] if isinstance(bucket[0], str) else None

    while len(done) < len(tasks):
        polled: dict[str, dict[str, Any]] = {}
        fresh: list[tuple[tuple[bool, datetime], str, dict[str, Any], str]] = []
        for task_id in tasks:
            if task_id in done:
                continue
            try:
                task = client.task(task_id)
            except SwarmError as exc:
                _emit(labels[task_id], f"! {exc}")
                done.add(task_id)
                failed = True
                continue
            # `[b 4674b39f]` for a workflow step: the step id is the name the
            # reader wrote in their spec (#88, SC-F15).
            labels[task_id] = task_label(task_id, task.get("step_id"))
            polled[task_id] = task

            for event in client.events(task_id, limit=50):
                key = str(event.get("event_id") or f"{event.get('at')}{event.get('type')}")
                if key in seen_events[task_id]:
                    continue
                seen_events[task_id].add(key)
                # `event_type`, not the raw field: an API older than this
                # plugin serves a stored cancel REQUEST as `cancelled`.
                kind = event_type(event) or "?"
                if kind == "heartbeat" and not args.verbose:
                    continue
                fresh.append((time_order(event.get("at")), task_id, event, kind))

        # ONE ORDER ACROSS TASKS, the platform's. Printed task by task, the
        # events of one poll came out in the order of the argument list, so a
        # dependent step's cancel printed above the upstream failure that
        # caused it (#88, SC-F9). Sorted by `at`, stably, and each carries its
        # time so an order across polls can be read off the screen too. The
        # key is `follow.time_order`, which `swarm follow` sorts by too (#194).
        fresh.sort(key=lambda item: item[0])
        for _, task_id, event, kind in fresh:
            _emit(labels[task_id], f"{clock(event.get('at'))} · {kind}")

        for task_id, task in polled.items():
            label = labels[task_id]
            # EVERY POLL, NOT ONCE. A task moves to a new attempt when a quota
            # park ends one and the resume starts the next (invariant 4), and
            # on a retry after a crash or a reclaim. A slow interval can see
            # RUNNING on both sides of the move, so no state says when to look.
            # Each attempt publishes under its own prefix. `tail` looked once
            # and kept the first id, so it polled the old attempt's objects
            # while the new one published, and its finished line explained the
            # task from the wrong attempt's logs. The task was read ABOVE,
            # before this, so on the poll that reads a terminal state this is
            # the attempt the task finished on.
            stored = attempts.get(task_id)
            latest, why = _latest_attempt(client, task_id)
            attempts_failed.discard(task_id)
            if why is not None:
                # Not fatal -- the state polling above still works, so the
                # tail can still report the outcome. But silence here is
                # indistinguishable from an agent that has printed nothing,
                # which is the reading an operator would take.
                attempts_failed.add(task_id)
                if stored:
                    # The stored attempt is still read below. What is lost is
                    # knowing whether it is still the newest.
                    warn_once(
                        task_id,
                        f"latest:{stored}",
                        f"! cannot tell whether {stored} is still the latest attempt: {why}",
                    )
                else:
                    warn_once(task_id, "attempts", f"! logs unavailable: {why}")
            elif latest and stored and latest != stored:
                # Said, as `follow._logs` says it, rather than left to be read
                # off output that starts again from nothing. Every position
                # held was measured against the old attempt's objects, and the
                # new attempt's streams start at zero.
                _emit(
                    label,
                    f"attempt {latest} is now the latest one, not {stored}: what follows "
                    f"is {latest}'s output, not a continuation of {stored}'s",
                )
                for stream in STREAMS:
                    shown_to.pop((task_id, stream), None)
                found_log.discard(task_id)
                unreadable.discard(task_id)
            if latest:
                attempts[task_id] = latest
            attempt = attempts.get(task_id)
            where = resolve_bucket() if attempt else None
            if attempt and where is None:
                # A CONFIGURATION error, said. It was raised inside the read
                # below and swallowed there as "not published yet".
                warn_once(task_id, "bucket", f"! logs unavailable: {bucket[0]}")
            if attempt and where:
                unreadable.discard(task_id)
                for stream in STREAMS:
                    try:
                        blob = download(client, _live_log_uri(task, attempt, stream, bucket=where))
                    except SwarmError as exc:
                        if exc.status == 404:
                            # Not published yet -- or never: the worker skips
                            # an empty stream and does not flush at exit. The
                            # finished line below reads the completed log to
                            # tell those apart.
                            continue
                        # Anything else -- a 403 on the tenant's prefix, a
                        # timeout -- is a read that FAILED, and an agent that
                        # printed nothing looks exactly like it unless it is
                        # said. Once per attempt: the URI in it names one.
                        warn_once(task_id, f"read:{attempt}", f"! logs unreadable: {exc}")
                        unreadable.add(task_id)
                        continue
                    found_log.add(task_id)
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
                        _emit(label, f"… {offset - already} bytes of {stream} were missed")
                        already = offset
                    new_text = body[max(0, already - offset):]
                    if new_text.strip():
                        _emit(label, new_text.rstrip("\n"))
                    shown_to[key] = offset + len(body)

            state = task.get("state")
            printed = any(shown_to.get((task_id, stream), 0) > 0 for stream in STREAMS)
            quiet = not printed and not attempt and task_id not in attempts_failed
            if quiet and state not in TERMINAL:
                # ONE line, not one a poll, and not nothing: nothing is what an
                # agent that printed nothing ALSO looks like (#88, SC-F19).
                warn_once(task_id, "quiet", NO_ATTEMPT_YET)
            if (
                attempt
                and where
                and state not in TERMINAL
                and not printed
                and task_id not in found_log
                and task_id not in unreadable
                and task_id not in attempts_failed
            ):
                # An attempt, and every live object it would publish answered
                # 404. `follow` says so; `tail` printed nothing for this state
                # (#193), which is also what an agent that printed nothing
                # looks like. Once per attempt: a resume is a new one.
                warn_once(task_id, f"nolog:{attempt}", NO_LOG_YET)
            if state in TERMINAL:
                done.add(task_id)
                if not printed:
                    if not attempt and task_id in attempts_failed:
                        # "No attempt" is an ANSWER from the attempts route;
                        # a failed read of it is not, and was warned above.
                        _emit(
                            label,
                            "whether it started is unknown -- its attempts could not be read",
                        )
                    elif not attempt:
                        _emit(label, NEVER_STARTED)
                    elif task_id in attempts_failed:
                        # The attempt read is known. Whether the task finished
                        # on it is not, so a cause taken from its logs could
                        # be the stale-attempt explanation all over again.
                        _emit(
                            label,
                            f"nothing was shown from attempt {attempt}, and whether a later "
                            "attempt ran is unknown -- its attempts could not be read",
                        )
                    elif where:
                        _emit(
                            label,
                            _finished_quietly(
                                client,
                                task,
                                attempt,
                                where,
                                live_found=task_id in found_log,
                                live_unreadable=task_id in unreadable,
                            ),
                        )
                # The state as every screen spells it, and what the task
                # produced -- `follow`'s closing line, from the same function
                # (#193). `tail` printed `OK` here.
                git = ((task.get("result_summary") or {}).get("git")) or {}
                _emit(label, terminal_line(state, git))
                if state != "SUCCEEDED":
                    failed = True
        if len(done) < len(tasks):
            time.sleep(args.interval)
    return EXIT_FAIL if failed else EXIT_OK


def cmd_follow(client: SwarmClient, args) -> int:
    """The same answer `swarm_follow` gives a model, printed for a person.

    WHY BOTH THIS AND `tail`. They read different things and neither is
    redundant. `tail` reads the GCS objects DIRECTLY with the operator's own
    credentials, which is the only thing that works when the API is the part
    that is broken. `follow` goes through `GET /v1/tasks/{id}/logs`, which
    redacts at read time and enforces the tenant boundary -- and which is the
    only path an MCP tool has. Having the command print the tool's own report
    is deliberate: when a session says an agent produced nothing, this is how a
    person checks whether that was true, against the same bytes.

    It deliberately does NOT re-derive anything. `follow` builds the report and
    `render` turns it into lines; a second answer to "what is new" living here
    is how a CLI and a tool start disagreeing.

    A FINISHED TASK IS PRINTED AS FINISHED ONCE, AND THEN LEFT ALONE. Every
    poll used to re-read every task and reprint every finished one's terminal
    line -- 111 lines for five steps, one of them 20 times (#88, SC-F8). A task
    leaves the poll once `follow.settled` says nothing of it is still owed, and
    `render`'s memo keeps the lines that describe a task rather than something
    new about it to one each.
    """
    pending = _expand(client, list(args.task_ids))
    cursor: dict[str, Any] = {}
    memo: dict[str, set[str]] = {}
    last: dict[str, dict[str, Any]] = {}
    while pending:
        report = follow(
            client,
            pending,
            cursor=cursor,
            max_log_bytes=args.max_log_bytes,
            include_heartbeats=args.verbose,
        )
        cursor.update(report["cursor"])
        for line in render(report, memo=memo):
            print(line, flush=True)
        for task in report["tasks"]:
            last[task["task_id"]] = task
        pending = [task["task_id"] for task in report["tasks"] if not settled(task)]
        if args.once or not pending:
            break
        time.sleep(args.interval)

    # A task that could not be READ is a failure even though it never reached a
    # terminal state -- reporting exit 0 for it would tell a script the run was
    # fine because nothing said otherwise. `last` holds each task's final
    # report, including the ones that left the poll early.
    failed = any(
        task["read"] == "failed"
        or (task["terminal"] and task["state"] != "SUCCEEDED")
        for task in last.values()
    )
    return EXIT_FAIL if failed else EXIT_OK


def _print_failure(client: SwarmClient, task: dict[str, Any]) -> None:
    """The per-attempt detail, printed only when the task failed.

    The SAME function the MCP tool calls. A terminal that explained a failure
    differently from the tool would be two accounts of one dead agent.
    """
    failure = explain_failure(client, task)
    if failure is None:
        return
    if failure.get("attempts_unreadable"):
        print(f"  attempts  {failure['attempts_unreadable']}")
        return
    if failure.get("note"):
        print(f"  attempts  {failure['note']}")
        return
    last = failure.get("last_attempt") or {}
    # `exit_code_note` carries the "this is UNKNOWN, not 0" sentence, and a
    # bare `—` in its place would be the very substitution it exists to stop.
    exit_code = last.get("exit_code")
    shown = str(exit_code) if exit_code is not None else "not recorded (UNKNOWN, not 0)"
    print(f"  attempt {last.get('attempt_id') or '—'}  gen {last.get('generation')}")
    print(f"    backend  {last.get('backend') or '—'}  exit {shown}")
    if last.get("execution_name"):
        print(f"    execution {last['execution_name']}")
    if last.get("oom_near_miss"):
        print("    OOM near miss -- the run came close to its memory limit")
    if last.get("error"):
        print(f"    error    {last['error']}")
    for earlier in failure.get("earlier_attempts") or []:
        print(
            f"    earlier  gen {earlier.get('generation')} "
            f"exit {earlier.get('exit_code')} {earlier.get('error') or ''}".rstrip()
        )


def _upstream_steps(client: SwarmClient, task: dict[str, Any]) -> list[str]:
    """The upstream steps a cascade-cancelled step was cancelled FOR.

    The scheduler cancels a step whose dependency failed with `last_error` "an
    upstream workflow step did not succeed" and a `cancelled` event whose
    detail names the parents as `failed_parents` -- task ids. `swarm result`
    printed neither, so a step cancelled because of another one read as a step
    that simply stopped (#88, SC-F10). The newest events are asked for first
    (`order=desc`, which the route serves); each parent is read once, for its
    step id and state.
    """
    task_id = task_id_of(task)
    try:
        data = client.request("GET", f"/v1/tasks/{task_id}/events?limit=50&order=desc")
    except SwarmError as exc:
        return [f"could not be read: {exc}"]
    events = data.get("events") if isinstance(data, dict) else None
    parents: list[str] = []
    for event in events or []:
        detail = event.get("detail")
        if (
            event_type(event) == "cancelled"
            and isinstance(detail, dict)
            and detail.get("failed_parents")
        ):
            parents = [str(parent) for parent in detail["failed_parents"]]
            break
    out: list[str] = []
    for parent in parents:
        try:
            upstream = client.task(parent)
        except SwarmError as exc:
            out.append(f"{parent}  (could not be read: {exc})")
            continue
        out.append(f"{upstream.get('step_id') or '?'}  {upstream.get('state')}  {parent}")
    return out


def cmd_result(client: SwarmClient, args) -> int:
    task = client.task(args.task_id)
    if args.json:
        print(json.dumps(task.get("result_summary") or {}, indent=2))
        return EXIT_OK
    summary = task.get("result_summary") or {}
    git = summary.get("git") or {}
    state = task.get("state")
    print(f"{args.task_id}  {state}")
    print(_masked_line(task))
    if task.get("step_id"):
        print(f"  step    {task['step_id']} of {task.get('workflow_id') or '?'}")
    # WHY IT ENDED, as `sc task` prints it. `swarm result` never said why a
    # step was cancelled, although the task document carries it (#88, SC-F10).
    if task.get("last_error"):
        print(f"  why     {task['last_error']}")
    if state == "CANCELLED" and task.get("workflow_id"):
        for line in _upstream_steps(client, task):
            print(f"  upstream {line}")
    _print_failure(client, task)
    if not git:
        print(f"  code: {explain_absence(task)}")
        return EXIT_OK
    print(f"  base    {git.get('base') or '—'}")
    # ABSENT, NOT 0 (#191). A `git` summary that carries only the harvest's
    # error counted nothing, and `commits 0 +0/-0` is a measurement. The mark
    # is what `sc task` prints for the same summary.
    commits = git.get("commit_count")
    if commits is None:
        print("  commits —")
    else:
        print(f"  commits {commits}  +{git.get('insertions', 0)}/-{git.get('deletions', 0)}")
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


#: How often `swarm cancel --wait` re-reads a task it is waiting on.
_CANCEL_POLL_SECONDS = 2.0


def _wait_until_stopped(client: SwarmClient, task_id: str, seconds: float) -> str | None:
    """Poll one task until it is terminal or `seconds` pass. Returns its last state."""
    deadline = time.monotonic() + seconds
    while True:
        state = client.task(task_id).get("state")
        remaining = deadline - time.monotonic()
        if state in TERMINAL or remaining <= 0:
            return state
        time.sleep(min(_CANCEL_POLL_SECONDS, remaining))


def cmd_cancel(client: SwarmClient, args) -> int:
    """Cancel, and say which of the two things the API did.

    A TASK HOLDING CAPACITY IS NOT CANCELLED BY THIS CALL. The route cancels an
    idle task outright, but only FLAGS one that is LEASED, DISPATCHED,
    STARTING or RUNNING: its worker or the reconciler releases the lease and
    writes CANCELLED later, because freeing the slot from here would free one a
    live container still occupies (invariant 1). This printed "<id> cancelled"
    for both -- on 2026-09-25 (#88, SC-F1) for a task that `swarm status`
    showed RUNNING in the same second. The route answers with the task as its
    own write left it, so the state in that answer is the discriminator (it is
    exactly what the route derives `released_immediately` from).

    `--wait SECONDS` polls each flagged task until it has stopped, and exits 1
    if one has not by then: the cancel is then requested, not done.
    """
    wait = float(getattr(args, "wait", 0) or 0)
    code = EXIT_OK
    for task_id in args.task_ids:
        task = client.cancel(task_id)
        state = task.get("state")
        if state == "CANCELLED":
            print(f"{task_id} cancelled", flush=True)
            continue
        print(
            f"{task_id} cancel requested; stops when the worker releases it "
            f"(still {state or 'in a state the API did not report'})",
            flush=True,
        )
        if wait <= 0:
            continue
        final = _wait_until_stopped(client, task_id, wait)
        if final == "CANCELLED":
            print(f"{task_id} cancelled", flush=True)
        elif final in TERMINAL:
            print(f"{task_id} finished {final} before the cancel took effect", flush=True)
        else:
            print(
                f"{task_id} still {final} after {wait:g}s; the cancel is requested, not done",
                flush=True,
            )
            code = EXIT_FAIL
    return code


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
        print(f"  follow: {follow_command(followable)}")
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
    steps = report["steps"]
    # FINISHED means every step has stopped. Read from the steps, which are
    # live, rather than from the derived state alone, which may be absent.
    finished = bool(steps) and all(row.get("state") in TERMINAL for row in steps)
    header = f"{report['workflow_id']}  {state}  (stored: {report.get('stored_state')})"
    if report.get("cancel_requested") and not finished:
        # The workflow's own flag. Its state does not carry it -- a cancel only
        # flags a step that holds capacity -- so without this a cancelled
        # workflow read as plainly RUNNING (#88, SC-F2).
        header += "  cancel requested"
    print(header)
    if report.get("state_unavailable_because"):
        print(f"  ! {report['state_unavailable_because']}")
    if report.get("state_incomplete_because"):
        print(f"  ! {report['state_incomplete_because']}")
    # Each distinct no-patch sentence is printed ONCE, under the steps, and
    # each step points at it. It used to be printed in full under every step
    # that had no patch -- one paragraph five times for a five-step workflow
    # (#88, SC-F16).
    reasons: list[str] = []
    for row in steps:
        line = f"  {row['step_id']}  {row.get('state') or '—'}  {row.get('task_id') or '—'}"
        # The step task's `masked N` (owner decision, 2026-09-26), from the
        # task the read joined; a step with no task read has no count to say.
        if isinstance(row.get("masked"), dict):
            known = [n for n in row["masked"].values() if isinstance(n, int)]
            line += f"  masked {sum(known)}" if known else "  masked —"
        if row.get("cancel_requested") and row.get("state") not in TERMINAL:
            line += "  cancel requested; stops when the worker releases it"
        print(line)
        for key in ("state_unavailable_because", "park_reason"):
            if row.get(key):
                print(f"      {key}: {row[key]}")
        # As `sc agents` words it -- `provider concurrency limit
        # (provider:anthropic)` -- and not as a Python repr of the stored dicts.
        blockers = describe_blockers(row.get("blocked_by"))
        if blockers:
            print(f"      blocked by: {'; '.join(blockers)}")
        if row.get("error"):
            print(f"      error: {row['error']}")
        produced = row.get("produced") or {}
        if produced:
            patch = produced.get("patch")
            if not patch:
                reason = str(produced.get("no_patch_because") or "no reason recorded")
                if reason not in reasons:
                    reasons.append(reason)
                patch = f"none [{reasons.index(reason) + 1}]"
            # A step that cloned nothing or never started MEASURED no commits,
            # and `commits 0` beside its footnote was a zero nobody counted
            # (#191). The count is left out; the footnote says why.
            commits = produced.get("commits")
            counted = "" if commits is None else f"commits {commits}  "
            print(f"      {counted}patch {patch}")
    for number, reason in enumerate(reasons, 1):
        print(
            textwrap.fill(
                reason,
                width=78,
                initial_indent=f"  [{number}] ",
                subsequent_indent=" " * (len(str(number)) + 5),
                break_on_hyphens=False,
                break_long_words=False,
            )
        )
    # NOT for a workflow that has finished: a tail of steps that have all
    # stopped prints their ends and exits, which is not following anything.
    if report.get("follow_live_with") and not finished:
        print(f"  follow: {report['follow_live_with']}")
    return EXIT_FAIL if report.get("state") is None else EXIT_OK


def cmd_workflow_cancel(client: SwarmClient, args) -> int:
    """Cancel a workflow, and say which steps stopped and which were only asked to.

    The route answers `tasks_cancelled` for every step it asked to cancel. A
    step holding capacity is only FLAGGED by that ask and stays DISPATCHED or
    RUNNING until its worker releases it, and this printed "cancelled" for it
    -- on 2026-09-25 (#88, SC-F2) for a step that stayed DISPATCHED for about
    seventy seconds more. So the workflow is read ONCE afterwards, and each step
    is described by the state that read returns.
    """
    result = workflows.cancel(client, args.workflow_id)
    workflow_id = result.get("workflow_id") or args.workflow_id
    print(f"{workflow_id} cancel requested")

    states: dict[str, Any] = {}
    step_of: dict[str, Any] = {}
    try:
        envelope = workflows.fetch(client, str(workflow_id))
    except SwarmError as exc:
        # Nothing is guessed: every step is described as what is certain.
        print(f"  ! the steps' states could not be re-read: {exc}")
    else:
        for step in envelope["workflow"].get("steps") or []:
            if step.get("task_id"):
                step_of[str(step["task_id"])] = step.get("step_id")
        for task in envelope.get("tasks") or []:
            if isinstance(task, dict) and task.get("id"):
                states[str(task["id"])] = task.get("state")

    def name(task_id: str) -> str:
        step = step_of.get(task_id)
        return f"{step}  {task_id}" if step else task_id

    for task_id in [str(t) for t in result.get("tasks_cancelled") or []]:
        state = states.get(task_id)
        if state == "CANCELLED":
            print(f"  {name(task_id)}  cancelled")
        elif state in TERMINAL:
            print(f"  {name(task_id)}  finished {state} before the cancel took effect")
        else:
            now = f"still {state}" if state else "its state was not re-read"
            print(f"  {name(task_id)}  cancel requested; {now} until the worker releases it")
    # Printed, not swallowed. A step that had already finished is not a failed
    # cancel, and an operator who cannot see the difference re-runs the cancel.
    for task_id in [str(t) for t in result.get("tasks_already_terminal") or []]:
        state = states.get(task_id)
        print(f"  {name(task_id)}  already finished{f' ({state})' if state else ''}")
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

#: `sc`'s deployment-and-identity commands, reachable as `swarm login` and the
#: rest for the same reason the views are: one implementation, two spellings.
#: Routed on the first token like the views, so `swarm context add x --url y`
#: reaches `sc`'s parser verbatim.
SC_IDENTITY_COMMANDS = ("login", "logout", "whoami", "context")


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
    context = getattr(args, "context", None)
    prefix = ["--context", context] if context else []
    return _delegate_to_sc(prefix + [args.command] + list(args.argv))


# -- setup and diagnosis ---------------------------------------------------
#
# These three exist because of how this platform fails for a NEWCOMER. Every
# other command assumes a working connection; when there is not one, the error
# is "Invalid JWT audience" or a 403 with an HTML body, and neither says the
# true thing, which is usually "your laptop cannot mint that kind of token, and
# here is the one that it can".
#
# `profiles` joined them for the same reason rather than by analogy: the
# question it answers ("what may I name?") is one people ask while everything
# else is failing, and it reads the frozen catalogue rather than the cluster, so
# it is the one command that can still answer then. All three carry
# `no_client=True`.


def cmd_profiles(_client, args) -> int:
    """The runner catalogue: what you may name, and what each name is.

    NO CLIENT, and that is the useful part as well as the cheap part. The
    catalogue is the frozen contract, which this process already holds, so this
    answers when nothing else does -- which is precisely when someone is
    guessing at a profile name because `swarm doctor` is still telling them why
    the API is unreachable.

    It prints the SAME allow-listed view the `swarm_profiles` tool returns, from
    the same function. No image and no command appear in either: a caller names
    a profile and the execution details follow from the name, so they are not
    part of the vocabulary this surface offers.
    """
    entries = catalogue.catalogue()
    if args.json:
        print(json.dumps({"profiles": entries}, indent=2))
        return EXIT_OK
    for entry in entries:
        mark = "ok  " if entry["available"] else "--  "
        print(
            f"  {mark} {entry['name']:<12} {entry['backend']:<15} "
            f"{entry['resource_class']:<9} {entry['cpu']:g} cpu / "
            f"{entry['memory_gib']} GiB  timeout {entry['timeout_seconds']}s"
        )
        if entry.get("provider"):
            print(f"       provider {entry['provider']} -- needs a registered credential")
        if entry.get("inputs"):
            # What `--input` and a step's `inputs` may carry (#142).
            print(
                textwrap.fill(
                    ", ".join(f"{key} ({spec['kind']})" for key, spec in entry["inputs"].items()),
                    width=78,
                    initial_indent="       inputs ",
                    subsequent_indent=" " * 14,
                    break_on_hyphens=False,
                    break_long_words=False,
                )
            )
        if not entry["available"]:
            # WRAPPED and indented, like `doctor`'s explanations: the reason is
            # a sentence naming the remedy, and printed raw it is the part that
            # scrolls off.
            print(
                textwrap.fill(
                    entry["disabled_reason"],
                    width=78,
                    initial_indent="       refused: ",
                    subsequent_indent=" " * 16,
                    break_on_hyphens=False,
                    break_long_words=False,
                )
            )
    return EXIT_OK


def _doctor_deployment(args) -> Any:
    """Which deployment doctor is about, printed first: it decides the tier.

    Printed BEFORE the tier because the signed-in tier exists only for a
    deployment with a Desktop OAuth client, and "which deployment" is the first
    question anyone debugging this has. A config file that cannot be read is
    reported and doctor carries on -- explaining a broken setup is its job.
    """
    from . import config

    try:
        deployment = config.resolve(context=getattr(args, "context", None))
    except SwarmError as exc:
        print(f"deployment  UNREADABLE -- {exc}")
        return None
    if deployment is None:
        print("deployment  none configured")
        print(
            textwrap.fill(
                f"add one with `{terminal_command('sc context add <name> --url <url>')}`, "
                "or set SWARM_URL; the sc plugin asks at install",
                width=78,
                initial_indent="            ",
                subsequent_indent="            ",
                break_on_hyphens=False,
                break_long_words=False,
            )
        )
        return None
    print(f"deployment  {deployment.context}  {deployment.url}")
    print(
        textwrap.fill(
            f"from {deployment.source}",
            width=78,
            initial_indent="            ",
            subsequent_indent="            ",
            break_on_hyphens=False,
            break_long_words=False,
        )
    )
    if deployment.client_id:
        print(f"sign-in     Desktop OAuth client {deployment.client_id}")
    return deployment


def _print_reach(detection: Any, *, reached: bool) -> None:
    """What this tier reaches: the MEASUREMENT when there is one, else the table.

    THE TABLE IS A PREDICTION, AND IT WAS PRINTED OVER A MEASUREMENT. On
    2026-09-25 (#88, SC-F3) doctor reached the team deployment on the
    service-account tier -- `/v1/tenants/me` answered -- and in the same run
    printed "reaches solo deployments" and the pre-grant paragraph saying that
    tier is "missing authorisation" at the front door. The read is now done
    before this is printed, and when it succeeded that is the answer: this
    deployment was reached, and why another kind of deployment might not be is
    no longer this command's question. When it failed, the table and its
    explanations are printed as before, labelled as what they are -- they are
    exactly what a reader needs to say WHY it failed.
    """
    if reached:
        print("reaches     this deployment -- measured: /v1/tenants/me answered")
        return
    print(f"reaches     {', '.join(REACHES[detection.tier])} deployments (by tier; not measured)")
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


def cmd_doctor(_client, args) -> int:
    """Say which deployment, which tier this machine is on, and what that reaches."""
    deployment = _doctor_deployment(args)
    detection = detect(deployment)
    print(f"tier        {detection.tier.value}")
    # Wrapped like everything else here: a detail names the context and, on
    # the user-credentials tier, the route its token takes -- often past 80.
    print(
        textwrap.fill(
            detection.detail,
            width=78,
            initial_indent=" " * 12,
            subsequent_indent=" " * 12,
            break_on_hyphens=False,
            break_long_words=False,
        )
    )

    for name, verdict in detection.considered:
        print(f"  checked   {name}: {verdict}")

    print()
    if deployment is None:
        # ONLY the solo path needs a project: it asks Cloud Run for the
        # address. A configured deployment names its own, and demanding a
        # gcloud project from a plugin user who has none would fail the one
        # command meant to explain what is wrong.
        try:
            print(f"project     {project_id()}")
            print(f"service     {service_name()} / {region()}")
        except SwarmError as exc:
            print(f"project     UNKNOWN -- {exc}")
            _print_reach(detection, reached=False)
            return EXIT_FAIL

    # WHICH DOOR, PRINTED BEFORE THE READ, and this is the line whose absence
    # made this command mislead. `doctor` reported a tier, a project and a
    # service and then "api UNREACHABLE" -- so a reader concluded the API was
    # down, when the truth was that the bridge had asked Cloud Run for an
    # address whose ingress refuses everyone outside the VPC while the load
    # balancer that serves the whole organisation was named in Track C's tfvars
    # and never read. The address and the kind of credential that address takes
    # are the two facts that turn "unreachable" into something actionable.
    from .client import front_door_host

    front = deployment.url if deployment is not None and deployment.front_door else ""
    if not front:
        # The legacy sources: API_HOST, or this repository's tfvars in
        # developer mode (SWARM_MCP_CONFIG_FROM=repo) and never otherwise.
        host = front_door_host()
        front = f"https://{host}" if host else ""
    if front:
        # WHICH CREDENTIAL, not "the credential". A signed-in developer
        # presents their own ID token; a deployment that configured its own
        # OAuth client takes an ID token minted for that client id; everything
        # else presents an access token. Printing the wrong one sends a reader
        # to debug the one thing that was already right.
        if detection.tier is Tier.SIGNED_IN:
            presents = f"your own ID token, from `{terminal_command('sc login')}`"
        elif detection.tier is Tier.IAP:
            presents = "an ID token for the IAP client id"
        else:
            presents = "an OAuth ACCESS token"
        # TWO LINES, because one was 83 columns with a hostname of ordinary
        # length and this command is read in a terminal. `test_doctor_...`
        # asserts nothing here exceeds 80 for exactly that reason.
        print(f"front door  {front}  (IAP)")
        print(f"            this tier presents {presents}")
        if detection.tier is Tier.PROXY:
            print(
                textwrap.fill(
                    "this deployment is reached through its load balancer, so no "
                    "local proxy is started -- but a gcloud USER token is not one "
                    "IAP accepts here: measured 2026-09-24 it answers 401 with IAP "
                    "error code 900, because the deployment's IAP uses a "
                    "Google-managed OAuth client, which admits only allowlisted "
                    "programmatic clients. Sign in as yourself instead: give this "
                    "context the deployment's Desktop OAuth client id "
                    f"(`{terminal_command('sc context add <name> --url <url> --client-id <id>')}`) "
                    f"and run `{terminal_command('sc login')}`. "
                    "In CI, set SWARM_IMPERSONATE_SA to a service account that "
                    "holds roles/iap.httpsResourceAccessor; without that grant IAP "
                    "answers 403 and NAMES the service account, which is the "
                    "refusal that tells you the credential itself was accepted.",
                    width=78,
                    initial_indent="            ",
                    subsequent_indent="            ",
                    break_on_hyphens=False,
                    break_long_words=False,
                )
            )
    else:
        print("front door  none configured -- Cloud Run direct (takes a Google ID token)")

    # The reachability check is last and is allowed to fail: everything above
    # is still worth printing when the API is down, and is exactly what someone
    # needs in order to say WHY it is down.
    try:
        with SwarmClient(context=getattr(args, "context", None)) as client:
            endpoint = client.base_url
            me = client.request("GET", "/v1/tenants/me")
    except SwarmError as exc:
        print("api         UNREACHABLE")
        for line in str(exc).split(" -- "):
            print(f"            {line.strip()}")
        _print_reach(detection, reached=False)
        return EXIT_FAIL

    # NESTED, under `tenant` and `principal` (swarm_api/routes/tenants.py). The
    # flat reads printed `tenant None`, `admin None` and "identity (not
    # reported)" for a route that had just answered all three (#88, SC-F3).
    principal = principal_of(me)
    print(f"api         {endpoint}")
    print(f"identity    {principal.get('email') or '(not reported)'}")
    print(f"tenant      {tenant_of(me) or '(not reported)'}")
    groups = principal.get("groups") or []
    print(f"groups      {', '.join(groups) if groups else 'none -- personal tenant'}")
    if principal.get("is_admin_unresolved"):
        # `is_admin: false` is an assertion; the route says when it is not one.
        admin = "unknown -- the directory did not answer"
    elif principal.get("is_admin") is None:
        admin = "(not reported)"
    else:
        admin = str(principal["is_admin"])
    print(f"admin       {admin}")
    _print_reach(detection, reached=True)
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
            print(f"       deploy it first, then run `{terminal_command('swarm init')}` again")
            ok = False

    detection = detect()
    print(f"  ok   tier {detection.tier.value} (reaches {', '.join(REACHES[detection.tier])})")

    if not ok:
        return EXIT_FAIL

    target = Path(args.write or ".env")
    lines = [
        f"# Written by `{terminal_command('swarm init')}`. Safe to commit? "
        "NO -- it names your project.",
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
    print(f"Try:  {terminal_command('swarm dispatch')} \"say hello\" --profile mock")
    return EXIT_OK


def _run_quiet(argv):
    from .client import _run

    return _run(argv)


# -- entry point -----------------------------------------------------------


#: What a task-id argument accepts, said once for every command that takes one.
_IDS_HELP = "task ids, or a workflow id (wf_...) for every step of that workflow"


def _workflow_epilog() -> str:
    """`swarm workflow --help`'s example: the smallest spec that is worth writing.

    THE CLI DID NOT SAY WHAT A SPEC LOOKS LIKE (#88, SC-F17). The command takes
    a JSON file and nothing in `--help` showed one, so a first workflow was
    written by reading `workflows.py`. The step keys are read from
    `workflows._STEP_KEYS` -- the list `build_steps` enforces -- so this text
    cannot offer a key the command then refuses.
    """
    optional = ", ".join(sorted(workflows._STEP_KEYS - {"step_id", "prompt"}))
    keys = textwrap.fill(
        f"Each step needs step_id and prompt; it may also carry {optional} "
        f"(runner_profile defaults to {workflows.DEFAULT_PROFILE}). No other "
        "step key is sent. `inputs` holds only what the step's profile "
        'declares -- e.g. {"sleep_seconds": 120} for mock; the command at the '
        "end lists each profile's. At the top level, all optional: "
        "strategy, carrier, repository_url, repository_ref, on_step_failure, "
        "priority, label -- the flags below override the file's.",
        width=76,
    )
    # Spelled for this install, and on a line of its own: `textwrap.fill`
    # breaks at spaces, and the plugin-only spelling has spaces inside its
    # quoted requirement, so a wrapped copy would not run.
    profiles_command = terminal_command("swarm profiles")
    return (
        "A minimal spec: two steps, the second reading a file the first wrote.\n"
        "\n"
        "  {\n"
        '    "steps": [\n'
        '      {"step_id": "research", "runner_profile": "mock",\n'
        '       "prompt": "Survey the auth code and write notes.md"},\n'
        '      {"step_id": "draft", "runner_profile": "mock",\n'
        '       "prompt": "Draft the fix that notes.md describes",\n'
        '       "depends_on": ["research"],\n'
        '       "input_from": {"research": "notes.md"}}\n'
        "    ]\n"
        "  }\n"
        "\n"
        f"{keys}\n"
        "\n"
        "What each runner profile declares:\n"
        "\n"
        f"  {profiles_command}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    # RAW, so the exit-code table in the module docstring stays a table. The
    # default formatter reflowed it into one sentence -- "0 the thing asked for
    # happened 1 it did not 2 it happened..." (#88, SC-F17).
    parser = argparse.ArgumentParser(
        prog="swarm",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--context",
        default=None,
        help=f"which configured deployment to use (`{help_command('sc context list')}`); "
        "default: SWARM_URL, SWARM_CONTEXT, the plugin's, or the current context",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("dispatch", help="submit one task")
    d.add_argument("prompt", help="the agent's instructions, or - to read stdin")
    d.add_argument("--profile", default="claude-code")
    d.add_argument("--repo", default=os.environ.get("SWARM_REPO") or None)
    d.add_argument("--ref", default=None)
    d.add_argument("--label", default=None, help="recorded as metadata.unit")
    # ATTRIBUTION ONLY, and the help text has to say so. This flag looked like
    # model selection and is not: it becomes the TOP-LEVEL `model` field of the
    # task, which `swarm_api.schemas.TaskCreate` documents as "recorded for
    # attribution and cost reporting -- it selects nothing about the container".
    # Nothing carries it any further. `scheduler.dispatch.worker_env` builds the
    # worker's entire environment and deliberately carries "identifiers and
    # endpoints" only, so no MODEL reaches the container from a task; the
    # worker's own `cfg.model` is read from the Job definition's MODEL, and the
    # runner's `--model` argument comes from `input.model`, which this flag does
    # not set. So `--model` changed what the task record SAYS and never what
    # ran -- the worst shape a flag can have.
    #
    # Documented rather than refused. The field is real, it round-trips through
    # `codec.task_to_api`, and `swarm result --json` prints it, so a caller
    # tagging a run for cost reporting is using it correctly. Removing it would
    # break that and would still leave the API accepting the same field.
    #
    # Wiring it through to the agent is NOT a fix to make here: a caller-chosen
    # model in the execution environment is an execution parameter supplied by
    # the caller, which is what CONTRACT.md invariant 10 exists to forbid. That
    # is an owner decision and belongs in docs/contract-change-requests.md.
    d.add_argument(
        "--model",
        default=None,
        help=(
            "recorded on the task for attribution and cost reporting; "
            "it does NOT select the model the agent runs"
        ),
    )
    d.add_argument("--timeout", type=int, default=None)
    # DATA FOR THE RUNNER, and only what its profile declares (#142). Refused by
    # name for any other key and for any profile that declares none; the names
    # in the help are read from the one table that decides.
    d.add_argument(
        "--input",
        action="append",
        default=[],
        dest="inputs",
        metavar="KEY=VALUE",
        help=(
            "an input the profile declares, e.g. --input sleep_seconds=120; "
            "repeatable. Declared today by: "
            f"{', '.join(sorted(catalogue.DECLARED_INPUTS)) or 'no profile'} "
            f"(`{help_command('swarm profiles')}` lists each one's inputs)"
        ),
    )
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("status", help="one line per task")
    s.add_argument("task_ids", nargs="+", help=_IDS_HELP)
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("tail", help="follow logs and events until every task finishes")
    t.add_argument("task_ids", nargs="+", help=_IDS_HELP)
    t.add_argument("--interval", type=float, default=3.0)
    t.add_argument("--verbose", action="store_true", help="include heartbeat events")
    t.set_defaults(func=cmd_tail)

    f = sub.add_parser(
        "follow", help="what is new since the last look, through the API (what the MCP tool sees)"
    )
    f.add_argument("task_ids", nargs="+", help=_IDS_HELP)
    f.add_argument("--interval", type=float, default=3.0)
    f.add_argument("--once", action="store_true", help="one poll, then exit")
    f.add_argument("--max-log-bytes", type=int, default=DEFAULT_LOG_BUDGET, dest="max_log_bytes")
    f.add_argument("--verbose", action="store_true", help="include heartbeat events")
    f.set_defaults(func=cmd_follow)

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

    c = sub.add_parser("cancel", help="cancel tasks (a task holding capacity is flagged, not stopped)")
    c.add_argument("task_ids", nargs="+")
    c.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="poll each flagged task until it has stopped, up to SECONDS; "
        "exit 1 if one has not",
    )
    c.set_defaults(func=cmd_cancel)

    w = sub.add_parser(
        "workflow",
        help="submit a DAG of agents from a JSON spec",
        description="Submit a DAG of agents, read from a JSON spec file or stdin.",
        epilog=_workflow_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    w.add_argument("spec", help="path to the workflow JSON, or - to read stdin")
    w.add_argument("--repo", default=os.environ.get("SWARM_REPO") or None)
    w.add_argument("--ref", default=None)
    w.add_argument(
        "--strategy",
        default=None,
        choices=workflows.STRATEGIES,
        help="what happens to the agents' work: collect (the default -- patches "
        "are kept and nothing is pushed), direct-pr (each step opens its own "
        "pull request) or integrate (the final step opens one); the last two "
        "need a repository, from --repo or the spec's repository_url",
    )
    w.add_argument(
        "--carrier",
        default=None,
        choices=workflows.CARRIERS,
        help="where a step's work is kept for the next step: checkpoints (the "
        "default) or branches (pushed, and they outlive the platform)",
    )
    w.add_argument("--label", default=None, help="recorded as metadata.unit")
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

    for alias in ("accounts", "agents", "capacity", "trouble") + SC_IDENTITY_COMMANDS:
        alias_parser = sub.add_parser(alias, help=f"shorthand for `sc {alias}`")
        alias_parser.add_argument("argv", nargs=argparse.REMAINDER)
        alias_parser.set_defaults(func=cmd_sc, no_client=True)

    # `no_client=True` for the same reason `doctor` and `init` carry it: the
    # catalogue is the frozen contract, held in this process, so asking for a
    # client would make the one command that works without a cluster require one.
    prof = sub.add_parser(
        "profiles", help="the runner profiles you may name, and what each one is"
    )
    prof.add_argument("--json", action="store_true")
    prof.set_defaults(func=cmd_profiles, no_client=True)

    doc = sub.add_parser("doctor", help="which auth tier this machine is on, and what it reaches")
    doc.set_defaults(func=cmd_doctor, no_client=True)

    ini = sub.add_parser("init", help="first-run setup: check, write .env, say what is missing")
    ini.add_argument("--write", default=None, help="where to write (default: .env)")
    ini.add_argument("--force", action="store_true")
    ini.set_defaults(func=cmd_init, no_client=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in SC_COMMANDS + SC_IDENTITY_COMMANDS:
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
        with SwarmClient(context=args.context) as client:
            return args.func(client, args)
    except SwarmError as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
