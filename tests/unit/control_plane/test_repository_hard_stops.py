"""The registration fields the hard stops read (docs/schedules.md §4.4, lane S11).

`platform` marks a repository as the platform's own. Only a platform admin may
set or clear it, because clearing it would loosen the owner's holds, and each
change is an `admin_audit` entry written in the SAME transaction as the change.
`hard_stop_paths` is the list of protected paths. A member may add patterns but
cannot remove the four defaults, which are its floor.

What is held here:

  * a member's create or PATCH carrying `platform` answers 403, before the
    forge is read and with nothing written: no registration change and no
    audit entry;
  * an admin's set and clear are each one `admin_audit` entry, committed in
    the one transaction that changes the registration;
  * a `hard_stop_paths` that drops one of the four defaults answers 422
    naming it, on create and on PATCH, and nothing is written;
  * both fields round-trip through create, PATCH and read, and a registration
    stored before them reads as `platform: false` with the defaults;
  * the four defaults are exactly the list §4.4 names.

No credentials, no network: the secret reader is a fake and GitHub is a fake
transport under the real client.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_api import repositories
from swarm_api.repositories import HARD_STOP_PATHS_DEFAULT, hard_stop_paths_of

from .conftest import auth_header
from .repo_fakes import GitHubRepos, TenantTokens, make_client, repo_entry

WIDGETS = repo_entry("saga-xyz/widgets", default_branch="develop", private=True)
DEFAULTS = [".github/workflows/**", "terraform/bootstrap/**", "**/iam*.tf", "CODEOWNERS"]
SCHEDULES_DOC = Path(__file__).resolve().parents[3] / "docs" / "schedules.md"

# root is a platform admin and a member of eng (conftest.group_map); alice is
# a member of eng and nothing more.
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


@pytest.fixture
def commits(db, monkeypatch):
    """The document paths each committed transaction wrote, in commit order."""
    seen: list[list[str]] = []
    original = db.transaction

    def transaction(**kwargs):
        txn = original(**kwargs)
        commit = txn._commit

        def _commit():
            seen.append([ref.path for _op, ref, _data in txn._buffer])
            return commit()

        txn._commit = _commit
        return txn

    monkeypatch.setattr(db, "transaction", transaction)
    return seen


def _register(client, user=MEMBER, **body):
    payload = {"repository": "saga-xyz/widgets", **body}
    return client.post("/v1/repositories", json=payload, headers=auth_header(user))


def _patch(client, repo_id, body, user=MEMBER):
    return client.patch(f"/v1/repositories/{repo_id}", json=body, headers=auth_header(user))


def _audit(db) -> list[dict]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def _repo_paths(db) -> list[str]:
    return [path for path in db.docs if path.startswith("repositories/")]


# --------------------------------------------------------------------------
# the defaults are §4.4's
# --------------------------------------------------------------------------

def test_the_four_defaults_are_the_list_the_design_names():
    text = SCHEDULES_DOC.read_text(encoding="utf-8")
    found = re.search(r"`hard_stop_paths`, a field lane S11 adds, default `\[(.*?)\]`", text)
    assert found is not None, "§4.4 no longer names the hard_stop_paths default"
    named = re.findall(r'"([^"]+)"', found.group(1))
    assert named == DEFAULTS
    assert list(HARD_STOP_PATHS_DEFAULT) == DEFAULTS
    assert len(HARD_STOP_PATHS_DEFAULT) == 4


# --------------------------------------------------------------------------
# platform: a platform admin's, audited in the same transaction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [True, False])
def test_a_member_cannot_register_with_platform(make, db, value):
    client, transport = make()
    response = _register(client, platform=value)
    assert response.status_code == 403, response.text
    # Refused before the forge is read, and nothing written.
    assert transport.calls == []
    assert _repo_paths(db) == []
    assert _audit(db) == []


@pytest.mark.parametrize("value", [True, False])
def test_a_member_cannot_patch_platform(make, db, value):
    client, _ = make()
    record = _register(client).json()["repository"]
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    response = _patch(client, record["repo_id"], {"platform": value})
    assert response.status_code == 403, response.text
    assert db.docs[f"repositories/{record['repo_id']}"] == before
    assert _audit(db) == []
    # Carried beside a change a member may make, it is still refused whole.
    response = _patch(client, record["repo_id"], {"platform": value, "paused": True})
    assert response.status_code == 403, response.text
    assert db.docs[f"repositories/{record['repo_id']}"] == before
    assert _audit(db) == []


def test_another_tenants_member_cannot_patch_platform_either(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    response = _patch(client, record["repo_id"], {"platform": True}, user="bob")
    assert response.status_code in (403, 404), response.text
    assert db.docs[f"repositories/{record['repo_id']}"] == before
    assert _audit(db) == []


def test_an_admin_sets_platform_and_the_audit_is_in_the_same_transaction(make, db, commits):
    client, _ = make()
    record = _register(client).json()["repository"]
    assert record["platform"] is False
    path = f"repositories/{record['repo_id']}"
    commits.clear()

    response = _patch(client, record["repo_id"], {"platform": True}, user=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["platform"] is True
    assert db.docs[path]["platform"] is True

    entries = _audit(db)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "repository_platform_set"
    assert entry["target_repo_id"] == record["repo_id"]
    assert entry["by"] == "root@saga.xyz"
    assert entry["detail"] == {
        "tenant_id": "eng",
        "repository": "saga-xyz/widgets",
        "platform": {"from": False, "to": True},
    }
    # ONE commit wrote both the registration and the audit entry.
    writing = [paths for paths in commits if path in paths]
    assert len(writing) == 1
    assert sorted(p.split("/")[0] for p in writing[0]) == ["admin_audit", "repositories"]


def test_an_admin_clears_platform_with_its_own_audit_entry(make, db, commits):
    client, _ = make()
    record = _register(client).json()["repository"]
    path = f"repositories/{record['repo_id']}"
    assert _patch(client, record["repo_id"], {"platform": True}, user=ADMIN).status_code == 200
    commits.clear()

    response = _patch(client, record["repo_id"], {"platform": False}, user=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["platform"] is False
    assert db.docs[path]["platform"] is False
    actions = sorted(entry["action"] for entry in _audit(db))
    assert actions == ["repository_platform_cleared", "repository_platform_set"]
    cleared = [e for e in _audit(db) if e["action"] == "repository_platform_cleared"][0]
    assert cleared["detail"]["platform"] == {"from": True, "to": False}
    writing = [paths for paths in commits if path in paths]
    assert len(writing) == 1
    assert sorted(p.split("/")[0] for p in writing[0]) == ["admin_audit", "repositories"]


def test_an_admin_patch_that_does_not_change_platform_writes_no_audit(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    response = _patch(client, record["repo_id"], {"platform": False}, user=ADMIN)
    assert response.status_code == 200, response.text
    assert _audit(db) == []


def test_an_admin_registers_a_platform_repository_audited_in_the_create(make, db, commits):
    client, _ = make()
    response = _register(client, user=ADMIN, platform=True)
    assert response.status_code == 201, response.text
    record = response.json()["repository"]
    assert record["platform"] is True
    path = f"repositories/{record['repo_id']}"
    assert db.docs[path]["platform"] is True
    entries = _audit(db)
    assert [e["action"] for e in entries] == ["repository_platform_set"]
    assert entries[0]["target_repo_id"] == record["repo_id"]
    writing = [paths for paths in commits if path in paths]
    assert len(writing) == 1
    assert sorted(p.split("/")[0] for p in writing[0]) == ["admin_audit", "repositories"]


def test_platform_is_never_stored_true_without_an_audit_entry(db):
    store = repositories.Repositories(db, now=lambda: None)
    record = {"repo_id": "repo_" + "0" * 16, "tenant_id": "eng", "owner": "o", "repo": "r",
              "platform": True}
    with pytest.raises(ValueError):
        store.create(record)
    assert _repo_paths(db) == [] and _audit(db) == []


def test_platform_must_be_a_boolean(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    response = _patch(client, record["repo_id"], {"platform": "yes"}, user=ADMIN)
    assert response.status_code == 422, response.text
    assert _audit(db) == []


# --------------------------------------------------------------------------
# hard_stop_paths: the four defaults are a floor
# --------------------------------------------------------------------------

def test_a_registration_starts_with_the_four_defaults(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    assert record["hard_stop_paths"] == DEFAULTS
    assert record["hard_stop_paths_floor"] == DEFAULTS
    assert db.docs[f"repositories/{record['repo_id']}"]["hard_stop_paths"] == DEFAULTS


def test_a_member_may_add_patterns(make, db):
    client, _ = make()
    wanted = DEFAULTS + ["deploy/**", "SECURITY.md"]
    response = _register(client, hard_stop_paths=wanted)
    assert response.status_code == 201, response.text
    record = response.json()["repository"]
    assert record["hard_stop_paths"] == wanted

    more = wanted + ["infra/prod/**"]
    response = _patch(client, record["repo_id"], {"hard_stop_paths": more})
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["hard_stop_paths"] == more
    assert db.docs[f"repositories/{record['repo_id']}"]["hard_stop_paths"] == more
    # And remove its own again, down to the floor.
    response = _patch(client, record["repo_id"], {"hard_stop_paths": DEFAULTS})
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["hard_stop_paths"] == DEFAULTS
    # Changing the paths is a member's change: no admin audit.
    assert _audit(db) == []


@pytest.mark.parametrize("dropped", DEFAULTS)
def test_registering_without_a_default_answers_422_naming_it(make, db, dropped):
    client, transport = make()
    paths = [p for p in DEFAULTS if p != dropped] + ["deploy/**"]
    response = _register(client, hard_stop_paths=paths)
    assert response.status_code == 422, response.text
    assert dropped in response.text
    assert transport.calls == []
    assert _repo_paths(db) == []


@pytest.mark.parametrize("dropped", DEFAULTS)
def test_a_patch_that_drops_a_default_answers_422_naming_it(make, db, dropped):
    client, _ = make()
    record = _register(client).json()["repository"]
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    paths = [p for p in DEFAULTS if p != dropped]
    for user in (MEMBER, ADMIN):
        # Not even an admin: the floor is §4.4's, not a permission.
        response = _patch(client, record["repo_id"], {"hard_stop_paths": paths}, user=user)
        assert response.status_code == 422, response.text
        assert dropped in response.text
        assert db.docs[f"repositories/{record['repo_id']}"] == before


def test_an_empty_list_drops_all_four(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    response = _patch(client, record["repo_id"], {"hard_stop_paths": []})
    assert response.status_code == 422, response.text
    for path in DEFAULTS:
        assert path in response.text


@pytest.mark.parametrize("bad", [
    "",
    " deploy/**",
    "deploy/** ",
    "/etc/**",
    "deploy/\x00**",
    "deploy/\n**",
    "x" * 257,
])
def test_a_pattern_that_is_not_a_repository_path_is_refused(make, db, bad):
    client, _ = make()
    record = _register(client).json()["repository"]
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    response = _patch(client, record["repo_id"], {"hard_stop_paths": DEFAULTS + [bad]})
    assert response.status_code == 422, (bad, response.text)
    assert db.docs[f"repositories/{record['repo_id']}"] == before


def test_the_list_is_bounded(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    many = DEFAULTS + [f"dir{i}/**" for i in range(repositories.HARD_STOP_PATHS_MAX)]
    response = _patch(client, record["repo_id"], {"hard_stop_paths": many})
    assert response.status_code == 422, response.text


def test_a_repeated_pattern_is_stored_once(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    response = _patch(client, record["repo_id"], {"hard_stop_paths": DEFAULTS + ["a/**", "a/**"]})
    assert response.status_code == 200, response.text
    assert response.json()["repository"]["hard_stop_paths"] == DEFAULTS + ["a/**"]


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

def test_read_and_list_return_both_fields(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    assert _patch(client, record["repo_id"], {"platform": True}, user=ADMIN).status_code == 200
    extra = DEFAULTS + ["deploy/**"]
    assert _patch(client, record["repo_id"], {"hard_stop_paths": extra}).status_code == 200

    one = client.get(f"/v1/repositories/{record['repo_id']}", headers=auth_header(MEMBER))
    assert one.status_code == 200
    got = one.json()["repository"]
    assert got["platform"] is True
    assert got["hard_stop_paths"] == extra
    assert got["hard_stop_paths_floor"] == DEFAULTS

    listed = client.get("/v1/repositories", headers=auth_header(MEMBER)).json()["repositories"]
    assert [(r["platform"], r["hard_stop_paths"]) for r in listed] == [(True, extra)]


def test_a_registration_stored_before_the_fields_reads_as_not_platform_with_the_defaults(make, db):
    client, _ = make()
    record = _register(client).json()["repository"]
    stored = db.docs[f"repositories/{record['repo_id']}"]
    stored.pop("platform")
    stored.pop("hard_stop_paths")
    got = client.get(f"/v1/repositories/{record['repo_id']}", headers=auth_header(MEMBER))
    assert got.status_code == 200
    body = got.json()["repository"]
    assert body["platform"] is False
    assert body["hard_stop_paths"] == DEFAULTS
    # And a PATCH of something else leaves it reading the same.
    assert _patch(client, record["repo_id"], {"paused": True}).status_code == 200
    body = client.get(f"/v1/repositories/{record['repo_id']}", headers=auth_header(MEMBER)).json()
    assert body["repository"]["platform"] is False
    assert body["repository"]["hard_stop_paths"] == DEFAULTS


def test_what_the_hard_stops_read_always_carries_the_floor():
    # A stored list is never trusted to hold the floor: what S5 reads re-applies it.
    assert hard_stop_paths_of({}) == DEFAULTS
    assert hard_stop_paths_of({"hard_stop_paths": None}) == DEFAULTS
    assert hard_stop_paths_of({"hard_stop_paths": ["deploy/**", "CODEOWNERS"]}) == [
        "deploy/**", "CODEOWNERS", ".github/workflows/**", "terraform/bootstrap/**", "**/iam*.tf",
    ]
    assert hard_stop_paths_of({"hard_stop_paths": [*DEFAULTS, 7]}) == DEFAULTS
    assert repositories.platform_of({}) is False
    assert repositories.platform_of({"platform": "true"}) is False
    assert repositories.platform_of({"platform": True}) is True
