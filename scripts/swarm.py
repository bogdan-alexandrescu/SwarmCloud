"""Dispatch agents to SwarmCloud and collect what they produced.

This is the bridge. Without it the platform runs agents perfectly well and
nothing here can ask it to, which is the state it has been in.

WHY IT TALKS TO FIRESTORE AND NOT TO THE API
--------------------------------------------
The control plane's ingress is INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER and the
terraform module VALIDATES against INGRESS_TRAFFIC_ALL, so `POST /v1/tasks` from
a workstation reaches Google's 404 rather than the API. Until there is an
internal load balancer with IAP in front of it, in-process is the only path.

It goes through SubmissionService rather than writing documents, and that is the
part to keep if this is ever rewritten. The service is where the runner profile
is resolved by name and where image, command, resource class and backend are
filled in from the frozen catalogue instead of being taken from the caller
(CONTRACT.md invariant 10). A direct write would skip exactly the check that
stops a caller naming their own image -- and root-in-pod (BUILD_PROMPT_V2 §2.2)
makes that check matter more, not less.

    scripts/swarm.py dispatch claude-code "prompt" [--repo URL] [--count N]
    scripts/swarm.py collect  task_a task_b ...      [--timeout 1800]
    scripts/swarm.py status   [task_id ...]
    scripts/swarm.py logs     task_id
    scripts/swarm.py cancel   task_id
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

os.environ.setdefault("PROJECT_ID", "saga-agents-staging")
os.environ.setdefault("REGION", "us-central1")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("FIRESTORE_DATABASE", "swarm")
os.environ.setdefault("ARTIFACT_BUCKET", "swarm-artifacts-saga-agents-staging")
os.environ.setdefault("LOG_LEVEL", "WARNING")

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for app in ("common", "swarm-api", "quota-broker"):
    sys.path.insert(0, os.path.join(_HERE, "apps", app))

#: Terminal states. A task in one of these will not change again, so `collect`
#: stops waiting rather than polling until its timeout.
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

#: PARKED is NOT terminal -- parked work resumes by itself when capacity or a
#: credential returns -- but it is reported distinctly, because a caller waiting
#: half an hour for something that is parked on a missing credential deserves to
#: be told rather than left to time out.
STALLED = {"PARKED"}


def _db():
    from google.cloud import firestore

    return firestore.Client(
        project=os.environ["PROJECT_ID"],
        database=os.environ["FIRESTORE_DATABASE"],
    )


def _caller_email() -> str:
    import subprocess

    out = subprocess.run(
        ["gcloud", "config", "get-value", "account"],
        capture_output=True, text=True, check=False,
    )
    email = out.stdout.strip()
    if not email or "@" not in email:
        raise SystemExit("could not determine the calling account; run: gcloud auth login")
    return email


def _context():
    """An AuthContext for whoever gcloud is, resolved the way the API resolves it."""
    from swarm_api.auth import AuthContext
    from swarm_common.identity import Principal, resolve_tenant

    email = _caller_email()
    # groups=() on purpose: reading membership needs Cloud Identity, and the
    # swarm-api service account has no permission to do it either, so the
    # deployed API resolves an ungrouped caller to the same personal fallback.
    # Using the frozen resolver rather than restating the rule keeps the two
    # from drifting.
    principal = Principal(email=email, subject=email, domain=email.split("@")[-1], groups=())
    return AuthContext(
        principal=principal,
        tenant_id=resolve_tenant(principal, ()),
        is_admin=False,
        tenant_principal=email,
    )


def cmd_dispatch(args: argparse.Namespace) -> int:
    from swarm_api.deps import build_context
    from swarm_api.schemas import TaskCreate

    spec: dict[str, Any] = {
        "runner_profile": args.profile,
        "input": {"prompt": args.prompt} if args.prompt else {},
    }
    if args.repo:
        spec["repository_url"] = args.repo
    if args.timeout:
        spec["timeout_seconds"] = args.timeout

    # Distinct prompts, one per line, for a real fan-out. `--count` repeats one
    # prompt; this is how a dozen agents each get a different job, which is what
    # a fan-out actually is.
    prompts = [args.prompt] * args.count
    if args.from_file:
        lines = [
            line.strip()
            for line in open(args.from_file, encoding="utf-8")
            if line.strip() and not line.startswith("#")
        ]
        if not lines:
            raise SystemExit(f"{args.from_file} has no prompts")
        prompts = lines

    app = build_context(db=_db())
    ctx = _context()
    # One submission for the whole batch, not N submissions. The service
    # validates the batch size and admits them together, and N separate calls
    # would be N separate wake-ups of the scheduler.
    specs = []
    for prompt in prompts:
        one = dict(spec)
        one["input"] = {"prompt": prompt} if prompt else {}
        specs.append(TaskCreate(**one))
    result = app.submissions.submit_tasks(ctx, specs)

    ids = [t.id for t in result.tasks]
    if args.json:
        print(json.dumps({"tasks": ids, "tenant": ctx.tenant_id}))
    else:
        for task in result.tasks:
            print(f"{task.id}  {task.state}  tenant={task.tenant_id}  profile={task.runner_profile}")
        if not result.woke_scheduler:
            # Worth saying: the scheduler ticks every minute anyway, so this is
            # a delay and not a failure, and silence here reads like one.
            print("  (scheduler not woken directly; its next tick picks these up within ~60s)")
    return 0


def _task(db, task_id: str) -> dict[str, Any]:
    snap = db.collection("tasks").document(task_id).get()
    if not getattr(snap, "exists", False):
        raise SystemExit(f"no such task: {task_id}")
    return snap.to_dict() or {}


def _state(task: dict[str, Any]) -> str:
    state = task.get("state")
    return getattr(state, "value", None) or str(state or "UNKNOWN").replace("TaskState.", "")


def cmd_status(args: argparse.Namespace) -> int:
    db = _db()
    if args.tasks:
        rows = [(t, _task(db, t)) for t in args.tasks]
    else:
        tenant = _context().tenant_id
        rows = [
            (d.id, d.to_dict() or {})
            for d in db.collection("tasks").where("tenant_id", "==", tenant).limit(25).stream()
        ]
    if not rows:
        print("  no tasks")
        return 0
    print(f"  {'TASK':26} {'STATE':12} {'ATT':>3}  {'PROFILE':12} DETAIL")
    for task_id, t in sorted(rows, key=lambda r: str(r[1].get("created_at"))):
        detail = t.get("park_reason") or str(t.get("error") or "")[:48]
        print(f"  {task_id:26} {_state(t):12} {t.get('attempt_count', 0):>3}  "
              f"{str(t.get('runner_profile', '')):12} {detail}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Wait for tasks and print what each produced.

    Polls rather than subscribes: Firestore listeners need a long-lived
    connection this tool has no reason to hold, and a poll every few seconds is
    cheap against work measured in minutes.
    """
    db = _db()
    deadline = time.time() + args.timeout
    pending = list(args.tasks)
    done: dict[str, dict[str, Any]] = {}

    while pending and time.time() < deadline:
        for task_id in list(pending):
            task = _task(db, task_id)
            state = _state(task)
            if state in TERMINAL:
                done[task_id] = task
                pending.remove(task_id)
                if not args.json:
                    print(f"  {task_id}  {state}")
            elif state in STALLED and not args.wait_parked:
                # Reported rather than waited out. Parked work resumes by
                # itself, but not necessarily inside this timeout, and a caller
                # who does not know it is parked learns nothing from the wait.
                done[task_id] = task
                pending.remove(task_id)
                if not args.json:
                    print(f"  {task_id}  {state}  ({task.get('park_reason') or 'parked'})")
        if pending:
            time.sleep(args.interval)

    for task_id in pending:
        done[task_id] = _task(db, task_id)

    results = {}
    for task_id, task in done.items():
        entry: dict[str, Any] = {"state": _state(task), "attempts": task.get("attempt_count", 0)}
        if task.get("park_reason"):
            entry["park_reason"] = task["park_reason"]
        if task.get("error"):
            entry["error"] = str(task["error"])[:400]
        if args.output:
            entry["output"] = _agent_output(task_id, task)
        results[task_id] = entry

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for task_id, entry in results.items():
            if entry.get("output"):
                print(f"\n=== {task_id} ===\n{entry['output']}")
    # Non-zero when anything did not succeed, so this composes with `set -e`.
    return 0 if all(e["state"] == "SUCCEEDED" for e in results.values()) else 1


