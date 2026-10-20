"""A service submission clones with the credential the tenant's own checks
use (INDEX-CRED; part of #1041).

Owner report 2026-10-13 ~20:10 PDT: "it says all tests pass for
/repositories/tokens but it can't index the repo in my personal workspace?"
Measured 2026-10-14 ~03:10 UTC in u-bogdan: Try it passed for
sagaxyz/ai-studio through the owner's GitHub connection slot
(`-git-u-<hex>`), while the index run task_57773968e37d4d6d8300 ran with
`forge_credential` "git" -- the tenant `-git` slot, which a personal
workspace does not have -- and failed "Repository not found". Server-side
reads already resolved through `forge.resolve_forge_slot` (INDEX-USER-SLOT);
task submission's choice for SERVICE submissions did not.

Held here, over the in-memory Firestore with no credentials:

  * a service submission in a personal tenant with only its owner's user
    slot runs on that slot, and says so ("owner's GitHub connection");
  * a group tenant with a `-git` slot runs on it, unchanged, even when the
    submitter has a slot of their own;
  * with no credential, a private registered repository is refused up front
    with `no_github_credential`, and nothing is stored; a public one is not;
  * the index poll's back-off mark moves when the slot it would clone with
    moves, so ai-studio resumes on its own after the release -- and a tenant
    still on `-git` keeps its mark;
  * the repositories routes serve which credential indexing uses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api import forge, indexfailure
from swarm_api.auth import AuthContext, StaticTokenVerifier
from swarm_api.codec import forge_credential_source, task_to_api
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.gittokens import GitTokens, Scope, TokenState, provider_suffix, record_for_slot
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.repoindex import RepoIndex
from swarm_api.repositories import repo_id_for
from swarm_api.schemas import TaskCreate
from swarm_api.validation import FORGE_SOURCE_METADATA_KEY, RESERVED_METADATA_KEYS
from swarm_api.waker import NullWaker
from swarm_common.identity import Principal, tenant_id_for_user
from swarm_common.models import Tenant

from .conftest import ENG_GROUP, PROJECT, api_settings, seed_tenant
from .spec_signer import LocalSpecSigner

OWNER = "alice@example.com"
PERSONAL = tenant_id_for_user(OWNER)
PRIVATE = ("sagaxyz", "ai-studio")
NOW = datetime(2026, 10, 14, 3, 10, tzinfo=timezone.utc)


@pytest.fixture
def context(db, tokens, group_map):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=LocalSpecSigner(),
    )


def _personal_auth() -> AuthContext:
    return AuthContext(
        principal=Principal(email=OWNER, subject="sub-alice", domain="example.com", groups=()),
        tenant_id=PERSONAL,
        is_admin=False,
        tenant_principal=OWNER,
    )


def _eng_auth() -> AuthContext:
    return AuthContext(
        principal=Principal(email=OWNER, subject="sub-alice", domain="example.com", groups=()),
        tenant_id="eng",
        is_admin=False,
        tenant_principal=ENG_GROUP,
    )


def _user_slot(db, tenant_id: str, email: str, *, login: str | None = None,
               state: TokenState = TokenState.ACTIVE) -> None:
    record = record_for_slot(tenant_id, Scope.USER, user=email, registered_by=email, now=NOW)
    doc = record.to_firestore()
    doc["state"] = state.value
    if login is not None:
        doc["forge_login"] = login
    db.collection("git_tokens").document(record.token_id).set(doc)


def _register(db, tenant_id: str, owner: str, repo: str, visibility: str | None) -> str:
    """A registration as `Repositories.create` leaves it, as far as submission reads it."""
    repo_id = repo_id_for(tenant_id, owner, repo)
    db.collection("repositories").document(repo_id).set({
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "visibility": visibility,
    })
    return repo_id


def _spec(owner: str = PRIVATE[0], repo: str = PRIVATE[1]) -> TaskCreate:
    return TaskCreate(runner_profile="mock", input={"prompt": "index"},
                      repository_url=f"https://github.com/{owner}/{repo}.git")


def _stored(db) -> list[dict[str, Any]]:
    return [doc for key, doc in db.docs.items()
            if key.startswith("tasks/") and key.count("/") == 1]


# --------------------------------------------------------------------------
# which slot a service submission runs with
# --------------------------------------------------------------------------

def test_a_personal_tenants_service_submission_runs_on_its_owners_connection(context, db):
    _user_slot(db, PERSONAL, OWNER)
    _register(db, PERSONAL, *PRIVATE, "private")

    result = context.submissions.submit_tasks(_personal_auth(), [_spec()],
                                              service_submission=True)

    task = result.tasks[0]
    assert task.forge_credential == provider_suffix(Scope.USER, user=OWNER)
    assert task.forge_credential != "git", "the tenant -git slot u-bogdan never had"
    assert task.forge_access == "write"
    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "owner"
    served = task_to_api(task)
    assert served["forge_credential_source"] == "owner's GitHub connection, service submission"


def test_the_switch_off_fallback_in_a_personal_tenant_uses_the_same_rule(context, db):
    # REPOSITORY_GRANTS_ENFORCED off: an ungranted person's task ran "on the
    # tenant token", which in a personal workspace does not exist.
    _user_slot(db, PERSONAL, OWNER)
    result = context.submissions.submit_tasks(_personal_auth(), [_spec()])
    assert result.tasks[0].forge_credential == provider_suffix(Scope.USER, user=OWNER)


def test_a_group_tenant_with_its_git_slot_is_unchanged(context, db):
    seed_tenant(db, "eng", credentials=("git",))
    # The submitter's own slot in eng does not move a SERVICE run off -git.
    _user_slot(db, "eng", OWNER)

    result = context.submissions.submit_tasks(_eng_auth(), [_spec()], service_submission=True)

    task = result.tasks[0]
    assert task.forge_credential == "git"
    assert task.forge_access == "write"
    assert task.metadata[FORGE_SOURCE_METADATA_KEY] == "tenant"
    assert task_to_api(task)["forge_credential_source"] == "tenant token, service submission"


def test_the_shared_rule_is_the_one_reads_resolve_through(db):
    _user_slot(db, PERSONAL, OWNER)
    tenant = Tenant(tenant_id=PERSONAL, kind="user", principal=OWNER, created_at=NOW)
    index = forge.FirestoreSlotIndex(db)
    assert forge.service_slot(tenant, index) == forge.resolve_forge_slot(tenant, index=index)
    assert forge.service_slot(tenant, index).source == "owner"


def test_a_persons_own_task_says_its_their_credential_not_the_owners():
    # Only the platform's mark says "owner": a user slot without it is the submitter's.
    slot = provider_suffix(Scope.USER, user=OWNER)
    assert forge_credential_source(slot) == "the submitter's GitHub credential"
    assert forge_credential_source(slot, "owner") == "owner's GitHub connection, service submission"
    assert forge_credential_source("git", "owner") == "tenant token, service submission"


def test_the_source_mark_is_reserved_from_callers():
    assert FORGE_SOURCE_METADATA_KEY in RESERVED_METADATA_KEYS


# --------------------------------------------------------------------------
# no credential: refused up front for a private repository
# --------------------------------------------------------------------------

def test_no_credential_and_a_private_repository_is_refused_before_anything_is_queued(
        context, db):
    _register(db, PERSONAL, *PRIVATE, "private")

    with pytest.raises(forge.NoForgeCredential) as refused:
        context.submissions.submit_tasks(_personal_auth(), [_spec()], service_submission=True)

    assert refused.value.status_code == 409
    assert refused.value.code == "no_forge_credential"
    text = str(refused.value)
    assert text.startswith(f"No GitHub credential for {PERSONAL}: connect GitHub under "
                           "Setup › Git tokens")
    assert "sagaxyz/ai-studio" in text
    assert refused.value.detail["repositories"] == ["sagaxyz/ai-studio"]
    assert _stored(db) == []


def test_a_revoked_connection_is_no_credential(context, db):
    _user_slot(db, PERSONAL, OWNER, state=TokenState.REVOKED)
    _register(db, PERSONAL, *PRIVATE, "private")
    with pytest.raises(forge.NoForgeCredential):
        context.submissions.submit_tasks(_personal_auth(), [_spec()], service_submission=True)


def test_no_credential_and_a_public_repository_still_runs(context, db):
    # Public repositories indexed fine in u-bogdan: no token is needed.
    _register(db, PERSONAL, "bogdan-alexandrescu", "claudeswitch", "public")
    result = context.submissions.submit_tasks(
        _personal_auth(), [_spec("bogdan-alexandrescu", "claudeswitch")],
        service_submission=True)
    assert result.tasks[0].forge_credential == "git"


# --------------------------------------------------------------------------
# the poll's back-off follows the credential it would use
# --------------------------------------------------------------------------

class _Tenants:
    def __init__(self, *tenants: Tenant) -> None:
        self._by_id = {t.tenant_id: t for t in tenants}

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self._by_id.get(tenant_id)


def _index(db, *tenants: Tenant) -> RepoIndex:
    return RepoIndex(db, store=_Tenants(*tenants), submissions=None, inspection=None,
                     tokens=None, forge=None, now=lambda: NOW)


def _mark_before_the_release(db, tenant_id: str) -> str:
    """What `_credential_mark` recorded before INDEX-CRED: the records alone."""
    return indexfailure.credential_mark(GitTokens(db, now=lambda: NOW).list(tenant_id))


def test_the_new_slot_counts_as_a_credential_change(db):
    _user_slot(db, PERSONAL, OWNER)
    tenant = Tenant(tenant_id=PERSONAL, kind="user", principal=OWNER, created_at=NOW)
    backoff = {"credential": _mark_before_the_release(db, PERSONAL), "failures": 2}

    mark = _index(db, tenant)._credential_mark(PERSONAL)

    assert mark is not None
    assert indexfailure.credential_changed(backoff, mark), (
        "ai-studio must resume on its own once its runs clone with the owner's slot")
    # And once recorded under the new rule, an unchanged slot holds it.
    assert not indexfailure.credential_changed({"credential": mark}, mark)


def test_a_tenant_still_on_its_git_slot_keeps_its_mark(db):
    tenant = Tenant(tenant_id="eng", kind="group", principal=ENG_GROUP, created_at=NOW,
                    credentials=["git"])
    _user_slot(db, "eng", OWNER)
    before = _mark_before_the_release(db, "eng")
    assert _index(db, tenant)._credential_mark("eng") == before


# --------------------------------------------------------------------------
# the console is told which credential indexing uses
# --------------------------------------------------------------------------

def test_the_view_names_the_owners_connection_and_its_login(db):
    _user_slot(db, PERSONAL, OWNER, login="bogdan-alexandrescu")
    tenant = Tenant(tenant_id=PERSONAL, kind="user", principal=OWNER, created_at=NOW)
    view = forge.service_credential_view(tenant, forge.FirestoreSlotIndex(db))
    assert view == {
        "source": "owner",
        "secret_name": f"swarm-tenant-{PERSONAL}-{provider_suffix(Scope.USER, user=OWNER)}",
        "login": "bogdan-alexandrescu",
    }


def test_the_view_says_none_when_there_is_no_credential(db):
    tenant = Tenant(tenant_id=PERSONAL, kind="user", principal=OWNER, created_at=NOW)
    view = forge.service_credential_view(tenant, forge.FirestoreSlotIndex(db))
    assert view == {"source": "none", "secret_name": None, "login": None}


def test_the_view_says_the_team_token_for_a_tenant_with_git(db):
    tenant = Tenant(tenant_id="eng", kind="group", principal=ENG_GROUP, created_at=NOW,
                    credentials=["git"])
    view = forge.service_credential_view(tenant, forge.FirestoreSlotIndex(db))
    assert view["source"] == "tenant"
    assert view["secret_name"] == "swarm-tenant-eng-git"


def test_the_repositories_routes_serve_the_index_credential(db, tokens, group_map, objects):
    from .conftest import auth_header
    from .repo_fakes import TenantTokens, make_client
    from .repo_index_fakes import IndexGitHub, sha

    seed_tenant(db, "eng", credentials=("git",))
    client = make_client(db, tokens, group_map, objects, forge_tokens=TenantTokens(),
                         transport=IndexGitHub(heads={"main": sha("one")}))
    created = client.post("/v1/repositories", json={"repository": "example-org/widgets"},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]

    listed = client.get("/v1/repositories", headers=auth_header("alice"))
    one = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice"))

    assert listed.status_code == 200, listed.text
    [row] = listed.json()["repositories"]
    assert row["index_credential"]["source"] == "tenant"
    assert row["index_credential"]["secret_name"] == "swarm-tenant-eng-git"
    assert one.json()["repository"]["index_credential"] == row["index_credential"]


def test_a_group_tenant_with_no_listed_slot_is_not_refused(context, db):
    # An operator can store swarm-tenant-<t>-git with create-secrets.sh and
    # nothing swarm-api reads lists it: the clone may well work, so a group
    # tenant's run is dispatched on `git` as before rather than refused.
    _register(db, "eng", *PRIVATE, "private")
    result = context.submissions.submit_tasks(_eng_auth(), [_spec()], service_submission=True)
    assert result.tasks[0].forge_credential == "git"
