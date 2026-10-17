"""A workspace's code-intelligence setting: GET/PUT /v1/workspaces/{id}/code-intelligence (CTXQ-SET).

docs/design/context-quality.md §4.7 and §5.4. Owner decisions 2026-10-14:

  * the setting is `workspace_settings/{tenant_id}.code_intelligence`, one
    field of the ONE settings document a workspace has, beside JUDGE-SELECT's
    `judgment_backend` and TRAIN-WS's `merge`; writing it never touches them;
  * OFF BY DEFAULT: with nothing stored, Jev sees no code (`jev: false`) and
    local context is whatever the platform default is (`inherit`);
  * only the workspace's owner (a personal workspace's principal) or a
    platform admin changes it, as for `judgment_backend`: a member is 403
    `code_intelligence_owner_required` (the switch SHIPS ON, owner decision
    2026-10-14), a caller outside the workspace 404, and nothing is written
    for either;
  * every change is one `admin_audit` row written in the SAME transaction: a
    failed write writes neither the setting nor the row.

And the reads CTXQ-S's gate (`agent_worker.graphstage.decide`) and CTXQ-J's
(`agent_worker.judge.settings.JudgeSettings`) make are of the document this
API writes, so the field names cannot drift apart unnoticed.

MUTATION: drop the owner/admin check in `routes/codeintel.py` and
`test_a_member_cannot_change_it_once_the_switch_is_on` fails; write the audit outside the
transaction and `test_a_failed_write_writes_neither_the_setting_nor_its_audit`
fails; replace the document instead of the field and
`test_writing_it_leaves_judgment_backend_and_merge_unchanged` fails.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_worker import graphstage
from agent_worker.judge.settings import JudgeSettings
from swarm_api import codeintel, judgment, mergesettings, refusals
from swarm_api.repositories import repo_id_for
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant
from .test_repository_hard_stops import commits  # noqa: F401  (fixture)

CAROL = "carol@saga.xyz"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    for tenant_id, repo in (("eng", "widgets"), ("eng", "gadgets"), ("research", "widgets")):
        repo_id = repo_id_for(tenant_id, "saga-xyz", repo)
        db.docs[f"repositories/{repo_id}"] = {
            "repo_id": repo_id, "tenant_id": tenant_id, "owner": "saga-xyz", "repo": repo,
            "default_branch": "main", "created_at": NOW,
        }
    return db


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/code-intelligence"


def _get(client, tenant_id, user):
    return client.get(_url(tenant_id), headers=auth_header(user))


def _put(client, tenant_id, user, body):
    return client.put(_url(tenant_id), headers=auth_header(user), json=body)


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def _document(db, tenant_id) -> dict | None:
    return db.docs.get(f"workspace_settings/{tenant_id}")


def _block(db, tenant_id) -> dict | None:
    return (_document(db, tenant_id) or {}).get(codeintel.FIELD)


# --------------------------------------------------------------------------
# where it lives, and off by default
# --------------------------------------------------------------------------

def test_the_setting_is_a_field_of_the_one_workspace_settings_document():
    assert codeintel.COLLECTION == "workspace_settings"
    assert codeintel.COLLECTION == judgment.SETTINGS_COLLECTION == mergesettings.COLLECTION
    assert codeintel.FIELD == "code_intelligence"
    assert codeintel.FIELD not in (judgment.SETTING_FIELD, mergesettings.FIELD)


def test_it_is_off_by_default(client, db, seeded):
    response = _get(client, "eng", "alice")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng"
    assert body["jev"] is False
    assert body["context"] == "inherit" and body["context_ab"] == "inherit"
    assert body["defaults"]["jev"] is False
    assert body["can_edit"] is False
    assert body["updated_at"] is None and body["updated_by"] is None
    repos = {r["repository"]: r for r in body["repositories"]}
    assert set(repos) == {"saga-xyz/widgets", "saga-xyz/gadgets"}
    assert all(r["context"] == "inherit" for r in repos.values())
    # Reading writes nothing.
    assert _document(db, "eng") is None and _audit(db) == []
    # The pure read of an absent or malformed block is off, too.
    assert codeintel.settings_of(None).jev is False
    assert codeintel.settings_of({"jev": "yes", "context": "maybe"}) == codeintel.settings_of(None)


def test_a_workspace_that_set_nothing_sends_jev_no_code_by_the_workers_own_read(seeded):
    # CTXQ-J's reader, over what this API leaves when nothing was set.
    assert JudgeSettings.from_mapping(_document(seeded, "eng")).code_opt_in is False


# --------------------------------------------------------------------------
# who may change it
# --------------------------------------------------------------------------

def test_the_owner_of_a_personal_workspace_changes_it_and_is_audited(client, db, seeded):
    response = _put(client, CAROL_TENANT, "carol", {"jev": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jev"] is True and body["can_edit"] is True
    assert body["updated_by"] == CAROL
    (entry,) = _audit(db)
    assert entry["action"] == codeintel.AUDIT_ACTION
    assert entry["target_tenant_id"] == CAROL_TENANT
    assert entry["by"] == CAROL
    assert isinstance(entry["at"], datetime)
    assert entry["detail"] == {"tenant_id": CAROL_TENANT,
                               "from": {"jev": False}, "to": {"jev": True}}


def test_a_platform_admin_changes_any_workspace(client, db, seeded):
    for tenant_id in ("eng", "research", CAROL_TENANT):
        response = _put(client, tenant_id, "root", {"jev": True})
        assert response.status_code == 200, response.text
    assert [e["detail"]["tenant_id"] for e in _audit(db)] == ["eng", "research", CAROL_TENANT]
    assert all(e["by"] == "root@saga.xyz" for e in _audit(db))
    assert all(_block(db, t)["jev"] is True for t in ("eng", "research", CAROL_TENANT))


def test_a_member_cannot_change_it_and_nothing_is_written(client, db, seeded, monkeypatch):
    # alice is a member of eng, neither its owner (a group has none) nor an admin.
    # No env is set: the refusal is enforced by default.
    monkeypatch.delenv(refusals.env_name(codeintel.OwnerRequired.code), raising=False)
    response = _put(client, "eng", "alice", {"jev": True})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == codeintel.OwnerRequired.code
    assert _document(db, "eng") is None
    assert _audit(db) == []
    # CONTROL: the platform admin and a personal workspace's owner still succeed, one audit row each.
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert _put(client, CAROL_TENANT, "carol", {"jev": True}).status_code == 200
    assert len(_audit(db)) == 2


def test_the_owner_refusal_ships_on_by_owner_decision(client, db, seeded, monkeypatch):
    # Was test_the_owner_refusal_is_a_switched_refusal_that_ships_off (report-only);
    # changed to the enforced behaviour by owner decision 2026-10-14.
    switch = refusals.SWITCHES[codeintel.OwnerRequired.code]
    assert switch.default is True
    monkeypatch.delenv(switch.env, raising=False)
    assert _put(client, "eng", "alice", {"jev": True}).status_code == 403
    # It can still be switched off explicitly, and then the change is audited under the caller.
    monkeypatch.setenv(switch.env, "off")
    assert _put(client, "eng", "alice", {"jev": True}).status_code == 200
    (entry,) = _audit(db)
    assert entry["by"] == "alice@saga.xyz"


def test_another_workspace_is_not_found_and_nothing_is_written(client, db, seeded):
    # bob is in research, not eng; alice is not carol.
    for tenant_id, user in (("eng", "bob"), (CAROL_TENANT, "alice")):
        assert _get(client, tenant_id, user).status_code == 404
        response = _put(client, tenant_id, user, {"jev": True})
        assert response.status_code == 404, response.text
        assert "jev" not in response.text
    assert _put(client, "no-such-workspace", "root", {"jev": True}).status_code == 404
    assert _document(db, "eng") is None and _document(db, CAROL_TENANT) is None
    assert _audit(db) == []


# --------------------------------------------------------------------------
# the write: one field, one transaction
# --------------------------------------------------------------------------

def test_writing_it_leaves_judgment_backend_and_merge_unchanged(client, db, seeded):
    # The two settings already in the document, as their own modules store
    # them. Seeded rather than PUT: the in-memory transaction applies a
    # `set(..., merge=True)` as a replace (fakes.FakeTransaction.set), so two
    # PUTs through those APIs would not leave both fields in the fake.
    db.docs["workspace_settings/eng"] = {
        "tenant_id": "eng",
        judgment.SETTING_FIELD: judgment.INTERNAL_LAYA,
        "judgment_backend_set_by": "root@saga.xyz",
        mergesettings.FIELD: {"tenant_id": "eng", "mode": "train", "train_size": 6,
                              "max_train_age_minutes": 90},
    }
    before = dict(_document(db, "eng"))
    # CONTROL: both APIs read them, so "unchanged" below could have failed.
    assert client.get("/v1/workspaces/eng/judgment-backend", headers=auth_header("root")
                      ).json()["judgment_backend"] == judgment.INTERNAL_LAYA
    assert client.get("/v1/workspaces/eng/merge-settings", headers=auth_header("root")
                      ).json()["train_size"] == 6
    assert codeintel.FIELD not in before

    response = _put(client, "eng", "root", {"jev": True, "context": "off"})
    assert response.status_code == 200, response.text

    after = _document(db, "eng")
    assert after[codeintel.FIELD]["jev"] is True
    assert after[codeintel.FIELD]["context"] == "off"
    assert {k: v for k, v in after.items() if k != codeintel.FIELD} == before
    # And the other two APIs still read what they stored.
    assert client.get("/v1/workspaces/eng/judgment-backend", headers=auth_header("root")
                      ).json()["judgment_backend"] == judgment.INTERNAL_LAYA
    assert client.get("/v1/workspaces/eng/merge-settings", headers=auth_header("root")
                      ).json()["train_size"] == 6


def test_unknown_keys_of_the_block_survive_a_write(client, db, seeded):
    # A key another lane keeps in the block (CTXQ-J reads a monthly cap there)
    # is not this API's to drop.
    db.docs["workspace_settings/eng"] = {
        "tenant_id": "eng", codeintel.FIELD: {"jev_monthly_cap_tokens": 1000}}
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert _block(db, "eng")["jev_monthly_cap_tokens"] == 1000
    assert _block(db, "eng")["jev"] is True


def test_the_change_and_its_audit_commit_in_one_transaction(client, db, seeded, commits):  # noqa: F811
    commits.clear()
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    writing = [paths for paths in commits if "workspace_settings/eng" in paths]
    assert len(writing) == 1
    assert sorted({p.split("/")[0] for p in writing[0]}) == ["admin_audit", "workspace_settings"]


def test_a_failed_write_writes_neither_the_setting_nor_its_audit(db, seeded, monkeypatch):
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
    store = codeintel.CodeIntelligenceStore(db, now=lambda: NOW, mac=judgment.SettingsMac())
    with pytest.raises(RuntimeError):
        store.put("eng", codeintel.CodeIntelligencePut(jev=True), by="root@saga.xyz")
    assert _document(db, "eng") is None
    assert _audit(db) == []


def test_a_put_that_changes_nothing_audits_nothing(client, db, seeded):
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert _put(client, "eng", "root", {"context": "inherit"}).status_code == 200
    assert len(_audit(db)) == 1


def test_inherit_clears_a_workspace_value(client, db, seeded):
    assert _put(client, "eng", "root", {"context": "off", "context_ab": "on"}).status_code == 200
    assert _block(db, "eng")["context"] == "off"
    response = _put(client, "eng", "root", {"context": "inherit", "context_ab": "inherit"})
    assert response.status_code == 200, response.text
    assert response.json()["context"] == "inherit" and response.json()["context_ab"] == "inherit"
    assert "context" not in _block(db, "eng") and "context_ab" not in _block(db, "eng")
    entry = _audit(db)[-1]
    assert entry["detail"]["from"] == {"context": "off", "context_ab": "on"}
    assert entry["detail"]["to"] == {"context": None, "context_ab": None}


# --------------------------------------------------------------------------
# the worker's reads of what this API writes
# --------------------------------------------------------------------------

def test_graphstage_reads_the_workspace_context_this_api_writes(client, db, seeded):
    assert _put(client, "eng", "root", {"context": "off"}).status_code == 200
    decision = graphstage.decide(repository_override=None,
                                 workspace_settings=_document(db, "eng"), task_id="t-1")
    assert (decision.stage, decision.source) == (False, "workspace")

    assert _put(client, "eng", "root", {"context": "on", "context_ab": "on"}).status_code == 200
    decision = graphstage.decide(repository_override=None,
                                 workspace_settings=_document(db, "eng"), task_id="t-1")
    assert (decision.stage, decision.source, decision.ab) == (True, "workspace", "on")


def test_the_judge_reads_the_jev_opt_in_this_api_writes(client, db, seeded):
    assert _put(client, "eng", "root", {"jev": True}).status_code == 200
    assert JudgeSettings.from_mapping(_document(db, "eng")).code_opt_in is True
    assert _put(client, "eng", "root", {"jev": False}).status_code == 200
    assert JudgeSettings.from_mapping(_document(db, "eng")).code_opt_in is False


def test_the_names_and_defaults_match_the_workers_reader():
    assert codeintel.FIELD == graphstage.CODE_INTELLIGENCE_FIELD
    assert codeintel.CONTEXT_FIELD == graphstage.CONTEXT_FIELD
    assert codeintel.AB_FIELD == graphstage.AB_FIELD
    assert codeintel.DEFAULT_CONTEXT == graphstage.DEFAULT_CONTEXT
    assert codeintel.DEFAULT_AB == graphstage.DEFAULT_AB


def test_the_view_says_what_each_repository_effectively_gets(client, db, seeded):
    widgets = repo_id_for("eng", "saga-xyz", "widgets")
    db.docs[f"repositories/{widgets}"]["context"] = "off"
    body = _put(client, "eng", "root", {"context": "on"}).json()
    repos = {r["repo_id"]: r for r in body["repositories"]}
    assert repos[widgets]["context"] == "off" and repos[widgets]["effective_context"] == "off"
    gadgets = repo_id_for("eng", "saga-xyz", "gadgets")
    assert repos[gadgets]["context"] == "inherit" and repos[gadgets]["effective_context"] == "on"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {},
    {"jev": "yes"},
    {"jev": 1},
    {"context": "sometimes"},
    {"context_ab": "maybe"},
    {"judgment_backend": "typesafe"},
    {"image": "evil:latest"},
])
def test_an_invalid_body_is_refused_and_nothing_is_written(client, db, seeded, body):
    response = _put(client, "eng", "root", body)
    assert response.status_code == 422, response.text
    assert _document(db, "eng") is None
    assert _audit(db) == []
