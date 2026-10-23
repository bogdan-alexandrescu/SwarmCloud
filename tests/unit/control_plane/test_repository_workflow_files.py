"""A registration's `workflow_files` switch (#2029, WF-APP-TOKEN).

BGD, 2026-10-22 (decision 2): "Self-serve, per-repo opt-in": a switch on each
repository registration, off by default, set by the workspace's admin when the
repository is added or later, and audited like `agents_may_file_issues`. A
branch workflow runs with the repository's secrets before any merge hold,
which is why it is per repository and off by default. What these tests hold:

  * off unless stored as a real `True` (`workflow_files_of`);
  * served on the registration's read;
  * only the workspace's admin may set it, on create or PATCH: a member's
    answers 403 with the same refusal text shape as `agents_may_file_issues`,
    with nothing written;
  * each change writes ONE append-only `admin_audit` entry in the same
    transaction; a PATCH that changes nothing audits nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api import codeintel, refusals, repositories
from swarm_api.repositories import repo_id_for
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant
from .repo_fakes import GitHubRepos, TenantTokens, make_client, repo_entry
from .test_repository_hard_stops import commits  # noqa: F401  (fixture)

CAROL = "carol@example.com"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
ENG_WIDGETS = repo_id_for("eng", "example-org", "widgets")
CAROL_NOTES = repo_id_for(CAROL_TENANT, "example-org", "notes")
FIELD = "workflow_files"


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    for tenant_id, repo_id, repo in (("eng", ENG_WIDGETS, "widgets"),
                                     (CAROL_TENANT, CAROL_NOTES, "notes")):
        db.docs[f"repositories/{repo_id}"] = {
            "repo_id": repo_id, "tenant_id": tenant_id, "owner": "example-org", "repo": repo,
            "default_branch": "main", "created_at": NOW, "index": {},
        }
    return db


def _patch(client, repo_id, body, user):
    return client.patch(f"/v1/repositories/{repo_id}", json=body, headers=auth_header(user))


def _get(client, repo_id, user):
    response = client.get(f"/v1/repositories/{repo_id}", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["repository"]


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


@pytest.mark.parametrize("stored", [None, False, "true", 1, "yes"])
def test_only_a_stored_true_reads_on(stored):
    data = {} if stored is None else {FIELD: stored}
    # MUTATION: read it with `bool(...)` and "true", 1 and "yes" read on.
    assert repositories.workflow_files_of(data) is False


def test_platform_does_not_turn_it_on():
    # MUTATION: default it to `platform_of` (as agents_may_file_issues does) and this is True.
    assert repositories.workflow_files_of({"platform": True}) is False
    assert repositories.workflow_files_of({FIELD: True}) is True


def test_it_is_served_on_the_read(client, db, seeded):
    # MUTATION: drop the field from `to_api` and this is a KeyError.
    assert _get(client, CAROL_NOTES, "carol")[FIELD] is False
    db.docs[f"repositories/{CAROL_NOTES}"][FIELD] = True
    assert _get(client, CAROL_NOTES, "carol")[FIELD] is True


def test_a_non_boolean_is_a_422(client, db, seeded):
    # MUTATION: type it `bool` rather than StrictBool and "true" is accepted.
    assert _patch(client, CAROL_NOTES, {FIELD: "true"}, "carol").status_code == 422
    assert FIELD not in db.docs[f"repositories/{CAROL_NOTES}"]


def test_a_members_change_is_403_and_nothing_is_written(client, db, seeded, monkeypatch):
    monkeypatch.delenv(refusals.env_name(codeintel.OwnerRequired.code), raising=False)
    response = _patch(client, ENG_WIDGETS, {FIELD: True}, "alice")
    # MUTATION: pass `auth.email` as `workflow_files_by` and this is 200.
    assert response.status_code == 403, response.text
    assert FIELD not in db.docs[f"repositories/{ENG_WIDGETS}"]
    assert _audit(db) == []


def test_the_store_refuses_a_change_without_the_admin_who_made_it(db, seeded):
    body = repositories.RepositoryPatch(workflow_files=True)
    store = repositories.Repositories(db, now=lambda: NOW)
    # MUTATION: drop the `workflow_files_by` check in `Repositories.patch`.
    with pytest.raises(repositories.Forbidden) as refused:
        store.patch("eng", ENG_WIDGETS, body)
    # The same refusal text shape as agents_may_file_issues.
    assert refused.value.message == "only the workspace's admin may set workflow_files"
    assert FIELD not in db.docs[f"repositories/{ENG_WIDGETS}"]


def test_an_admins_change_is_one_audit_entry_in_the_same_transaction(
        client, db, seeded, commits):  # noqa: F811
    commits.clear()
    response = _patch(client, CAROL_NOTES, {FIELD: True}, "carol")
    assert response.status_code == 200, response.text
    assert response.json()["repository"][FIELD] is True
    # MUTATION: skip `changes[FIELD]` and this is missing.
    assert db.docs[f"repositories/{CAROL_NOTES}"][FIELD] is True
    # MUTATION: skip `_workflow_files_audit` and there is no entry.
    (entry,) = _audit(db)
    assert entry["action"] == "repository_workflow_files_set"
    assert entry["target_repo_id"] == CAROL_NOTES
    assert entry["by"] == CAROL
    assert entry["detail"]["tenant_id"] == CAROL_TENANT
    assert entry["detail"]["repository"] == "example-org/notes"
    assert entry["detail"][FIELD] == {"from": False, "to": True}
    # MUTATION: write the audit outside the transaction and it is a second commit.
    writing = [paths for paths in commits if f"repositories/{CAROL_NOTES}" in paths]
    assert len(writing) == 1
    assert sorted({p.split("/")[0] for p in writing[0]}) == ["admin_audit", "repositories"]
    # Turned off again: a second entry, `from` true.
    assert _patch(client, CAROL_NOTES, {FIELD: False}, "carol").status_code == 200
    assert repositories.workflow_files_of(db.docs[f"repositories/{CAROL_NOTES}"]) is False
    entries = sorted(_audit(db), key=lambda e: e["detail"][FIELD]["from"])
    assert [e["detail"][FIELD] for e in entries] == [{"from": False, "to": True},
                                                     {"from": True, "to": False}]


def test_a_patch_that_changes_nothing_audits_nothing(client, db, seeded):
    response = _patch(client, CAROL_NOTES, {FIELD: False}, "carol")
    assert response.status_code == 200, response.text
    # MUTATION: audit every PATCH that carries the field and this has an entry.
    assert _audit(db) == []


WIDGETS = repo_entry("example-org/widgets", default_branch="main", private=True)


@pytest.fixture
def make(db, tokens, group_map, objects):
    def build():
        transport = GitHubRepos(repos={"example-org/widgets": WIDGETS})
        return make_client(db, tokens, group_map, objects, forge_tokens=TenantTokens(),
                           transport=transport)
    return build


def test_an_admin_sets_it_when_adding_the_repository_audited_in_the_create(
        make, db, commits, monkeypatch):  # noqa: F811
    client = make()
    response = client.post("/v1/repositories", headers=auth_header("root"),
                           json={"repository": "example-org/widgets", FIELD: True})
    assert response.status_code == 201, response.text
    record = response.json()["repository"]
    # MUTATION: drop it from `registration_record` and this is False.
    assert record[FIELD] is True
    path = f"repositories/{record['repo_id']}"
    assert db.docs[path][FIELD] is True
    entries = [e for e in _audit(db) if e["action"] == "repository_workflow_files_set"]
    # MUTATION: skip the create's audit and there is no entry.
    assert len(entries) == 1 and entries[0]["detail"][FIELD] == {"from": False, "to": True}
    writing = [paths for paths in commits if path in paths]
    assert len(writing) == 1
    assert sorted({p.split("/")[0] for p in writing[0]}) == ["admin_audit", "repositories"]


def test_a_members_create_carrying_it_is_403_before_anything_is_written(make, db, monkeypatch):
    monkeypatch.delenv(refusals.env_name(codeintel.OwnerRequired.code), raising=False)
    client = make()
    response = client.post("/v1/repositories", headers=auth_header("alice"),
                           json={"repository": "example-org/widgets", FIELD: True})
    # MUTATION: skip the admin gate on create and this is 201.
    assert response.status_code == 403, response.text
    assert not [p for p in db.docs if p.startswith("repositories/")]
    assert _audit(db) == []


def test_the_store_refuses_a_create_with_it_on_and_no_admin(db):
    store = repositories.Repositories(db, now=lambda: NOW)
    record = {"repo_id": "repo_" + "0" * 16, "tenant_id": "eng", "owner": "o", "repo": "r",
              FIELD: True}
    # MUTATION: drop the create's check and it is stored with no one to audit.
    with pytest.raises(ValueError):
        store.create(record)
    assert not [p for p in db.docs if p.startswith("repositories/")]
