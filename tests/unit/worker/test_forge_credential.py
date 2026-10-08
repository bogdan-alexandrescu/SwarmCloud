"""The worker reads the task's signed GitHub credential and re-checks its grant (OB5, #780).

docs/onboarding.md §3.3 steps 3-4, contract request 54:

  * the worker reads `tenant.secret_name(task.forge_credential or "git")`, the
    latest version, at every forge call -- never cached -- and only once the
    generation check and then the spec signature have passed;
  * a `forge_access == "read"` task never reads a push credential: a step
    that opens a pull request (or carries its work on a branch) ends before
    the agent starts, and any other push is refused where it would happen;
  * for a user slot, the submitter's grant is read again by id before the
    clone and before each push or pull request. Deleted before the clone, the
    attempt ends before the agent starts and the lease is only released;
    deleted before the push, the final checkpoint is taken, the push is
    refused, and the attempt ends PUBLISH_REFUSED.

Every fake token is built at runtime, never written as one literal.

MUTATIONS, each caught below: read `-git` whatever the task names -- the
user-slot read fails. Trust `forge_credential` on an unsigned task -- the
legacy test fails. Drop the STEP 4b' grant check from `_prepare` -- the
clone-time test sees the agent start. Read the token before
`_forge_write_refusal` in `_publish_git` -- the token-count assertions fail.
Drop the `forge_write_refused` ending in `_record_finalised` -- the push test
sees SUCCEEDED or OUTPUTS_MISSING. Move the grant check before the generation
check -- the fencing test sees a grant read.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, secrets as secrets_mod, workspace as workspace_mod
from swarm_api import forgeapp, gittokens, repositories
from swarm_common.states import EventType, TaskState

from fakes import FakeSecretClient
from worker_seeds import TENANT, seed_attempt
from test_strategy_end_to_end import (  # noqa: F401 -- fixtures
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
    refs,
)

PERSON = "Ada@Example.com"
OWNER, REPO = "Saga", "Widgets"
GITHUB_URL = f"https://github.com/{OWNER}/{REPO}.git"


def user_token() -> str:
    """A GitHub user access token's shape, built here so no literal is one."""
    return "ghu_" + "x" * 36


def slot(user: str = PERSON) -> str:
    return gittokens.provider_suffix(gittokens.Scope.USER, user=user)


def repo_id() -> str:
    return repositories.repo_id_for(TENANT, OWNER, REPO)


def grant_path(user: str = PERSON) -> str:
    return f"forge_grants/{TENANT}__{forgeapp.user_hash(user)}__{repo_id()}"


def seed_grant(db, *, mode: str = "write", user: str = PERSON, **override: Any) -> None:
    """A grant document in the shape `swarm_api.access.AccessService.grant` writes."""
    doc = {
        "tenant_id": TENANT,
        "user": user.strip().lower(),
        "user_hash": forgeapp.user_hash(user),
        "repo_id": repo_id(),
        "repository": f"{OWNER}/{REPO}",
        "owner": OWNER.lower(),
        "mode": mode,
        "can_push": True,
    }
    doc.update(override)
    db.seed(grant_path(user), doc)


def secret_client() -> FakeSecretClient:
    return FakeSecretClient({f"swarm-tenant-{TENANT}-{slot()}": user_token()})


def grant_reads(monkeypatch, db) -> list[str]:
    """Every read of the grant collection, in order."""
    reads: list[str] = []
    real = db.collection

    def collection(name: str):
        if name == secrets_mod.FORGE_GRANTS:
            reads.append(name)
        return real(name)

    monkeypatch.setattr(db, "collection", collection)
    return reads


# --------------------------------------------------------------------------
# The parity the restated recipe depends on
# --------------------------------------------------------------------------


def test_the_grant_collection_and_id_are_swarm_apis():
    assert secrets_mod.FORGE_GRANTS == forgeapp.GRANTS
    from swarm_api import access

    hashed = secrets_mod.user_slot_hash(slot())
    assert hashed == forgeapp.user_hash(PERSON)
    assert f"{TENANT}__{hashed}__{repo_id()}" == access.grant_id_for(
        TENANT, PERSON.lower(), repo_id()
    )


