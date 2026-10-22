"""The `-git-wf` slot's broker half (docs/design/agent-autonomy.md §3.3; lane AU-1, part of #1933).

Owner decision, 2026-10-22 (Q3): "A -git-wf slot read only by swarm-api,
platform repo only, merge held NEEDS_OWNER". `git_push_branch` reads
`swarm-tenant-<tenant>-git-wf` only when the pushed range touches
`.github/workflows/**` or `.github/actions/**`, only for a `platform: true`
registration, and only when the slot holds a version. Otherwise the push is
refused `workflow_scope_missing`, naming the files, before anything reaches
the forge -- never GitHub's generic `remote_rejected`.

Real git against a `file://` bare repository (lane G4's seam), the signed spec
and the attempt key of `test_git_tools_api`. The slot is a `StaticWorkflowSlot`
whose value is built at runtime.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from swarm_api import forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gitexec import GitExec
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import seed_tenant
from .test_git_tools_api import (  # noqa: F401  (fixtures)
    OWN_BRANCH,
    SIGNER,
    _commit,
    _settings,
    audit_rows,
    call,
    client,
    origin,
    push_fields,
    remote_tip,
    running_task,
    slots,
    worker_tokens,
)
from .test_git_tools_reads import forge, seed_registration  # noqa: F401  (forge: a fixture)


class Slot:
    """The `-git-wf` reader: a value per tenant, and every read recorded."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.read: list[str] = []


@pytest.fixture
def wf() -> Slot:
    return Slot()


@pytest.fixture
def app(db, worker_tokens, group_map, objects, forge, slots, origin, wf):  # noqa: F811
    from swarm_api.gittools import GitTools, InMemoryBundles, StaticWorkflowSlot
    from swarm_api.routes import gittools as routes
    from swarm_api.routes.children import _worker_verifier

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=_settings(), db=db, signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=slots,
        forge_writer=forgewrite.GitHubWriter(send=forge, locate=forge.locate, fetch=forge.fetch),
    )
    application = create_app(ctx)
    remote = f"file://{origin['remote']}"

    def service() -> GitTools:
        return GitTools.from_context(
            ctx, verifier=_worker_verifier(ctx),
            gitexec=GitExec(scratch_root=origin["tmp"] / "scratch", local_remotes=True),
            remote_url=lambda ref: remote, bundles=InMemoryBundles(),
            workflow_slot=StaticWorkflowSlot(wf.values, on_read=wf.read.append),
        )

    application.dependency_overrides[routes.git_tools] = service
    return application


def workflow_commit(origin: dict[str, Any], path: str = ".github/workflows/ci.yml") -> str:
    target = Path(origin["work"]) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    return _commit(origin["work"], path, "on: push")


def test_a_workflow_touching_push_with_no_git_wf_version_is_workflow_scope_missing(
        client, db, origin, wf):
    seed_registration(db, platform=True)
    attempt, _ = running_task(client, db)
    workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 422, response.text
    body = response.json()
    # MUTATION: skip the pushed range's path check and the push reaches the forge
    # (GitHub then answers `remote_rejected`; the file:// remote here lets it land).
    assert body["code"] == "workflow_scope_missing"
    assert body["code"] != "remote_rejected"
    assert ".github/workflows/ci.yml" in body["message"]
    assert remote_tip(origin, OWN_BRANCH) is None
    assert wf.read == ["eng"]
    row = audit_rows(db)[-1]
    assert row["code"] == "workflow_scope_missing"
    assert row["workflow_paths"] == [".github/workflows/ci.yml"]


def test_an_actions_file_counts_as_a_workflow_file(client, db, origin, wf):
    seed_registration(db, platform=True)
    attempt, _ = running_task(client, db)
    workflow_commit(origin, ".github/actions/setup/action.yml")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    # MUTATION: check only .github/workflows/ and this push lands.
    assert response.json()["code"] == "workflow_scope_missing"


def test_a_repository_that_is_not_platform_never_reads_the_slot(client, db, origin, wf):
    seed_registration(db, platform=False)
    wf.values["eng"] = "ghp" + "_" + secrets.token_hex(18)
    attempt, _ = running_task(client, db)
    workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 422, response.text
    # MUTATION: drop the `platform_of` check and the slot pushes to any repository.
    assert response.json()["code"] == "workflow_scope_missing"
    assert "platform" in response.json()["message"]
    assert wf.read == []
    assert remote_tip(origin, OWN_BRANCH) is None


def test_on_the_platform_repository_with_a_version_the_push_lands_under_the_slot(
        client, db, origin, wf, slots):
    seed_registration(db, platform=True)
    value = "ghp" + "_" + secrets.token_hex(18)
    wf.values["eng"] = value
    attempt, _ = running_task(client, db)
    sha = workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 200, response.text
    # MUTATION: refuse every workflow-touching push and this is 422.
    assert remote_tip(origin, OWN_BRANCH) == sha
    row = audit_rows(db)[-1]
    assert row["result"] == "ok" and row["slot"] == "git-wf"
    assert value not in response.text and value not in str(audit_rows(db))


def test_a_push_that_touches_no_workflow_file_never_reads_the_slot(client, db, origin, wf):
    seed_registration(db, platform=True)
    wf.values["eng"] = "ghp" + "_" + secrets.token_hex(18)
    attempt, _ = running_task(client, db)
    sha = _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 200, response.text
    assert remote_tip(origin, OWN_BRANCH) == sha
    # MUTATION: read the -git-wf slot on every push and this lists "eng".
    assert wf.read == []
    assert audit_rows(db)[-1]["slot"] == "git"
