"""A workspace's merge settings: GET/PUT /v1/workspaces/{id}/merge-settings (TRAIN-WS).

Owner decision 2026-10-14: "the merge train should be a setting users manage
on their own workspace and admins on all workspaces". docs/merge-trains.md §3.

What these tests hold:

  * WHO. The owner of a personal workspace (`u-<user>`, `kind: user`, its
    principal the caller) and a platform admin may change it; for a group
    workspace, a platform admin (the API has no group-admin role yet). A
    member who is neither is refused 403 `merge_settings_owner_required`
    through `refusals.refuse` -- switched, so it ships report-only -- and a
    caller who is not even a member is answered the established 404, so
    another workspace's settings are neither served nor confirmed to exist
    (invariant 9).
  * AUDIT. Every change is one `admin_audit` entry (who, from, to, when) in
    the same transaction as the change; a PUT that changes nothing audits
    nothing.
  * EFFECTIVE MODE. A repository's override (`inherit | direct | train`) on
    its registration beats the workspace mode; `inherit` stores null.
  * Validation: train size 1..10, max train age bounded, unknown fields 422.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api import mergesettings, refusals
from swarm_api.repositories import repo_id_for
from swarm_common.identity import tenant_id_for_user

from .conftest import auth_header, seed_tenant
from .test_repository_hard_stops import commits  # noqa: F401  (fixture)

CAROL = "carol@saga.xyz"
CAROL_TENANT = tenant_id_for_user(CAROL)
SWITCH_ON = {refusals.env_name("merge_settings_owner_required"): "on"}


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    personal = seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    for tenant_id, repo in (("eng", "widgets"), ("eng", "gadgets"), ("research", "widgets"),
                            (CAROL_TENANT, "notes")):
        repo_id = repo_id_for(tenant_id, "saga-xyz", repo)
        db.docs[f"repositories/{repo_id}"] = {
            "repo_id": repo_id, "tenant_id": tenant_id, "owner": "saga-xyz", "repo": repo,
            "default_branch": "main", "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        }
    return personal


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/merge-settings"


def _get(client, tenant_id, user):
    return client.get(_url(tenant_id), headers=auth_header(user))


def _put(client, tenant_id, user, body):
    return client.put(_url(tenant_id), headers=auth_header(user), json=body)


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def _settings_doc(db, tenant_id):
    whole = db.docs.get(f"{mergesettings.COLLECTION}/{tenant_id}")
    return (whole or {}).get(mergesettings.FIELD)


# --------------------------------------------------------------------------
# defaults and the read
# --------------------------------------------------------------------------

def test_a_workspace_with_no_settings_merges_directly_with_the_documented_defaults(client, seeded):
    response = _get(client, "eng", "alice")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "direct"
    assert body["train_size"] == mergesettings.TRAIN_SIZE_DEFAULT == 4
    assert body["max_train_age_minutes"] == mergesettings.MAX_TRAIN_AGE_DEFAULT == 90
    assert body["can_edit"] is False
    repos = {r["repository"]: r for r in body["repositories"]}
    assert set(repos) == {"saga-xyz/widgets", "saga-xyz/gadgets"}
    assert all(r["override"] == "inherit" and r["effective_mode"] == "direct"
               for r in repos.values())


def test_a_caller_outside_the_workspace_is_answered_not_found(client, seeded):
    # bob is in research, not eng: the established 404, never the settings.
    response = _get(client, "eng", "bob")
    assert response.status_code == 404, response.text
    assert "train_size" not in response.text
    assert _get(client, "no-such-workspace", "root").status_code == 404


# --------------------------------------------------------------------------
# who may change it
# --------------------------------------------------------------------------

def test_the_owner_of_a_personal_workspace_changes_it_and_is_audited(client, db, seeded):
    response = _put(client, CAROL_TENANT, "carol", {"mode": "train", "train_size": 6})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "train" and body["train_size"] == 6 and body["can_edit"] is True
    (entry,) = _audit(db)
    assert entry["action"] == mergesettings.AUDIT_ACTION
    assert entry["by"] == CAROL
    assert isinstance(entry["at"], datetime)
    assert entry["detail"]["tenant_id"] == CAROL_TENANT
    assert entry["detail"]["from"] == {"mode": "direct", "train_size": 4}
    assert entry["detail"]["to"] == {"mode": "train", "train_size": 6}


def test_a_platform_admin_changes_any_workspace(client, db, seeded):
    for tenant_id in ("eng", "research", CAROL_TENANT):
        response = _put(client, tenant_id, "root", {"mode": "train"})
        assert response.status_code == 200, response.text
    assert [e["detail"]["tenant_id"] for e in _audit(db)] == ["eng", "research", CAROL_TENANT]
    assert all(e["by"] == "root@saga.xyz" for e in _audit(db))


def test_a_member_who_is_not_the_owner_is_refused_once_the_switch_is_on(
        client, db, seeded, monkeypatch):
    for name, value in SWITCH_ON.items():
        monkeypatch.setenv(name, value)
    response = _put(client, "eng", "alice", {"mode": "train"})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "merge_settings_owner_required"
    assert _settings_doc(db, "eng") is None
    assert _audit(db) == []


def test_the_owner_refusal_is_a_switched_refusal_that_ships_off(client, db, seeded, monkeypatch):
    switch = refusals.SWITCHES["merge_settings_owner_required"]
    assert switch.default is False
    monkeypatch.delenv(switch.env, raising=False)
    # Report-only: the change goes through, and it is audited as anyone's is.
    response = _put(client, "eng", "alice", {"mode": "train"})
    assert response.status_code == 200, response.text
    (entry,) = _audit(db)
    assert entry["by"] == "alice@saga.xyz"


def test_a_caller_outside_the_workspace_cannot_change_it_whatever_the_switch(
        client, db, seeded, monkeypatch):
    monkeypatch.delenv(refusals.env_name("merge_settings_owner_required"), raising=False)
    response = _put(client, "eng", "bob", {"mode": "train"})
    assert response.status_code == 404, response.text
    response = _put(client, CAROL_TENANT, "alice", {"mode": "train"})
    assert response.status_code == 404, response.text
    assert _settings_doc(db, "eng") is None and _settings_doc(db, CAROL_TENANT) is None
    assert _audit(db) == []


# --------------------------------------------------------------------------
# the audit and the write
# --------------------------------------------------------------------------

def test_a_put_that_changes_nothing_audits_nothing(client, db, seeded):
    assert _put(client, "eng", "root", {"mode": "train"}).status_code == 200
    assert _put(client, "eng", "root", {"mode": "train", "train_size": 4}).status_code == 200
    assert len(_audit(db)) == 1


def test_the_change_and_its_audit_are_one_transaction(client, db, seeded, commits):  # noqa: F811
    repo_id = repo_id_for("eng", "saga-xyz", "widgets")
    commits.clear()
    assert _put(client, "eng", "root", {
        "mode": "train", "repositories": {repo_id: "direct"}}).status_code == 200
    (entry,) = _audit(db)
    assert entry["detail"]["from"]["repositories"] == {repo_id: None}
    assert entry["detail"]["to"]["repositories"] == {repo_id: "direct"}
    assert db.docs[f"repositories/{repo_id}"][mergesettings.REPO_FIELD] == "direct"
    writing = [paths for paths in commits if f"repositories/{repo_id}" in paths]
    assert len(writing) == 1
    assert sorted({p.split("/")[0] for p in writing[0]}) == [
        "admin_audit", mergesettings.COLLECTION, "repositories"]


# --------------------------------------------------------------------------
# the per-repository override and the effective mode
# --------------------------------------------------------------------------

def test_a_repository_override_beats_the_workspace_mode(client, db, seeded):
    widgets = repo_id_for("eng", "saga-xyz", "widgets")
    gadgets = repo_id_for("eng", "saga-xyz", "gadgets")
    response = _put(client, "eng", "root", {
        "mode": "direct", "repositories": {widgets: "train"}})
    assert response.status_code == 200, response.text
    repos = {r["repo_id"]: r for r in response.json()["repositories"]}
    assert repos[widgets]["override"] == "train" and repos[widgets]["effective_mode"] == "train"
    assert repos[gadgets]["override"] == "inherit" and repos[gadgets]["effective_mode"] == "direct"

    # The workspace turns train on; the override still wins for its repository.
    response = _put(client, "eng", "root", {
        "mode": "train", "repositories": {widgets: "direct"}})
    repos = {r["repo_id"]: r for r in response.json()["repositories"]}
    assert repos[widgets]["effective_mode"] == "direct"
    assert repos[gadgets]["effective_mode"] == "train"

    # `inherit` clears the override: stored null, the workspace mode again.
    response = _put(client, "eng", "root", {"repositories": {widgets: "inherit"}})
    repos = {r["repo_id"]: r for r in response.json()["repositories"]}
    assert repos[widgets]["override"] == "inherit" and repos[widgets]["effective_mode"] == "train"
    assert db.docs[f"repositories/{widgets}"][mergesettings.REPO_FIELD] is None


@pytest.mark.parametrize("workspace, override, expected", [
    ("direct", None, "direct"),
    ("train", None, "train"),
    ("direct", "train", "train"),
    ("train", "direct", "direct"),
    ("train", "bogus", "train"),       # a malformed stored override is no override
    ("bogus", None, "direct"),          # a malformed stored mode is the default
])
def test_effective_mode_resolution(workspace, override, expected):
    settings = mergesettings.settings_of({"mode": workspace})
    registration = {} if override is None else {mergesettings.REPO_FIELD: override}
    assert mergesettings.effective_mode(settings, registration) == expected


def test_another_workspaces_repository_cannot_be_overridden(client, db, seeded):
    theirs = repo_id_for("research", "saga-xyz", "widgets")
    response = _put(client, "eng", "root", {"repositories": {theirs: "train"}})
    assert response.status_code == 404, response.text
    assert mergesettings.REPO_FIELD not in db.docs[f"repositories/{theirs}"]
    assert _audit(db) == []


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {},
    {"mode": "queue"},
    {"train_size": 0},
    {"train_size": 11},
    {"train_size": "4"},
    {"max_train_age_minutes": 1},
    {"max_train_age_minutes": 100000},
    {"repositories": {"repo_0000000000000000": "sometimes"}},
    {"image": "evil:latest"},
])
def test_an_invalid_body_is_refused_and_nothing_is_written(client, db, seeded, body):
    response = _put(client, "eng", "root", body)
    assert response.status_code == 422, response.text
    assert _settings_doc(db, "eng") is None
    assert _audit(db) == []
