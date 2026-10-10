"""A repository's `merge_policy`: a platform admin's setting, audited (WF-MERGE-API).

`merge_policy` ("off" | "on_merge_verdict") is the default a workflow on the
repository takes when its `metadata.merge` says nothing, and an issue run's
default `auto_merge` (tests/unit/control_plane/test_merge_verdict_workflows.py
and test_issue_runs.py hold what it does). Here, who may set it:

  * a member's create or PATCH carrying it answers 403, before the forge is
    read, with nothing written;
  * an admin's set is one `admin_audit` entry, in the transaction that
    changes the registration, and a PATCH that changes nothing audits nothing;
  * a value other than the two is a 422;
  * it round-trips through create, PATCH and read, and a registration stored
    before it reads as `merge_policy: null` -- the platform default.

No credentials, no network: the secret reader is a fake and GitHub is a fake
transport under the real client.
"""

from __future__ import annotations

import pytest

from swarm_api import repositories

from .conftest import auth_header
from .repo_fakes import GitHubRepos, TenantTokens, make_client, repo_entry
from .test_repository_hard_stops import commits  # noqa: F401  (fixture)

WIDGETS = repo_entry("saga-xyz/widgets", default_branch="develop", private=True)
ADMIN = "root"
MEMBER = "alice"


@pytest.fixture
def make(db, tokens, group_map, objects):
    def build():
        transport = GitHubRepos(repos={"saga-xyz/widgets": WIDGETS})
        client = make_client(
            db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=transport
        )
        return client, transport
    return build


def _register(client, user=MEMBER, **body):
    payload = {"repository": "saga-xyz/widgets", **body}
    return client.post("/v1/repositories", json=payload, headers=auth_header(user))


def _patch(client, repo_id, body, user=MEMBER):
    return client.patch(f"/v1/repositories/{repo_id}", json=body, headers=auth_header(user))


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def _repo_paths(db) -> list[str]:
    return [path for path in db.docs if path.startswith("repositories/")]


def test_a_registration_starts_with_no_policy(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    assert record["merge_policy"] is None
    assert _audit(db) == []


@pytest.mark.parametrize("value", ["off", "on_merge_verdict"])
def test_a_member_cannot_register_with_a_merge_policy(make, db, value):
    client, transport = make()
    response = _register(client, merge_policy=value)
    assert response.status_code == 403, response.text
    assert transport.calls == []
    assert _repo_paths(db) == []
    assert _audit(db) == []


@pytest.mark.parametrize("value", ["off", "on_merge_verdict"])
def test_a_member_cannot_patch_the_merge_policy(make, db, value):
    client, _ = make()
    record = _register(client).json()["repository"]
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    response = _patch(client, record["repo_id"], {"merge_policy": value, "paused": True})
    assert response.status_code == 403, response.text
    assert db.docs[f"repositories/{record['repo_id']}"] == before
    assert _audit(db) == []


def test_an_admin_sets_the_policy_and_the_audit_is_in_the_same_transaction(make, db, commits):  # noqa: F811
    client, _ = make()
    record = _register(client).json()["repository"]
    path = f"repositories/{record['repo_id']}"
    commits.clear()

    response = _patch(client, record["repo_id"], {"merge_policy": "on_merge_verdict"}, user=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["merge_policy"] == "on_merge_verdict"
    assert db.docs[path]["merge_policy"] == "on_merge_verdict"
    (entry,) = _audit(db)
    assert entry["action"] == "repository_merge_policy_set"
    assert entry["by"] == "root@saga.xyz"
    assert entry["detail"]["merge_policy"] == {"from": None, "to": "on_merge_verdict"}
    writing = [paths for paths in commits if path in paths]
    assert len(writing) == 1
    assert sorted(p.split("/")[0] for p in writing[0]) == ["admin_audit", "repositories"]

    # The same value again changes nothing and audits nothing.
    assert _patch(client, record["repo_id"], {"merge_policy": "on_merge_verdict"},
                  user=ADMIN).status_code == 200
    assert len(_audit(db)) == 1
    # Read back.
    got = client.get(f"/v1/repositories/{record['repo_id']}", headers=auth_header(MEMBER))
    assert got.json()["repository"]["merge_policy"] == "on_merge_verdict"


def test_an_admin_registers_with_a_policy_audited_in_the_create(make, db):
    client, _ = make()
    response = _register(client, user=ADMIN, merge_policy="off")
    assert response.status_code == 201, response.text
    assert response.json()["repository"]["merge_policy"] == "off"
    assert [e["action"] for e in _audit(db)] == ["repository_merge_policy_set"]


@pytest.mark.parametrize("value", ["on", "ON", True, "merge"])
def test_a_policy_that_is_not_off_or_on_merge_verdict_is_refused(make, db, value):
    client, _ = make()
    record = _register(client).json()["repository"]
    response = _patch(client, record["repo_id"], {"merge_policy": value}, user=ADMIN)
    assert response.status_code == 422, response.text
    assert _audit(db) == []


def test_a_policy_is_never_stored_without_an_audit_entry(db):
    store = repositories.Repositories(db, now=lambda: None)
    record = {"repo_id": "repo_" + "0" * 16, "tenant_id": "eng", "owner": "o", "repo": "r",
              "merge_policy": "on_merge_verdict"}
    with pytest.raises(ValueError):
        store.create(record)
    assert _repo_paths(db) == [] and _audit(db) == []


@pytest.mark.parametrize(("stored", "read"), [
    (None, None), ("off", "off"), ("on_merge_verdict", "on_merge_verdict"),
    ("on", None), (True, None),
])
def test_a_stored_value_reads_as_a_policy_only_when_it_is_one(stored, read):
    assert repositories.merge_policy_of({"merge_policy": stored}) == read
    assert repositories.merge_policy_of({}) is None
