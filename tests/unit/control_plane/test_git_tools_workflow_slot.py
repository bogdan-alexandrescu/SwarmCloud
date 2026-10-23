"""A workflow-touching push is made with the workspace's App token, per repository switch (#2029).

BGD, 2026-10-22 (WF-APP-TOKEN): the `-git-wf` slot is gone. `git_push_branch`
reads the pushed range's paths from the bundle; a range touching
`.github/workflows/**` or `.github/actions/**` is pushed only when the task's
OWN registration has `workflow_files` switched on, and then with a token the
SwarmCloud GitHub App mints for that one repository at push time
(`InstallationTokens.workflow_push_token`). Otherwise -- switch off, or a mint
that fails -- the push is refused `workflow_scope_missing`, naming the files,
before anything reaches the forge, and the agent writes `workflow-lines.patch`.

`platform: true` no longer matters here. A branch workflow runs with the
repository's secrets before any merge hold, which is why the switch is per
repository and off by default.

Real git against a `file://` bare repository (lane G4's seam), the signed spec
and the attempt key of `test_git_tools_api`. The mint is a fake whose token is
built at runtime.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

import pytest

from swarm_api import forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge_installation import (
    MINT_FAILED, MINT_NO_INSTALLATION, MINT_NOT_COVERED, MINT_NOT_GRANTED, WorkflowMintRefused,
)
from swarm_api.gitexec import GitExec
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import seed_tenant
from .test_git_tools_api import (  # noqa: F401  (fixtures)
    OWN_BRANCH,
    OWNER,
    REPO,
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

#: The advice every refusal ends with, kept whole (the worker reads it).
ADVICE = "Push without those files and write them as workflow-lines.patch"


class Mint:
    """`workflow_push_token`: a fresh runtime token per call, or a typed refusal."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.issued: list[str] = []
        self.refuse: str | None = None

    def workflow_push_token(self, tenant_id: str, owner: str, repo: str) -> str:
        self.calls.append((tenant_id, owner, repo))
        if self.refuse is not None:
            raise WorkflowMintRefused(self.refuse)
        token = "ghs" + "_" + secrets.token_hex(18)
        self.issued.append(token)
        return token


@pytest.fixture
def mint() -> Mint:
    return Mint()


@pytest.fixture
def app(db, worker_tokens, group_map, objects, forge, slots, origin, mint):  # noqa: F811
    from swarm_api.gittools import GitTools, InMemoryBundles
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
            workflow_tokens=mint,
        )

    application.dependency_overrides[routes.git_tools] = service
    return application


def switch(db, repo_id: str, on: bool | None) -> None:
    doc = db.docs[f"repositories/{repo_id}"]
    if on is None:
        doc.pop("workflow_files", None)
    else:
        doc["workflow_files"] = on


def workflow_commit(origin: dict[str, Any], path: str = ".github/workflows/ci.yml") -> str:
    target = Path(origin["work"]) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    return _commit(origin["work"], path, "on: push")


@pytest.mark.parametrize("on", [None, False, "true", 1])
def test_with_the_switch_off_a_workflow_push_is_refused_and_nothing_is_minted(
        client, db, origin, mint, on):
    repo_id = seed_registration(db)
    switch(db, repo_id, on)
    attempt, _ = running_task(client, db)
    workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 422, response.text
    body = response.json()
    # MUTATION: read `workflow_files` with `bool(...)` and "true"/1 push; drop
    # the check and every value pushes.
    assert body["code"] == "workflow_scope_missing"
    assert body["detail"]["paths"] == [".github/workflows/ci.yml"]
    # MUTATION: cut the message short and the worker loses the patch advice.
    assert body["message"].endswith(ADVICE)
    assert "workflow_files" in body["message"]
    assert ".github/workflows/ci.yml" in body["message"]
    # MUTATION: mint before checking the switch and this lists a call.
    assert mint.calls == []
    assert remote_tip(origin, OWN_BRANCH) is None
    row = audit_rows(db)[-1]
    assert row["code"] == "workflow_scope_missing"
    assert row["workflow_paths"] == [".github/workflows/ci.yml"]


