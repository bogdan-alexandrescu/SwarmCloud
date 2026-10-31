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
from typing import Any

from swarm_api.install_record import INDIVIDUAL, InstallRecord, check_fields
from swarm_common.identity import tenant_id_for_user
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
    # The first workspace: one person's own, or the first team. swarm-api
    # keys a team by its workspace's tenant id and writes it as `workspace`.
    personal = tenant_id_for_user(world.installer)
    if sizing.get("mode") == INDIVIDUAL:
        write(record.personal_path(), {"owner": world.installer, "workspace": personal,
                                       "progress": {"created_at": now}, "grant": {"ok": True},
                                       "smoke": {"state": "SUCCEEDED"}})
    else:
        write(record.team_path("team-1"), {"group": "swarm-team@" + world.installer.split("@")[1], "workspace": "team-1",
                                           "first_admin": world.installer, "invites": [],
                                           "progress": {"created_at": now}, "grant": {"ok": True},
                                           "smoke": {"state": "SUCCEEDED"}})
    # swarm-workspace-apply makes the workspace's tenant namespace.
    tenant = personal if sizing.get("mode") == INDIVIDUAL else "team-1"
    world.apply_in_cluster(tenant_render(world, tenant))  # type: ignore[attr-defined]
    # The console's two verification runs (DAYONE-SETUP-API-2).
    # The console's two verification runs (DAYONE-SETUP-API-2): swarm-api
    # submits them as a service, on the team's -git slot.
    deployment = world.deployment  # type: ignore[attr-defined]
    # `GET /v1/tenants/mine` lists the installer's own READY personal
    # workspace first, as the default and current one (owner decision
    # 2026-10-12), then the team (audit 5b, M3).
    deployment.tenants[:] = list(dict.fromkeys([personal, tenant]))
    world.first_workspace = tenant  # type: ignore[attr-defined]
    from swarm_install.steps.verify import SINGLE_PROMPT, workflow_spec

    from .deployment import REPOSITORY, SERVICE

    task = deployment.submit(profile="claude-code", submitter=SERVICE, tenant=tenant, repository=REPOSITORY,
                             prompt=SINGLE_PROMPT, opens_pr=True)
    workflow = deployment.workflow(submitter=SERVICE, tenant=tenant, repository=REPOSITORY,
                                   spec=json.loads(workflow_spec(REPOSITORY, "console")))
    write(record.console_path("verify_runs"), {
        "verify_runs": {"console_task": task.id, "console_workflow": workflow},
        "started_at": now, "by": world.installer})
    write(record.console_path("finished"), {"at": now, "by": world.installer})
