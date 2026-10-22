"""A registration's `agents_may_file_issues` (AU-0 REG-FLAG, part of #1933).

docs/design/agent-autonomy.md §3.2, owner decision Q2 (2026-10-22): "Per
repository registration, admin-set, off by default". An agent may write
issues and comments only when its tenant's registration of the repository
says `agents_may_file_issues: true`. What these tests hold:

  * a registration that never set the field reads off;
  * the platform's own registration (`platform: true`) reads on, and an
    admin's explicit `false` on it reads off;
  * a member's change is 403 with nothing written;
  * an admin's change writes `agents_may_file_issues_set` to `admin_audit` in
    the same transaction as the registration; a PATCH that changes nothing
    audits nothing;
  * another workspace's registration is a 404, and a non-boolean a 422.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api import codeintel, refusals, repositories
from swarm_api.repositories import repo_id_for
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant
from .test_repository_hard_stops import commits  # noqa: F401  (fixture)

CAROL = "carol@example.com"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ENG_WIDGETS = repo_id_for("eng", "example-org", "widgets")
CAROL_NOTES = repo_id_for(CAROL_TENANT, "example-org", "notes")
FIELD = "agents_may_file_issues"


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


def test_a_registration_without_the_field_reads_off(client, db, seeded):
    stored = db.docs[f"repositories/{CAROL_NOTES}"]
    assert FIELD not in stored
    # MUTATION: make `agents_may_file_issues_of` default to True and this fails.
    assert repositories.agents_may_file_issues_of(stored) is False
    # MUTATION: drop the field from `to_api` and this is a KeyError.
    assert _get(client, CAROL_NOTES, "carol")[FIELD] is False


def test_a_malformed_value_reads_off(seeded):
    # MUTATION: read the stored value with `bool(...)` and the string reads on.
    assert repositories.agents_may_file_issues_of({FIELD: "true"}) is False


def test_a_platform_registration_reads_on(client, db, seeded):
    db.docs[f"repositories/{ENG_WIDGETS}"]["platform"] = True
    # MUTATION: drop the `platform_of` default and this reads off.
    assert repositories.agents_may_file_issues_of(db.docs[f"repositories/{ENG_WIDGETS}"]) is True
    assert _get(client, ENG_WIDGETS, "alice")[FIELD] is True
    # CONTROL: the same registration without `platform` reads off.
    db.docs[f"repositories/{ENG_WIDGETS}"]["platform"] = False
    assert _get(client, ENG_WIDGETS, "alice")[FIELD] is False


def test_an_explicit_false_on_a_platform_registration_reads_off(seeded):
    # MUTATION: OR the stored value with `platform_of` and an admin can never
    # turn the platform's own repository off.
    assert repositories.agents_may_file_issues_of({"platform": True, FIELD: False}) is False


def test_a_members_change_is_403_and_nothing_is_written(client, db, seeded, monkeypatch):
    # alice is a member of eng: she may PATCH its schedule, not this.
    monkeypatch.delenv(refusals.env_name(codeintel.OwnerRequired.code), raising=False)
    response = _patch(client, ENG_WIDGETS, {FIELD: True}, "alice")
    # MUTATION: pass `auth.email` as `file_issues_by` in `patch_repository`
    # instead of `_context_by(...)` and this is 200.
    assert response.status_code == 403, response.text
    assert FIELD not in db.docs[f"repositories/{ENG_WIDGETS}"]
    assert _audit(db) == []
    # CONTROL: the same member's PATCH of a member's field still goes through.
    assert _patch(client, ENG_WIDGETS, {"paused": True}, "alice").status_code == 200


def test_the_store_refuses_a_change_without_the_admin_who_made_it(db, seeded):
    body = repositories.RepositoryPatch(agents_may_file_issues=True)
    store = repositories.Repositories(db, now=lambda: NOW)
    # MUTATION: drop the `file_issues_by` check in `Repositories.patch` and
    # this writes without anyone to audit.
    with pytest.raises(repositories.Forbidden):
        store.patch("eng", ENG_WIDGETS, body)
    assert FIELD not in db.docs[f"repositories/{ENG_WIDGETS}"]


def test_an_admins_change_is_audited_in_the_same_transaction(
        client, db, seeded, commits):  # noqa: F811
    commits.clear()
    response = _patch(client, CAROL_NOTES, {FIELD: True}, "carol")
    assert response.status_code == 200, response.text
    assert response.json()["repository"][FIELD] is True
    # MUTATION: skip `changes[FIELD]` in `Repositories.patch` and this is missing.
    assert db.docs[f"repositories/{CAROL_NOTES}"][FIELD] is True
    # MUTATION: skip `_file_issues_audit` and there is no entry.
    (entry,) = _audit(db)
    # MUTATION: rename `AUDIT_AGENTS_MAY_FILE_ISSUES_SET` and this fails.
    assert entry["action"] == "agents_may_file_issues_set"
    assert entry["target_repo_id"] == CAROL_NOTES
    assert entry["by"] == CAROL
    assert entry["detail"]["tenant_id"] == CAROL_TENANT
    assert entry["detail"]["repository"] == "example-org/notes"
    assert entry["detail"][FIELD] == {"from": False, "to": True}
    # MUTATION: write the audit outside the transaction and it is a second commit.
    writing = [paths for paths in commits if f"repositories/{CAROL_NOTES}" in paths]
    assert len(writing) == 1
    assert sorted({p.split("/")[0] for p in writing[0]}) == ["admin_audit", "repositories"]


def test_a_platform_admin_turns_the_platform_repository_off(client, db, seeded):
    db.docs[f"repositories/{ENG_WIDGETS}"]["platform"] = True
    response = _patch(client, ENG_WIDGETS, {FIELD: False}, "root")
    assert response.status_code == 200, response.text
    assert response.json()["repository"][FIELD] is False
    (entry,) = _audit(db)
    assert entry["by"] == "root@example.com"
    # MUTATION: audit `from` the stored value instead of the reading and this is None.
    assert entry["detail"][FIELD] == {"from": True, "to": False}


def test_a_patch_that_changes_nothing_audits_nothing(client, db, seeded):
    response = _patch(client, CAROL_NOTES, {FIELD: False}, "carol")
    assert response.status_code == 200, response.text
    # MUTATION: audit every PATCH that carries the field and this has one entry.
    assert _audit(db) == []


def test_another_workspaces_registration_is_not_found(client, db, seeded):
    response = _patch(client, ENG_WIDGETS, {FIELD: True}, "carol")
    # MUTATION: patch without re-checking the tenant and this is 200.
    assert response.status_code == 404, response.text
    assert FIELD not in db.docs[f"repositories/{ENG_WIDGETS}"]
    assert _audit(db) == []


def test_a_non_boolean_is_422(client, db, seeded):
    response = _patch(client, CAROL_NOTES, {FIELD: "yes"}, "carol")
    # MUTATION: type the field `bool` instead of `StrictBool` and "yes" coerces.
    assert response.status_code == 422, response.text
    assert FIELD not in db.docs[f"repositories/{CAROL_NOTES}"]