def test_platform_true_no_longer_opens_a_workflow_push(client, db, origin, mint):
    seed_registration(db, platform=True)
    attempt, _ = running_task(client, db)
    workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    # MUTATION: keep `platform_of` as the gate and this reaches the mint.
    assert response.json()["code"] == "workflow_scope_missing"
    assert mint.calls == []


def test_an_actions_file_counts_as_a_workflow_file(client, db, origin, mint):
    seed_registration(db)
    attempt, _ = running_task(client, db)
    workflow_commit(origin, ".github/actions/setup/action.yml")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    # MUTATION: check only .github/workflows/ and this push lands.
    assert response.json()["code"] == "workflow_scope_missing"
    assert remote_tip(origin, OWN_BRANCH) is None


def test_with_the_switch_on_the_push_lands_with_one_app_mint_for_that_repository(
        client, db, origin, mint, caplog):
    repo_id = seed_registration(db)
    switch(db, repo_id, True)
    attempt, _ = running_task(client, db)
    sha = workflow_commit(origin)
    with caplog.at_level(logging.DEBUG):
        response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 200, response.text
    assert remote_tip(origin, OWN_BRANCH) == sha
    # MUTATION: mint for another tenant or repository and this differs.
    assert mint.calls == [("eng", OWNER, REPO)]
    (token,) = mint.issued
    row = audit_rows(db)[-1]
    # MUTATION: record the token (or nothing) as the credential and this fails.
    assert row["result"] == "ok" and row["credential"] == "app_installation"
    assert row["slot"] != "git-wf"
    # MUTATION: put the token in the answer, the audit or a log line.
    assert token not in response.text
    assert token not in str(audit_rows(db))
    assert token not in str(db.docs)
    assert token not in caplog.text


def test_a_second_tenant_switching_on_the_same_repository_mints_for_itself(client, db, origin,
                                                                           mint):
    # research registered the same repository with the switch on; eng did not.
    other = seed_registration(db, tenant_id="research")
    switch(db, other, True)
    seed_registration(db)
    attempt, _ = running_task(client, db)
    workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    # MUTATION: read the switch from any tenant's registration of the
    # repository and eng's push is minted under research's opt-in.
    assert response.json()["code"] == "workflow_scope_missing"
    assert mint.calls == []


@pytest.mark.parametrize("reason,sentence", [
    (MINT_NOT_GRANTED, "the workspace's GitHub App installation has not granted Workflows"),
    (MINT_NOT_COVERED, f"does not cover {OWNER}/{REPO}"),
    (MINT_NO_INSTALLATION, "has no installation"),
    (MINT_FAILED, "could not mint"),
])
def test_each_mint_failure_is_its_own_reason_inside_workflow_scope_missing(
        client, db, origin, mint, reason, sentence):
    repo_id = seed_registration(db)
    switch(db, repo_id, True)
    mint.refuse = reason
    attempt, _ = running_task(client, db)
    workflow_commit(origin)
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "workflow_scope_missing"
    # MUTATION: answer every failure with one sentence and three cases are red.
    assert sentence in body["message"]
    assert body["message"].endswith(ADVICE)
    assert body["detail"]["paths"] == [".github/workflows/ci.yml"]
    assert remote_tip(origin, OWN_BRANCH) is None


def test_a_push_that_touches_no_workflow_file_never_mints(client, db, origin, mint):
    repo_id = seed_registration(db)
    switch(db, repo_id, True)
    attempt, _ = running_task(client, db)
    sha = _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 200, response.text
    assert remote_tip(origin, OWN_BRANCH) == sha
    # MUTATION: mint on every push and this lists a call.
    assert mint.calls == []
    assert audit_rows(db)[-1]["slot"] == "git"


def test_the_git_wf_slot_is_gone_from_the_broker():
    from swarm_api import gittools

    # MUTATION: keep the slot reader and these names still resolve.
    for name in ("WORKFLOW_SLOT", "WorkflowSlot", "StaticWorkflowSlot",
                 "SecretManagerWorkflowSlot"):
        assert not hasattr(gittools, name), name
    assert "workflow_slot" not in gittools.GitTools.__dataclass_fields__
