"""A service submission whose run gets an App installation token names the
tenant's `-git` slot, never the personal tenant's owner's user slot
(DAYONE-SERVICE-SLOT-APP; part of #2688; audit 4d F3).

In a one-person install the installer connects their own GitHub before stage
14 (their plugin verify runs use it), so their personal tenant has an owner's
user slot and no `-git` slot. The first team's smoke and the console's verify
runs are submitted as swarm-api's own service account. Before this lane the
service rule (`forge.service_slot`) named the OWNER's slot even when the run's
token was to be minted from the App: the scheduler's `forge_answer` saw a
`git-u-` slot whose hex is not the submitter's and parked the task as
CONNECTION_MISSING before any lease, and the worker's `grant_refusal` found
no grant of the service account's behind it. The page read PARKED as
"running" until verify's bound.

Held here over the in-memory Firestore, with no credentials and no network,
through the REAL scheduler and worker checks (not a fake of them):

  * the minted run names `git`, with write, labelled `app_installation`;
  * the scheduler admits it (`NO_USER_SLOT`, runnable) and the worker's
    grant check passes it;
  * the broker's decision over the same signed fields mints for it;
  * an unminted service submission in the same tenant keeps the owner's
    slot, as INDEX-CRED left it;
  * the owner's slot named for a service submitter is still refused by both
    checks, so nothing here widens who may use a person's slot.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from agent_worker import secrets as worker_secrets
from scheduler import credentials as sched
from swarm_api import credbroker
from swarm_api.auth import AuthContext
from swarm_api.gittokens import Scope, TokenState, provider_suffix, record_for_slot
from swarm_api.schemas import TaskCreate
from swarm_api.validation import FORGE_SOURCE_METADATA_KEY
from swarm_common.identity import Principal, tenant_id_for_user
from swarm_common.models import Tenant

from .conftest import PROJECT, seed_pool
from .test_service_app_token import INSTALLATION, OWNER, REPO, REPO_URL, SERVICE, _context, _enable

INSTALLER = "installer@example.com"
PERSONAL = tenant_id_for_user(INSTALLER)
NOW = datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)


def _seed_personal(db) -> None:
    tenant = Tenant(
        tenant_id=PERSONAL, kind="user", principal=INSTALLER, created_at=NOW,
        display_name=INSTALLER, max_active=20, capacity_units=40, enabled=True,
        credentials=[],
        service_account=f"swarm-agent-worker-{PERSONAL}@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/{PERSONAL}",
        namespace=f"swarm-tenant-{PERSONAL}",
    )
    db.docs[f"tenants/{PERSONAL}"] = asdict(tenant)
    seed_pool(db, f"tenant:{PERSONAL}", hard_limit=20)


def _connect_owner(db) -> None:
    """The installer's own GitHub connection: their user slot's record."""
    record = record_for_slot(PERSONAL, Scope.USER, user=INSTALLER, registered_by=INSTALLER,
                             now=NOW)
    doc = record.to_firestore()
    doc["state"] = TokenState.ACTIVE.value
    db.collection("git_tokens").document(record.token_id).set(doc)


def _service_auth() -> AuthContext:
    return AuthContext(
        principal=Principal(email=SERVICE, subject=f"sub-{SERVICE}",
                            domain=SERVICE.rsplit("@", 1)[-1], groups=()),
        tenant_id=PERSONAL, is_admin=False, tenant_principal=INSTALLER)


def _spec() -> TaskCreate:
    return TaskCreate(runner_profile="mock", input={"prompt": "verify"},
                      repository_url=REPO_URL)


def _admitted(db, task) -> tuple[object, str | None]:
    """What the scheduler and the worker say of the stored task."""
    answer = sched.forge_answer(task, sched.ForgeConnections(db))
    refusal = worker_secrets.grant_refusal(
        db, tenant_id=PERSONAL, suffix=task.forge_credential,
        repository_url=task.repository_url, write=True, submitted_by=task.submitted_by)
    return answer, refusal


def test_a_minted_service_run_in_a_personal_tenant_names_the_git_slot(db, tokens, group_map):
    _seed_personal(db)
    _connect_owner(db)
    _enable(db, PERSONAL, OWNER, INSTALLATION, user=INSTALLER)
    context = _context(db, tokens, group_map)

    task = context.submissions.submit_tasks(_service_auth(), [_spec()]).tasks[0]

    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "app_installation"
    # MUTATION: name `slot.provider` for a minted run and this is the owner's `git-u-` slot.
    assert task.forge_credential == "git"
    assert task.forge_access == "write"
    assert task.submitted_by == SERVICE

    answer, refusal = _admitted(db, task)
    # Before this lane: CONNECTION_MISSING -- parked before any lease, for good.
    assert answer is sched.ForgeAnswer.NO_USER_SLOT
    assert answer in sched._FORGE_RUNNABLE
    assert refusal is None

    # The broker decides from the same signed fields, and mints for this repository.
    release = credbroker.decide_run_token(
        {"submitted_by": task.submitted_by, "forge_credential": task.forge_credential,
         "repository_url": task.repository_url, "forge_access": task.forge_access},
        access="write")
    assert (release.suffix, release.owner, release.repo, release.write) == (
        "git", OWNER, REPO, True)


def test_an_unminted_service_run_keeps_the_owners_connection(db, tokens, group_map):
    # No App installation for the owner: the service rule as INDEX-CRED left it.
    _seed_personal(db)
    _connect_owner(db)
    context = _context(db, tokens, group_map)

    task = context.submissions.submit_tasks(_service_auth(), [_spec()]).tasks[0]

    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "owner"
    assert task.forge_credential == provider_suffix(Scope.USER, user=INSTALLER)


def test_the_owners_slot_is_still_not_a_service_submitters(db):
    # Nothing here relaxes the checks: a service submitter on the owner's
    # user slot is parked and has no grant, as before.
    _seed_personal(db)
    _connect_owner(db)

    class Stored:
        tenant_id = PERSONAL
        submitted_by = SERVICE
        repository_url = REPO_URL
        forge_credential = provider_suffix(Scope.USER, user=INSTALLER)

    answer, refusal = _admitted(db, Stored())
    assert answer is sched.ForgeAnswer.CONNECTION_MISSING
    assert refusal is not None