def _agent_output(task_id: str, task: dict[str, Any]) -> str:
    """The agent's own reply, read from the artifacts it left behind.

    Claude Code's `--output-format json` result document carries `result`, which
    is what the agent actually said. Everything else in the artifact tree is
    evidence; this is the answer.
    """
    try:
        from google.cloud import storage
    except ImportError:
        return "(google-cloud-storage is not installed)"

    bucket_name = os.environ["ARTIFACT_BUCKET"]
    tenant = task.get("tenant_id") or _context().tenant_id
    prefix = f"tenants/{tenant}/tasks/{task_id}/attempts/"
    client = storage.Client(project=os.environ["PROJECT_ID"])
    bucket = client.bucket(bucket_name)

    candidates = [
        b for b in client.list_blobs(bucket, prefix=prefix)
        if b.name.endswith("claude-code.stdout.log") or b.name.endswith("output.txt")
    ]
    if not candidates:
        return "(no output artifact)"
    raw = max(candidates, key=lambda b: b.updated).download_as_text()
    try:
        return str(json.loads(raw).get("result", raw))
    except (json.JSONDecodeError, AttributeError):
        return raw[:4000]


def cmd_logs(args: argparse.Namespace) -> int:
    db = _db()
    task = _task(db, args.task)
    print(f"  {args.task}  {_state(task)}")
    print(f"  artifacts: gs://{os.environ['ARTIFACT_BUCKET']}/tenants/"
          f"{task.get('tenant_id')}/tasks/{args.task}/")
    print(f"  output   : {_agent_output(args.task, task)[:2000]}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Request cancellation of a task.

    Goes through `store.request_cancel`, which is what POST
    /v1/tasks/{id}/cancel calls (routes/tasks.py). This used to call
    `submissions.cancel_task`, a method SubmissionService has never had -- so
    `swarm.py cancel` raised AttributeError every time it was used, and the only
    way to find that out was to need it.
    """
    from swarm_api.deps import build_context

    from swarm_api.errors import Conflict

    app = build_context(db=_db())
    ctx = _context()
    try:
        # request_cancel returns a Task MODEL, not the raw document `_state` reads.
        task = app.store.request_cancel(ctx.tenant_id, args.task, by=ctx.email)
    except Conflict as exc:
        # Already terminal. That is an answer, not a crash -- a CLI that prints a
        # traceback for "it already finished" teaches its user to distrust it.
        print(f"  {args.task}  {exc}")
        return 0
    state = getattr(task, "state", None)
    print(f"  {args.task}  {getattr(state, 'value', state)}  (cancellation requested)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("dispatch", help="send work to the cluster")
    d.add_argument("profile")
    d.add_argument("prompt", nargs="?", default="")
    d.add_argument("--repo", default="", help="repository to shallow-clone into the workspace")
    d.add_argument("--count", type=int, default=1, help="dispatch this many identical tasks")
    d.add_argument("--from-file", default="",
                   help="one prompt per line; blank lines and # comments ignored")
    d.add_argument("--timeout", type=int, default=0, help="per-task seconds; may only SHORTEN the profile's")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_dispatch)

    c = sub.add_parser("collect", help="wait for tasks and read what they produced")
    c.add_argument("tasks", nargs="+")
    c.add_argument("--timeout", type=int, default=1800)
    c.add_argument("--interval", type=int, default=10)
    c.add_argument("--output", action="store_true", default=True)
    c.add_argument("--json", action="store_true")
    c.add_argument("--wait-parked", action="store_true",
                   help="keep waiting on PARKED tasks instead of reporting them")
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("status", help="what this tenant has in flight")
    s.add_argument("tasks", nargs="*")
    s.set_defaults(func=cmd_status)

    l = sub.add_parser("logs", help="where one task's artifacts are, and what it said")
    l.add_argument("task")
    l.set_defaults(func=cmd_logs)

    k = sub.add_parser("cancel", help="stop a task")
    k.add_argument("task")
    k.set_defaults(func=cmd_cancel)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
