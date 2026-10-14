"""A repository's `context` override: PATCH /v1/repositories/{id} (CTXQ-SET).

docs/design/context-quality.md §4.7: whether a step on a repository is staged
with graph context is a STORED setting -- the registration's `context`
(`on` | `off`), over the workspace's code-intelligence default -- never a
caller's choice. What these tests hold:

  * a registration starts with no override (`inherit`);
  * the workspace's owner or a platform admin sets it; a member is 403 with
    nothing written once `code_intelligence_owner_required` is on, and is let
    through and audited while that switched refusal ships off;
  * `inherit` clears it -- stored null, the workspace's value again, which is
    what `agent_worker.graphstage.decide` then reads;
  * each change is one `admin_audit` row written in the same transaction as
    the registration, and a failed write writes neither; a PATCH that
    changes nothing audits nothing;
  * anything but the three words is a 422.

MUTATION: drop the owner/admin gate on `context` in `patch_repository` and
`test_a_member_cannot_set_it_once_the_switch_is_on` fails; skip the audit and
`test_the_owner_sets_it_and_it_is_audited_in_the_same_transaction` fails.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_worker import graphstage
from swarm_api import codeintel, refusals, repositories
from swarm_api.repositories import repo_id_for
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant
from .test_repository_hard_stops import commits  # noqa: F401  (fixture)

CAROL = "carol@saga.xyz"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ENG_WIDGETS = repo_id_for("eng", "saga-xyz", "widgets")
CAROL_NOTES = repo_id_for(CAROL_TENANT, "saga-xyz", "notes")


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    for tenant_id, repo_id, repo in (("eng", ENG_WIDGETS, "widgets"),
                                     (CAROL_TENANT, CAROL_NOTES, "notes")):
        db.docs[f"repositories/{repo_id}"] = {
            "repo_id": repo_id, "tenant_id": tenant_id, "owner": "saga-xyz", "repo": repo,
            "default_branch": "main", "created_at": NOW, "index": {},
        }
    return db


def _patch(client, repo_id, body, user):
    return client.patch(f"/v1/repositories/{repo_id}", json=body, headers=auth_header(user))


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def test_a_registration_starts_with_no_override(client, db, seeded):
    response = client.get(f"/v1/repositories/{CAROL_NOTES}", headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["context"] == "inherit"
    assert repositories.context_of(db.docs[f"repositories/{CAROL_NOTES}"]) is None


def test_the_owner_sets_it_and_it_is_audited_in_the_same_transaction(
        client, db, seeded, commits):  # noqa: F811
    commits.clear()
    response = _patch(client, CAROL_NOTES, {"context": "off"}, "carol")
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["context"] == "off"
    assert db.docs[f"repositories/{CAROL_NOTES}"]["context"] == "off"
    (entry,) = _audit(db)
    assert entry["action"] == repositories.AUDIT_CONTEXT_SET
    assert entry["target_repo_id"] == CAROL_NOTES
    assert entry["by"] == CAROL
    assert entry["detail"]["tenant_id"] == CAROL_TENANT
    assert entry["detail"]["context"] == {"from": None, "to": "off"}
    writing = [paths for paths in commits if f"repositories/{CAROL_NOTES}" in paths]
    assert len(writing) == 1
    assert sorted({p.split("/")[0] for p in writing[0]}) == ["admin_audit", "repositories"]


def test_a_platform_admin_sets_it(client, db, seeded):
    response = _patch(client, ENG_WIDGETS, {"context": "on"}, "root")
    assert response.status_code == 200, response.text
    assert db.docs[f"repositories/{ENG_WIDGETS}"]["context"] == "on"
    (entry,) = _audit(db)
    assert entry["by"] == "root@saga.xyz"


def test_a_member_cannot_set_it_once_the_switch_is_on(client, db, seeded, monkeypatch):
    # alice is a member of eng: she may PATCH its schedule, not this.
    monkeypatch.setenv(refusals.env_name(codeintel.OwnerRequired.code), "on")
    response = _patch(client, ENG_WIDGETS, {"context": "off"}, "alice")
    assert response.status_code == 403, response.text
    assert "context" not in db.docs[f"repositories/{ENG_WIDGETS}"]
    assert _audit(db) == []
    # CONTROL: the same member's PATCH of a member's field still goes through.
    assert _patch(client, ENG_WIDGETS, {"paused": True}, "alice").status_code == 200


def test_a_member_sets_it_while_the_switch_is_off(client, db, seeded, monkeypatch):
    monkeypatch.delenv(refusals.env_name(codeintel.OwnerRequired.code), raising=False)
    response = _patch(client, ENG_WIDGETS, {"context": "off"}, "alice")
    assert response.status_code == 200, response.text
    assert db.docs[f"repositories/{ENG_WIDGETS}"]["context"] == "off"
    (entry,) = _audit(db)
    assert entry["by"] == "alice@saga.xyz"


def test_another_workspaces_repository_is_not_found(client, db, seeded):
    response = _patch(client, ENG_WIDGETS, {"context": "off"}, "carol")
    assert response.status_code == 404, response.text
    assert "context" not in db.docs[f"repositories/{ENG_WIDGETS}"]
    assert _audit(db) == []


def test_inherit_clears_it_and_the_workspace_value_decides_again(client, db, seeded):
    db.docs[f"workspace_settings/{CAROL_TENANT}"] = {
        "tenant_id": CAROL_TENANT, "code_intelligence": {"context": "off", "context_ab": "on"}}
    assert _patch(client, CAROL_NOTES, {"context": "on"}, "carol").status_code == 200
    stored = db.docs[f"repositories/{CAROL_NOTES}"]
    decision = graphstage.decide(
        repository_override=stored.get(graphstage.REPOSITORY_CONTEXT_FIELD),
        workspace_settings=db.docs[f"workspace_settings/{CAROL_TENANT}"], task_id="t-1")
    assert (decision.stage, decision.source) == (True, "repository")

    response = _patch(client, CAROL_NOTES, {"context": "inherit"}, "carol")
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["context"] == "inherit"
    stored = db.docs[f"repositories/{CAROL_NOTES}"]
    assert stored["context"] is None
    decision = graphstage.decide(
        repository_override=stored.get(graphstage.REPOSITORY_CONTEXT_FIELD),
        workspace_settings=db.docs[f"workspace_settings/{CAROL_TENANT}"], task_id="t-1")
    assert (decision.stage, decision.source) == (False, "workspace")
    assert _audit(db)[-1]["detail"]["context"] == {"from": "on", "to": None}


def test_a_patch_that_changes_nothing_audits_nothing(client, db, seeded):
    assert _patch(client, CAROL_NOTES, {"context": "inherit"}, "carol").status_code == 200
    assert _patch(client, CAROL_NOTES, {"context": "off"}, "carol").status_code == 200
    assert _patch(client, CAROL_NOTES, {"context": "off"}, "carol").status_code == 200
    assert len(_audit(db)) == 1


def test_a_failed_write_writes_neither_the_override_nor_its_audit(db, seeded, monkeypatch):
    original = db.transaction

    def transaction(**kwargs):
        txn = original(**kwargs)
        put = txn.set

        def set_(ref, data, merge=False):
            if ref.path.startswith("admin_audit/"):
                raise RuntimeError("the audit write failed")
            return put(ref, data, merge=merge)

        txn.set = set_
        return txn

    monkeypatch.setattr(db, "transaction", transaction)
    store = repositories.Repositories(db, now=lambda: NOW)
    with pytest.raises(RuntimeError):
        store.patch(CAROL_TENANT, CAROL_NOTES, repositories.RepositoryPatch(context="off"),
                    context_by=CAROL)
    assert "context" not in db.docs[f"repositories/{CAROL_NOTES}"]
    assert _audit(db) == []


def test_the_store_refuses_it_without_the_checked_owner():
    with pytest.raises(repositories.Forbidden):
        repositories.Repositories(None, now=lambda: NOW).patch(
            CAROL_TENANT, CAROL_NOTES, repositories.RepositoryPatch(context="off"))


@pytest.mark.parametrize("value", ["maybe", "", True, None])
def test_anything_but_the_three_words_is_refused(client, db, seeded, value):
    response = _patch(client, CAROL_NOTES, {"context": value}, "carol")
    assert response.status_code == 422, response.text
    assert "context" not in db.docs[f"repositories/{CAROL_NOTES}"]
    assert _audit(db) == []


def test_the_override_words_match_the_workers_reader():
    assert repositories.CONTEXT_FIELD == graphstage.REPOSITORY_CONTEXT_FIELD
    assert set(repositories.CONTEXT_OVERRIDES) == {
        graphstage.ON, graphstage.OFF, graphstage.INHERIT}