# --------------------------------------------------------------------------
# Step 3: which secret is read
# --------------------------------------------------------------------------


def _verified(worker, task: dict[str, Any]) -> None:
    """What `_verify_spec` leaves behind for a signed, verified document."""
    worker._task = task
    worker._forge_signed = True


def test_a_user_slot_task_reads_the_users_secret_every_time(db, worker_factory):
    seed_attempt(db)
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    _verified(worker, {"repository_url": GITHUB_URL, "forge_credential": slot()})

    first = worker._git_token()
    second = worker._git_token()

    name = f"swarm-tenant-{TENANT}-{slot()}"
    assert first == second == user_token()
    # Read again at the second call: the latest version at that moment, which
    # is what lets an attempt outlive an 8-hour user token.
    assert client.accessed == [name, name]
    assert user_token() in worker.log._secrets, "the token was not registered for redaction"


def test_git_or_none_reads_the_tenant_token_as_before(db, worker_factory):
    seed_attempt(db)
    db.doc(f"tenants/{TENANT}")["credentials"] = ["git"]
    client = FakeSecretClient()
    worker, _, _ = worker_factory(secret_client=client)
    for named in (None, "git"):
        _verified(worker, {"repository_url": GITHUB_URL, "forge_credential": named})
        worker._git_token()
    assert client.accessed == [f"swarm-tenant-{TENANT}-git"] * 2


def test_an_unsigned_task_never_chooses_a_members_slot(db, worker_factory):
    """The legacy window admits an unsigned task; nothing signed its field."""
    seed_attempt(db)
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    worker._task = {"repository_url": GITHUB_URL, "forge_credential": slot()}
    worker._forge_signed = False

    assert worker._forge_suffix() == "git"
    worker._git_token()
    assert client.accessed == [], "an unsigned task read a member's slot"


# --------------------------------------------------------------------------
# A whole attempt
# --------------------------------------------------------------------------


def _seed_user_task(db, *, access: str | None = "write", dispatch: dict | None = None,
                    task_generation: int | None = None) -> None:
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.01},
                 task_generation=task_generation)
    doc = db.doc("tasks/task_1")
    doc["repository_url"] = GITHUB_URL
    doc["submitted_by"] = PERSON.lower()
    doc["forge_credential"] = slot()
    doc["forge_access"] = access
    doc["metadata"] = {"dispatch": dict(dispatch or {"strategy": "collect"})}


def _agent_starts(worker, monkeypatch) -> list[bool]:
    started: list[bool] = []
    real = worker._run_child_supervised

    def spy(child_env):
        started.append(True)
        return real(child_env)

    monkeypatch.setattr(worker, "_run_child_supervised", spy)
    return started


def _clone_from(worker, monkeypatch, origin: Path) -> list[bool]:
    """Clone the bare repository on disk in place of github.com.

    The signed document names the GitHub URL -- that is what the grant is
    read by -- and only the clone's transport is redirected, through the real
    `_maybe_clone`. Returns a list that records each clone.
    """
    cloned: list[bool] = []
    real = worker._maybe_clone

    def clone(task: dict[str, Any]):
        cloned.append(True)
        return real({**task, "repository_url": f"file://{origin}"})

    monkeypatch.setattr(worker, "_maybe_clone", clone)
    return cloned


def test_a_grant_deleted_before_the_clone_ends_the_attempt_before_the_agent(
    db, worker_factory, monkeypatch, log_stream
):
    _seed_user_task(db)
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    started = _agent_starts(worker, monkeypatch)
    cloned: list[bool] = []
    monkeypatch.setattr(worker, "_maybe_clone", lambda task: cloned.append(True))
    reads = grant_reads(monkeypatch, db)
    # No grant: the person removed the repository after submitting.

    assert worker.run() == lifecycle.ExitCode.FAILED

    assert reads, "the grant was never read"
    assert started == [] and cloned == [], "the agent or the clone ran on a revoked grant"
    assert client.accessed == [], "the user's token was read for a revoked grant"
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "cannot_start"
    assert task["last_error"].startswith("forge_grant_revoked:"), task["last_error"]
    assert task["result_summary"]["forge_check"]["cause"] == "forge_grant_revoked"
    # The lease is released, and nothing else is done to it: not extended,
    # not heartbeaten past the release.
    lease = db.doc("leases/lease_1")
    assert lease["released_at"] is not None
    assert task["current_lease_id"] is None
    assert [e for e in db.events("task_1") if e["type"] == EventType.RETRYING.value] == []
    assert user_token() not in log_stream.getvalue()


