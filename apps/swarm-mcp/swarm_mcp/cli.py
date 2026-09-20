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
import time
from pathlib import Path
from typing import Any

from .client import SwarmClient, SwarmError
from .patches import (
    apply_patch,
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
    return (
        f"gs://{_artifact_bucket()}/tenants/{task['tenant_id']}"
        f"/tasks/{task['task_id']}/attempts/{attempt_id}/logs/live/{stream}.tail.log"
    )


def _latest_attempt(client: SwarmClient, task_id: str) -> str | None:
    try:
        data = client.request("GET", f"/v1/tasks/{task_id}/attempts?limit=1")
    except SwarmError:
        return None
    attempts = data.get("attempts") if isinstance(data, dict) else None
    if not attempts:
        return None
    return attempts[0].get("attempt_id")


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
        print(task.get("task_id", ""), flush=True)
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

            attempt = attempts.get(task_id) or _latest_attempt(client, task_id)
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(SwarmClient(), args)
    except SwarmError as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
