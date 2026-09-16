"""Run the control plane locally against the REAL Firestore and Cloud Run Jobs.

Deploying to diagnose costs about eight minutes per iteration, which is a poor
way to work through a chain of integration bugs. This runs the same scheduler
and reconciler code in-process against the same project, so a fix is seconds.

    uv run python scripts/dev/drive.py state       # dump tasks, leases, attempts
    uv run python scripts/dev/drive.py drain       # one scheduler drain pass
    uv run python scripts/dev/drive.py reconcile   # one reconciliation pass
    uv run python scripts/dev/drive.py submit      # insert a mock task
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
    else:
        raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