def test_a_standing_grant_reaches_the_clone_and_the_agent(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    """The control for the test above: the same task with its grant runs."""
    _seed_user_task(db)
    seed_grant(db)
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    started = _agent_starts(worker, monkeypatch)
    cloned = _clone_from(worker, monkeypatch, origin)

    assert worker.run() == 0
    assert cloned == [True] and started == [True]
    assert f"swarm-tenant-{TENANT}-{slot()}" in client.accessed
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert user_token() not in log_stream.getvalue()


def test_a_stale_generation_exits_before_any_grant_or_credential_read(
    db, worker_factory, monkeypatch
):
    """Invariant 5 first: fencing, then the signature, then the credential."""
    _seed_user_task(db, task_generation=2)
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    started = _agent_starts(worker, monkeypatch)
    reads = grant_reads(monkeypatch, db)
    lease_before = dict(db.doc("leases/lease_1"))

    assert worker.run() == lifecycle.ExitCode.GENERATION_FENCED

    assert reads == [], "a superseded worker read the grant"
    assert client.accessed == [], "a superseded worker read a credential"
    assert started == []
    assert db.doc("leases/lease_1") == lease_before, "a superseded worker touched the lease"


def test_a_read_grant_ends_a_pull_request_step_before_the_agent(
    db, worker_factory, monkeypatch, log_stream
):
    _seed_user_task(db, access="read", dispatch={"strategy": "direct-pr"})
    seed_grant(db, mode="read")
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    started = _agent_starts(worker, monkeypatch)
    monkeypatch.setattr(worker, "_maybe_clone", lambda task: None)

    assert worker.run() == lifecycle.ExitCode.FAILED
    assert started == []
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "cannot_start"
    assert task["result_summary"]["forge_check"]["cause"] == "forge_read_grant"


def _push_attempt(worker_factory, monkeypatch, origin: Path, task: dict[str, Any],
                  client: FakeSecretClient):
    worker, config, _ = worker_factory(
        task_id="t-push", attempt_id="att-t-push", lease_id="lease-t-push",
        repository_url=f"file://{origin}", secret_client=client,
    )
    seed_attempt(worker.db, task_id="t-push", attempt_id="att-t-push", lease_id="lease-t-push")
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    _verified(worker, task)
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "agent.txt").write_text("the agent's work\n")
    return worker


