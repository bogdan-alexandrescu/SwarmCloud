"""`/v1/repositories`: a registered repository, per tenant (repo-index.md §1, §6, lane RI1).

What is held here:

  * a registration is the tenant's own: another tenant's `repo_id` reads,
    patches and deletes as a 404 indistinguishable from a missing one;
  * `repo_id` is deterministic, so registering twice is idempotent;
  * the repository is validated before the forge is read, by the issue
    reference's owner and repository patterns and `check_repository_url`;
  * the registration reads `GET /repos/{owner}/{repo}` once, with the
    caller's TENANT's token, for the default branch and the visibility, and
    refuses a repository the token cannot read with `no_access`, naming the
    secret and no part of its value;
  * nothing a caller sends can name an image, a command or a resource spec
    (invariant 10), and nothing stored or served holds a credential.

No credentials, no network: the secret reader is a fake and GitHub is a fake
transport under the real client.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from swarm_api import repositories

from .conftest import auth_header
from .repo_fakes import GitHubRepos, TenantTokens, make_client, repo_entry

WIDGETS = repo_entry("saga-xyz/widgets", default_branch="develop", private=True)


@pytest.fixture
def make(db, tokens, group_map, objects):
    def build(transport=None, secrets_reader=None):
        secrets_reader = secrets_reader or TenantTokens()
        transport = transport or GitHubRepos(repos={"saga-xyz/widgets": WIDGETS})
        client = make_client(
            db, tokens, group_map, objects, forge_tokens=secrets_reader, transport=transport
        )
        return client, secrets_reader, transport
    return build


def _register(client, user="alice", **body):
    payload = {"repository": "saga-xyz/widgets", **body}
    return client.post("/v1/repositories", json=payload, headers=auth_header(user))


def _expected_id(tenant: str, repository: str) -> str:
    digest = hashlib.sha256(f"{tenant}github.com/{repository.lower()}".encode()).hexdigest()
    return "repo_" + digest[:16]


# --------------------------------------------------------------------------
# register
# --------------------------------------------------------------------------

def test_registration_reads_the_forge_once_and_stores_the_record(make, db):
    client, secrets_reader, transport = make()
    response = _register(client)
    assert response.status_code == 201, response.text
    body = response.json()
    record = body["repository"]
    assert body["created"] is True
    assert record["repo_id"] == _expected_id("eng", "saga-xyz/widgets")
    assert record["tenant_id"] == "eng"
    assert record["forge"] == "github"
    assert (record["owner"], record["repo"]) == ("saga-xyz", "widgets")
    assert record["repository"] == "saga-xyz/widgets"
    assert record["repository_url"] == "https://github.com/saga-xyz/widgets"
    # From the forge, not from a guess.
    assert record["default_branch"] == "develop"
    assert record["default_branch_source"] == "forge"
    assert record["visibility"] == "private"
    assert record["allowed_profiles"] == ["claude-code"]
    assert record["created_by"] == "alice@saga.xyz"
    assert record["access"] == {
        "token_scope": "tenant",
        "secret_name": "swarm-tenant-eng-git",
        "can_read": True,
        "can_push": True,
        "read_at": record["access"]["read_at"],
    }
    index = record["index"]
    assert index["interval_hours"] == 24
    assert index["on_change"] == "poll"
    assert index["min_change_interval_minutes"] == 30
    assert index["full_every_days"] == 7
    assert index["paused"] is False
    assert index["current_sha"] is None and index["in_flight_task_id"] is None
    # One read, of the repository, with the caller's tenant's token.
    assert secrets_reader.asked == ["swarm-tenant-eng-git"]
    assert [url for url, _ in transport.calls] == [
        "https://api.github.com/repos/saga-xyz/widgets"
    ]
    stored = db.docs[f"repositories/{record['repo_id']}"]
    assert stored["tenant_id"] == "eng" and stored["default_branch"] == "develop"


def test_the_token_goes_to_one_header_and_is_never_stored_or_served(make, db):
    client, secrets_reader, transport = make()
    response = _register(client)
    assert response.status_code == 201
    token = secrets_reader.issued["swarm-tenant-eng-git"]
    url, headers = transport.calls[0]
    assert url.startswith("https://api.github.com/")
    assert headers["Authorization"] == f"Bearer {token}"
    assert token not in response.text
    assert token not in json.dumps(db.dump(), default=str)
    listed = client.get("/v1/repositories", headers=auth_header("alice"))
    assert token not in listed.text


def test_registering_twice_is_idempotent(make, db):
    client, _, transport = make()
    first = _register(client)
    second = _register(client, repository="Saga-XYZ/Widgets")
    assert first.status_code == 201
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["repository"]["repo_id"] == first.json()["repository"]["repo_id"]
    assert len([p for p in db.docs if p.startswith("repositories/")]) == 1
    # The existing record is answered; the forge is not read again.
    assert len(transport.calls) == 1


def test_a_default_branch_override_is_kept_and_says_so(make):
    client, _, _ = make()
    response = _register(client, default_branch="release/2026")
    assert response.status_code == 201, response.text
    record = response.json()["repository"]
    assert record["default_branch"] == "release/2026"
    assert record["default_branch_source"] == "override"


def test_the_forge_spelling_of_the_name_is_stored(make):
    client, _, _ = make()
    record = _register(client, repository="SAGA-XYZ/WIDGETS").json()["repository"]
    assert record["repository"] == "saga-xyz/widgets"
    assert record["repo_id"] == _expected_id("eng", "saga-xyz/widgets")


def test_two_tenants_register_the_same_repository_as_two_records(make, db):
    client, secrets_reader, _ = make()
    eng = _register(client, user="alice").json()["repository"]
    research = _register(client, user="bob").json()["repository"]
    assert eng["repo_id"] != research["repo_id"]
    assert research["repo_id"] == _expected_id("research", "saga-xyz/widgets")
    assert research["access"]["secret_name"] == "swarm-tenant-research-git"
    assert secrets_reader.asked == ["swarm-tenant-eng-git", "swarm-tenant-research-git"]


# --------------------------------------------------------------------------
# validation: refused before the forge is read
# --------------------------------------------------------------------------

@pytest.mark.parametrize("repository", [
    "widgets",
    "saga-xyz/widgets/extra",
    "saga-xyz/",
    "/widgets",
    "-saga/widgets",
    "saga-xyz/.hidden",
    "saga-xyz/..",
    "saga xyz/widgets",
    "https://github.com/saga-xyz/widgets",
    "saga-xyz/widgets#12",
    "a" * 40 + "/widgets",
])
def test_a_repository_that_is_not_owner_slash_repo_is_refused(make, repository):
    client, secrets_reader, transport = make()
    response = _register(client, repository=repository)
    assert response.status_code == 422, response.text
    assert transport.calls == [] and secrets_reader.asked == []


@pytest.mark.parametrize("body", [
    {"allowed_profiles": ["no-such-profile"]},
    {"allowed_profiles": []},
    {"allowed_profiles": ["claude-code", "claude-code"]},
    {"default_branch": "../main"},
    {"default_branch": "-x"},
    {"default_branch": "main.lock"},
    {"default_branch": "feature//x"},
    {"default_branch": "has space"},
    {"index": {"interval_hours": 0}},
    {"index": {"interval_hours": 169}},
    {"index": {"interval_hours": "sometimes"}},
    {"index": {"on_change": "webhook"}},
    {"index": {"min_change_interval_minutes": 5}},
    {"index": {"full_every_days": 31}},
    {"index": {"current_sha": "f" * 40}},
    {"forge": "gitlab"},
    {"tenant_id": "research"},
    {"repo_id": "repo_0000000000000000"},
])
def test_invalid_registrations_are_refused_before_the_forge_is_read(make, db, body):
    client, secrets_reader, transport = make()
    response = _register(client, **body)
    assert response.status_code == 422, (body, response.text)
    assert transport.calls == [] and secrets_reader.asked == []
    assert not [p for p in db.docs if p.startswith("repositories/")]


@pytest.mark.parametrize("field", ["image", "command", "resources", "backend"])
def test_a_caller_cannot_name_an_image_command_or_resources(make, field):
    client, _, transport = make()
    response = _register(client, **{field: "x"})
    assert response.status_code == 422
    assert "runner_profile" in response.json()["message"]
    assert transport.calls == []


def test_interval_off_is_accepted(make):
    client, _, _ = make()
    response = _register(client, index={"interval_hours": "off", "on_change": "off"})
    assert response.status_code == 201, response.text
    assert response.json()["repository"]["index"]["interval_hours"] == "off"
    assert response.json()["repository"]["index"]["on_change"] == "off"


# --------------------------------------------------------------------------
# the forge read's refusals
# --------------------------------------------------------------------------

def test_a_repository_the_token_cannot_see_is_refused_no_access(make, db):
    client, secrets_reader, transport = make(transport=GitHubRepos())
    response = _register(client)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "no_access"
    assert "swarm-tenant-eng-git" in body["message"]
    assert secrets_reader.issued["swarm-tenant-eng-git"] not in response.text
    assert not [p for p in db.docs if p.startswith("repositories/")]


def test_a_token_without_pull_is_refused_no_access(make, db):
    transport = GitHubRepos(repos={"saga-xyz/widgets": repo_entry("saga-xyz/widgets", pull=False)})
    client, _, _ = make(transport=transport)
    response = _register(client)
    assert response.status_code == 403
    assert response.json()["code"] == "no_access"
    assert not [p for p in db.docs if p.startswith("repositories/")]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_no_access(make, status):
    client, _, _ = make(transport=GitHubRepos(status={"/repos/saga-xyz/widgets": status}))
    response = _register(client)
    assert response.status_code == 403
    assert response.json()["code"] == "no_access"


@pytest.mark.parametrize("status", [500, 429, 301])
def test_any_other_answer_is_read_failed(make, db, status):
    client, _, _ = make(transport=GitHubRepos(status={"/repos/saga-xyz/widgets": status}))
    response = _register(client)
    assert response.status_code == 502
    assert response.json()["code"] == "read_failed"
    assert not [p for p in db.docs if p.startswith("repositories/")]


def test_a_transport_failure_is_read_failed_and_quotes_nothing(make):
    secret_text = "token " + "y" * 30
    client, _, _ = make(transport=GitHubRepos(raises=OSError(secret_text)))
    response = _register(client)
    assert response.status_code == 502
    assert secret_text not in response.text


def test_a_tenant_without_a_git_secret_is_told_how_to_store_one(make, db):
    client, _, transport = make(secrets_reader=TenantTokens(have=("research",)))
    response = _register(client)
    assert response.status_code == 409
    assert response.json()["code"] == "no_forge_credential"
    assert "swarm-tenant-eng-git" in response.json()["message"]
    assert transport.calls == []


def test_an_answer_that_is_not_a_repository_is_read_failed(make):
    transport = GitHubRepos(repos={"saga-xyz/widgets": {"full_name": "other/name"}})
    client, _, _ = make(transport=transport)
    response = _register(client)
    assert response.status_code == 502
    assert response.json()["code"] == "read_failed"


# --------------------------------------------------------------------------
# read, list, patch, delete -- and another tenant's id is a 404
# --------------------------------------------------------------------------

def test_get_and_list_serve_the_tenants_own_records_only(make):
    client, _, _ = make()
    eng = _register(client, user="alice").json()["repository"]
    research = _register(client, user="bob").json()["repository"]

    one = client.get(f"/v1/repositories/{eng['repo_id']}", headers=auth_header("alice"))
    assert one.status_code == 200
    assert one.json()["repository"]["repo_id"] == eng["repo_id"]

    listed = client.get("/v1/repositories", headers=auth_header("alice")).json()
    assert [r["repo_id"] for r in listed["repositories"]] == [eng["repo_id"]]
    assert listed["tenant_id"] == "eng"
    listed_bob = client.get("/v1/repositories", headers=auth_header("bob")).json()
    assert [r["repo_id"] for r in listed_bob["repositories"]] == [research["repo_id"]]


def test_another_tenants_id_reads_exactly_as_a_missing_one(make, db):
    client, _, _ = make()
    eng = _register(client, user="alice").json()["repository"]
    other = client.get(f"/v1/repositories/{eng['repo_id']}", headers=auth_header("bob"))
    missing = client.get("/v1/repositories/repo_0123456789abcdef", headers=auth_header("bob"))
    assert other.status_code == missing.status_code == 404
    assert other.json()["code"] == missing.json()["code"] == "not_found"
    assert other.json()["message"].replace(eng["repo_id"], "X") == \
        missing.json()["message"].replace("repo_0123456789abcdef", "X")

    patched = client.patch(
        f"/v1/repositories/{eng['repo_id']}", json={"paused": True}, headers=auth_header("bob")
    )
    assert patched.status_code == 404
    deleted = client.delete(f"/v1/repositories/{eng['repo_id']}", headers=auth_header("bob"))
    assert deleted.status_code == 404
    stored = db.docs[f"repositories/{eng['repo_id']}"]
    assert stored["index"]["paused"] is False


def test_a_malformed_id_is_not_found(make):
    client, _, _ = make()
    response = client.get("/v1/repositories/not-an-id", headers=auth_header("alice"))
    assert response.status_code == 404


def test_patch_changes_the_settings_the_design_names(make, db):
    client, _, transport = make()
    record = _register(client).json()["repository"]
    calls = len(transport.calls)
    response = client.patch(
        f"/v1/repositories/{record['repo_id']}",
        json={
            "default_branch": "main",
            "allowed_profiles": ["claude-code", "codex"],
            "paused": True,
            "index": {"interval_hours": 6, "on_change": "off",
                      "min_change_interval_minutes": 60, "full_every_days": 3},
        },
        headers=auth_header("alice"),
    )
    assert response.status_code == 200, response.text
    updated = response.json()["repository"]
    assert updated["default_branch"] == "main"
    assert updated["default_branch_source"] == "override"
    assert updated["allowed_profiles"] == ["claude-code", "codex"]
    assert updated["index"]["paused"] is True
    assert updated["index"]["interval_hours"] == 6
    assert updated["index"]["on_change"] == "off"
    assert updated["index"]["min_change_interval_minutes"] == 60
    assert updated["index"]["full_every_days"] == 3
    # Untouched fields stay.
    assert updated["created_by"] == record["created_by"]
    assert updated["created_at"] == record["created_at"]
    assert db.docs[f"repositories/{record['repo_id']}"]["index"]["paused"] is True
    # A PATCH does not read the forge.
    assert len(transport.calls) == calls


@pytest.mark.parametrize("body", [
    {"owner": "other"},
    {"repo": "other"},
    {"tenant_id": "research"},
    {"repository_url": "https://github.com/other/x"},
    {"index": {"current_sha": "f" * 40}},
    {"index": {"interval_hours": 0}},
    {"allowed_profiles": ["no-such-profile"]},
    {"default_branch": "a..b"},
    {"image": "x"},
    {},
])
def test_patch_refuses_what_it_may_not_change(make, db, body):
    client, _, _ = make()
    record = _register(client).json()["repository"]
    before = dict(db.docs[f"repositories/{record['repo_id']}"])
    response = client.patch(
        f"/v1/repositories/{record['repo_id']}", json=body, headers=auth_header("alice")
    )
    assert response.status_code == 422, (body, response.text)
    assert db.docs[f"repositories/{record['repo_id']}"] == before


def test_delete_unregisters(make, db):
    client, _, _ = make()
    record = _register(client).json()["repository"]
    response = client.delete(f"/v1/repositories/{record['repo_id']}", headers=auth_header("alice"))
    assert response.status_code == 204
    assert f"repositories/{record['repo_id']}" not in db.docs
    again = client.get(f"/v1/repositories/{record['repo_id']}", headers=auth_header("alice"))
    assert again.status_code == 404
    gone = client.delete(f"/v1/repositories/{record['repo_id']}", headers=auth_header("alice"))
    assert gone.status_code == 404


def test_the_list_pages(make, db):
    names = [f"saga-xyz/repo-{i:02d}" for i in range(5)]
    transport = GitHubRepos(repos={n: repo_entry(n) for n in names})
    client, _, _ = make(transport=transport)
    for name in names:
        assert _register(client, repository=name).status_code == 201
    seen: list[str] = []
    token = None
    pages = 0
    while True:
        params = {"limit": 2}
        if token:
            params["page_token"] = token
        body = client.get("/v1/repositories", params=params, headers=auth_header("alice")).json()
        pages += 1
        seen += [r["repository"] for r in body["repositories"]]
        token = body["next_page_token"]
        if not token:
            break
    assert pages == 3
    # Every record once: paged by `repo_id`, which needs no composite index.
    assert sorted(seen) == sorted(names) and len(seen) == len(set(seen))


def test_the_module_never_accepts_a_credential_field():
    # The record holds no credential field at all (repo-index.md §1).
    for model in (repositories.RepositoryCreate, repositories.RepositoryPatch):
        for name in model.model_fields:
            assert not any(word in name for word in ("token", "secret", "password", "key"))
