"""The console's Admin › Setup, done by the person in the browser.

Each step writes what swarm-api writes when the person finishes it, at the
paths `swarm_api.install_record.InstallRecord` names and with the fields its
`check_fields` admits, through the fake Firestore client (which refuses a
path real Firestore refuses). Nothing here is written where swarm-api would
not write it.
"""

from __future__ import annotations

import json
import secrets
from subprocess import run as _real_run  # bound before the walk patches subprocess.run
from types import SimpleNamespace
from typing import Any

from swarm_api.install_record import INDIVIDUAL, InstallRecord, check_fields
from swarm_api.routes import setup as setup_routes
from swarm_install import tfvars

from .world import REPO, World


def tenant_render(world: World, tenant: str) -> str:
    """kubernetes/render.py's tenant render, run for real, with the uniqueIds IAM assigned."""
    import sys

    cloud = world.cloud  # type: ignore[attr-defined]

    def uid(name: str) -> str:
        return str(cloud.accounts[f"{name}@{world.project}.iam.gserviceaccount.com"]["uniqueId"])

    done = _real_run([sys.executable, str(REPO / "kubernetes" / "render.py"), "tenant", f"--tenant={tenant}",
                      f"--project={world.project}", "--region=europe-west1",
                      f"--scheduler-uid={uid('swarm-scheduler')}", f"--reconciler-uid={uid('swarm-reconciler')}",
                      f"--verify-uid={uid('swarm-verify')}"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-600:]
    return done.stdout


def _env(world: World) -> str:
    """The engine's environment: its own document, `swarm-install/<env>`, already written."""
    found = sorted({p.split("/")[1] for p in world.documents if p.startswith("swarm-install/") and p.count("/") == 1})
    assert len(found) == 1, f"the engine wrote {found} as its document, not one"
    return found[0]


def admin_setup(url: str, world: World) -> None:
    """Every Admin › Setup step, in the console's order. `open_page` never
    raises, so a step the fake console could not do is kept for the walk's
    assertions (`World.unhandled`)."""
    if getattr(world, "console_done", False):
        return
    world.console_done = True  # type: ignore[attr-defined]
    try:
        _steps(world)
    except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
        world.unhandled.append(f"console: {type(exc).__name__}: {exc}")


def _steps(world: World) -> None:
    cloud = world.cloud  # type: ignore[attr-defined]
    project = world.project
    env = _env(world)
    record = InstallRecord(None, env)
    db = world.firestore_client(project, "swarm")
    engine = (world.documents.get(record.engine_path()) or ({}, 0))[0]
    sizing = engine.get("sizing") or {}
    from datetime import datetime, timezone

    now = datetime.fromtimestamp(world.clock.now(), timezone.utc).isoformat()

    def write(path: str, data: dict[str, Any]) -> None:
        check_fields(path, env, data)
        db.document(path).set(data, merge=True)

    # The GitHub App: created from the manifest, its three secrets written by swarm-api.
    for name in tfvars.APP_SECRETS:
        cloud.secrets.setdefault((project, name), {"name": f"projects/{cloud.number(project)}/secrets/{name}",
                                                   "labels": {"managed-by": "swarm-api"}})
        cloud.versions.setdefault((project, name), []).append(secrets.token_hex(16).encode())
    write(record.console_path("github_app"), {
        "id": 424242, "slug": "acme-swarm", "client_id": "Iv1." + secrets.token_hex(8),
        "installed_on": "acme", "secrets_written_at": now, "self_check": {"ok": True, "at": now}})
    # The Claude subscription accounts the sizing proposed, each signed in.
    accounts = {f"pool-{n}": {"owner": world.installer, "signed_in_at": now}
                for n in range(1, int(sizing.get("pool_accounts") or 1) + 1)}
    write(record.console_path("accounts"), accounts)
    # The first workspace: one person's own, or the first team, granted the
    # repository the App installation covers (decision 3).
    from .deployment import REPOSITORY

    grant = {"state": "granted", "repositories": [REPOSITORY]}
    if sizing.get("mode") == INDIVIDUAL:
        write(record.personal_path(), {"owner": world.installer, "workspace": "ws-personal",
                                       "progress": {"created_at": now}, "grant": grant,
                                       "smoke": {"state": "SUCCEEDED"}})
    else:
        write(record.team_path("team-1"), {"group": "swarm-team@" + world.installer.split("@")[1], "workspace": "ws-team-1",
                                           "first_admin": world.installer, "invites": [],
                                           "progress": {"created_at": now}, "grant": grant,
                                           "smoke": {"state": "SUCCEEDED"}})
    # swarm-workspace-apply makes the workspace's tenant namespace.
    tenant = "personal" if sizing.get("mode") == INDIVIDUAL else "team-1"
    world.apply_in_cluster(tenant_render(world, tenant))  # type: ignore[attr-defined]
    # Hand-over: `finish` writes `console/finished`, then starts the console's
    # two verification runs through swarm-api's own `ensure_verify_runs` (audit
    # 5b, B1), as a service on the team's -git slot. Nothing here writes
    # `console/verify_runs` itself, so a `finish` that started nothing would
    # leave the engine's verify waiting, as it would live.
    deployment = world.deployment  # type: ignore[attr-defined]
    deployment.tenants.append(tenant)
    write(record.console_path("finished"), {"at": now, "by": world.installer})
    path = record.personal_path() if sizing.get("mode") == INDIVIDUAL else record.team_path("team-1")
    doc = (world.documents.get(path) or ({}, 0))[0]
    first = setup_routes.FirstWorkspace(key=tenant, doc={**doc, "workspace": tenant}, path=path,
                                        principal=str(doc.get("group") or world.installer),
                                        personal=sizing.get("mode") == INDIVIDUAL)
    setup_routes.ensure_verify_runs(_Record(db, env), SimpleNamespace(now=lambda: now),
                                    first, runs=_Runs(deployment), by=world.installer)


class _Record(InstallRecord):
    """swarm-api's `InstallRecord` over the engine world's Firestore client:
    its reads, its paths and its `check_fields` as they are; only the
    transaction of `write` is the client's merge, which the fake holds."""

    def _ref(self, path: str) -> Any:
        return self._db.document(path)

    def write(self, path: str, data: Any) -> dict[str, Any]:
        check_fields(path, self.env, data)
        self._ref(path).set(dict(data), merge=True)
        return self.read(path) or {}


class _Runs:
    """swarm-api's `TeamRuns` over the fake deployment: a submission as the
    service, the task with the prompt swarm-api sends, the workflow with
    verify's own console spec."""

    def __init__(self, deployment: Any) -> None:
        self._deployment = deployment

    def submit_task(self, *, tenant_id: str, group: str, repository: str, prompt: str, label: str,
                    strategy: str = setup_routes.SMOKE_STRATEGY) -> str:
        from .deployment import SERVICE

        return self._deployment.submit(profile=setup_routes.RUN_PROFILE, submitter=SERVICE, tenant=tenant_id,
                                       repository=repository, prompt=prompt, opens_pr=True).id

    def submit_workflow(self, *, tenant_id: str, group: str, repository: str, label: str) -> str:
        from swarm_install.steps.verify import workflow_spec

        from .deployment import SERVICE

        return self._deployment.workflow(submitter=SERVICE, tenant=tenant_id, repository=repository,
                                         spec=json.loads(workflow_spec(repository, "console")))

    def task_state(self, *, tenant_id: str, task_id: str) -> str:
        return setup_routes.RUNNING

    def workflow_state(self, *, tenant_id: str, workflow_id: str) -> str:
        return setup_routes.RUNNING