def test_a_read_grant_never_reads_a_push_credential(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    """An integrate contributor pushes its branch; on a read grant it does not."""
    seed_grant(db, mode="read")
    client = secret_client()
    task = {
        "task_id": "t-push", "repository_url": GITHUB_URL, "forge_credential": slot(),
        "forge_access": "read",
        "metadata": {"dispatch": {"strategy": "integrate", "role": "contributor"}},
    }
    worker = _push_attempt(worker_factory, monkeypatch, origin, task, client)
    reads_at_clone = len(client.accessed)
    before = refs(origin)

    out = worker._harvest_git(publish=True)

    assert out["published"] is False
    assert out["forge_write_refused"]["cause"] == "forge_read_grant"
    assert refs(origin) == before, "a read grant pushed"
    assert forge.probes == [] and forge.pulls == []
    assert len(client.accessed) == reads_at_clone, "a push credential was read for a read grant"


def test_a_grant_deleted_before_the_push_refuses_it(
    db, worker_factory, monkeypatch, origin, local_urls, forge
):
    seed_grant(db)
    client = secret_client()
    task = {
        "task_id": "t-push", "repository_url": GITHUB_URL, "forge_credential": slot(),
        "forge_access": "write", "metadata": {"dispatch": {"strategy": "direct-pr"}},
    }
    worker = _push_attempt(worker_factory, monkeypatch, origin, task, client)
    reads_at_clone = len(client.accessed)
    del db.documents[grant_path()]
    before = refs(origin)

    out = worker._harvest_git(publish=True)

    assert out["forge_write_refused"]["cause"] == "forge_grant_revoked"
    assert refs(origin) == before and forge.pulls == []
    assert len(client.accessed) == reads_at_clone, "the token was read for a refused push"


@pytest.mark.parametrize("mode", ["write", "read"])
def test_a_grant_turned_read_refuses_the_push_and_a_write_grant_publishes(
    db, worker_factory, monkeypatch, origin, local_urls, forge, mode
):
    seed_grant(db)
    client = secret_client()
    task = {
        "task_id": "t-push", "repository_url": GITHUB_URL, "forge_credential": slot(),
        "metadata": {"dispatch": {"strategy": "direct-pr"}},
    }
    worker = _push_attempt(worker_factory, monkeypatch, origin, task, client)
    db.doc(grant_path())["mode"] = mode

    out = worker._harvest_git(publish=True)

    if mode == "write":
        assert out["published"] is True, out.get("publish_reason")
        assert "forge_write_refused" not in out
        assert forge.pulls, "the control opened no pull request"
    else:
        assert out["forge_write_refused"]["cause"] == "forge_grant_revoked"
        assert forge.pulls == []


def test_a_grant_deleted_during_the_run_checkpoints_then_refuses_and_ends(
    db, worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    """The whole attempt: cloned on a standing grant, the grant removed while
    the agent ran, then the final checkpoint, the refused push, the end."""
    _seed_user_task(db, dispatch={"strategy": "direct-pr"})
    seed_grant(db)
    client = secret_client()
    worker, _, _ = worker_factory(secret_client=client)
    _clone_from(worker, monkeypatch, origin)
    order: list[str] = []
    real_checkpoint = worker._checkpoint

    def checkpoint(label: str, **kwargs: Any):
        order.append(f"checkpoint:{label}")
        return real_checkpoint(label, **kwargs)

    real_upload = worker._upload_outputs

    def upload(**kwargs: Any):
        if kwargs.get("defer_publish") and db.documents.get(grant_path()) is not None:
            # The agent's change, and the person removing the repository.
            (worker.ws.work / lifecycle.REPO_DIR_NAME / "agent.txt").write_text("work\n")
            (worker.ws.artifacts / lifecycle.PR_TITLE_FILE).write_text("The agent's title\n")
            del db.documents[grant_path()]
            order.append("grant_deleted")
        return real_upload(**kwargs)

    monkeypatch.setattr(worker, "_checkpoint", checkpoint)
    monkeypatch.setattr(worker, "_upload_outputs", upload)
    before = refs(origin)

    assert worker.run() == lifecycle.ExitCode.FAILED

    assert "checkpoint:final" in order
    assert order.index("checkpoint:final") < order.index("grant_deleted")
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == "publish_refused"
    assert task["last_error"].startswith("forge_grant_revoked:"), task["last_error"]
    assert task["result_summary"]["forge_check"]["cause"] == "forge_grant_revoked"
    assert task["latest_checkpoint"], "no checkpoint holds the work"
    assert refs(origin) == before and forge.pulls == []
    assert db.doc("leases/lease_1")["released_at"] is not None
    assert user_token() not in log_stream.getvalue()
    assert user_token() not in str(task)


def test_the_grant_is_tenant_scoped(db):
    """A document at the id that names another tenant or person is no grant."""
    seed_grant(db, tenant_id="research")
    assert secrets_mod.grant_refusal(
        db, tenant_id=TENANT, suffix=slot(), repository_url=GITHUB_URL, write=False,
    )
    seed_grant(db, user_hash=hashlib.sha256(b"someone").hexdigest()[:16])
    assert secrets_mod.grant_refusal(
        db, tenant_id=TENANT, suffix=slot(), repository_url=GITHUB_URL, write=False,
    )
    seed_grant(db)
    assert secrets_mod.grant_refusal(
        db, tenant_id=TENANT, suffix=slot(), repository_url=GITHUB_URL, write=True,
    ) is None
    # A tenant token is not a person's: no grant stands behind it.
    assert secrets_mod.grant_refusal(
        db, tenant_id=TENANT, suffix="git", repository_url=GITHUB_URL, write=True,
    ) is None
