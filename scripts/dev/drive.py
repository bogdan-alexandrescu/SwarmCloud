"""Run the control plane locally against the REAL Firestore and Cloud Run Jobs.

Deploying to diagnose costs about eight minutes per iteration, which is a poor
way to work through a chain of integration bugs. This runs the same scheduler
and reconciler code in-process against the same project, so a fix is seconds.

    uv run python scripts/dev/drive.py state       # dump tasks, leases, attempts
    uv run python scripts/dev/drive.py drain       # one scheduler drain pass
    uv run python scripts/dev/drive.py reconcile   # one reconciliation pass
    uv run python scripts/dev/drive.py submit                  # a mock task
    uv run python scripts/dev/drive.py submit claude-code "..."  # a real agent
    uv run python scripts/dev/drive.py submit claude-code "..." <repo-url>
    uv run python scripts/dev/drive.py clean       # delete tasks/leases/attempts

It is a DEVELOPMENT tool: it writes to the real control plane, so it refuses to
run against anything but the dev environment.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("PROJECT_ID", "saga-agents-staging")
os.environ.setdefault("REGION", "us-central1")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("FIRESTORE_DATABASE", "swarm")
os.environ.setdefault("ARTIFACT_BUCKET", "swarm-artifacts-saga-agents-staging")
os.environ.setdefault("LOG_LEVEL", "INFO")

if os.environ["ENVIRONMENT"] != "dev":
    raise SystemExit("drive.py writes to the live control plane; dev only")

from swarm_common.logging_setup import configure_logging  # noqa: E402

configure_logging(os.environ["LOG_LEVEL"])

from google.cloud import firestore  # noqa: E402

DB = firestore.Client(project=os.environ["PROJECT_ID"], database=os.environ["FIRESTORE_DATABASE"])
COLLECTIONS = ("tasks", "leases", "attempts", "workflows")


def state() -> None:
    for name in COLLECTIONS:
        docs = list(DB.collection(name).limit(25).stream())
        print(f"\n{name}: {len(docs)}")
        for d in docs:
            r = d.to_dict() or {}
            if name == "tasks":
                print(f"  {d.id} state={r.get('state')} gen={r.get('current_generation')} "
                      f"lease={r.get('current_lease_id')} tenant={r.get('tenant_id')} "
                      f"attempts={r.get('attempt_count')} blocked={r.get('blocked_by')}")
            elif name == "leases":
                print(f"  {d.id} state={r.get('state')} gen={r.get('generation')} "
                      f"task={r.get('task_id')} released={r.get('released_at')} "
                      f"expires={r.get('expires_at')}")
            elif name == "attempts":
                print(f"  {d.id} task={r.get('task_id')} gen={r.get('generation')} "
                      f"backend={r.get('backend')} execution={r.get('execution_name')}")
            else:
                print(f"  {d.id} {r.get('state')}")
    pools = list(DB.collection("pools").limit(40).stream())
    busy = [(p.id, (p.to_dict() or {}).get("active", 0)) for p in pools]
    print("\npools with active > 0:", [b for b in busy if b[1]] or "none")


def clean() -> None:
    for name in COLLECTIONS:
        docs = list(DB.collection(name).limit(500).stream())
        for d in docs:
            d.reference.delete()
        print(f"  deleted {len(docs)} from {name}")
    for p in DB.collection("pools").limit(100).stream():
        if (p.to_dict() or {}).get("active"):
            p.reference.update({"active": 0})
            print(f"  reset pool {p.id}.active -> 0")


def drain() -> None:
    """One scheduler drain pass, in-process, against the real control plane."""
    from scheduler.config import SchedulerConfig
    from scheduler.main import build_scheduler

    scheduler = build_scheduler()
    report = scheduler.drain()
    print("\ndrain report:")
    for k, v in sorted(vars(report).items() if hasattr(report, "__dict__") else report.items()):
        print(f"  {k:26s} {v}")


def reconcile() -> None:
    from reconciler.config import ReconcilerConfig
    from reconciler.logs import build_logger
    from reconciler.service import build_reconciler
    from swarm_common.config import Settings

    settings = Settings.from_env()
    config = ReconcilerConfig.from_env(settings)
    logger = build_logger()
    rec = build_reconciler(config, settings, logger)
    report = rec.run_once()
    print("\nreconcile report:")
    for k, v in sorted(vars(report).items() if hasattr(report, "__dict__") else report.items()):
        print(f"  {k:26s} {v}")


def submit() -> None:
    """Submit a task the way the API would, from a machine the API cannot see.

    The control plane's ingress is INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER, and
    terraform REFUSES to set it to ALL, so `POST /v1/tasks` from a laptop gets
    Google's 404 rather than the API. Until there is an internal load balancer
    in front of it, this is how an operator submits.

    It goes through SubmissionService, NOT straight to Firestore. That matters:
    the service is where the runner profile is looked up by name and where
    everything a caller must not choose -- image, command, resource class,
    backend -- is filled in from the frozen catalogue instead (CONTRACT.md
    invariant 10). A raw document write would skip exactly the check that keeps
    a caller from naming their own image, and would quietly make this tool a
    hole in the thing it is used to test.

        uv run python scripts/dev/drive.py submit
        uv run python scripts/dev/drive.py submit claude-code "list the files"
    """
    from swarm_api.auth import AuthContext
    from swarm_api.deps import build_context
    from swarm_api.schemas import TaskCreate
    from swarm_common.identity import Principal, resolve_tenant

    profile = sys.argv[2] if len(sys.argv) > 2 else "mock"
    prompt = sys.argv[3] if len(sys.argv) > 3 else ""
    # A repository to work in. Shallow-cloned by the worker into the attempt's
    # workspace before the agent starts, so the agent finds a checkout rather
    # than an empty directory -- which is the difference between a task that can
    # do project work and one that can only answer questions.
    repo = sys.argv[4] if len(sys.argv) > 4 else os.environ.get("SWARM_REPO", "")

    email = _caller_email()

    # Built by hand because there is no ID token here -- the caller is the
    # operator's own ADC, and the API's verifier would have nothing to verify.
    #
    # `groups=()` on purpose, and it is not a shortcut: reading group membership
    # needs Cloud Identity, and resolve_tenant then maps an ungrouped caller to
    # the personal fallback `u-<local part>`. That is EXACTLY what the deployed
    # API does today, because the swarm-api service account has no group-read
    # permission either (CONTRACT.md, verified operational constraint). So this
    # resolves to the same tenant the real path would, rather than to a
    # privileged one -- and it uses the frozen resolver to get there instead of
    # rebuilding the rule and drifting from it.
    principal = Principal(email=email, subject=email, domain=email.split("@")[-1], groups=())
    tenant_id = resolve_tenant(principal, ())
    ctx = AuthContext(
        principal=principal,
        tenant_id=tenant_id,
        is_admin=False,
        tenant_principal=email,
    )

    payload: dict = {}
    if prompt:
        payload["prompt"] = prompt

    spec: dict = {"runner_profile": profile, "input": payload}
    if repo:
        spec["repository_url"] = repo

    app = build_context(db=DB)
    result = app.submissions.submit_tasks(ctx, [TaskCreate(**spec)])
    for task in result.tasks:
        print(f"  {task.id}  {task.state}  tenant={task.tenant_id}  profile={task.runner_profile}")
    print(f"  woke scheduler: {result.woke_scheduler}")


def _caller_email() -> str:
    """Whoever gcloud is currently authenticated as."""
    import subprocess

    out = subprocess.run(
        ["gcloud", "config", "get-value", "account"],
        capture_output=True, text=True, check=False,
    )
    email = out.stdout.strip()
    if not email or "@" not in email:
        raise SystemExit(
            "could not determine the calling account; run: gcloud auth login"
        )
    return email


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    if cmd == "state":
        state()
    elif cmd == "clean":
        clean()
    elif cmd == "drain":
        drain()
    elif cmd == "reconcile":
        reconcile()
    elif cmd == "submit":
        submit()
    else:
        raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
