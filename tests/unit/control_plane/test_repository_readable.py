"""`GET /v1/repositories/readable`: what the tenant's git token can read (Register C).

The owner picked Register C on 2026-10-05 (PICKS.md): the console lists what
the tenant's token can read and a person picks one. The list is GitHub's
`GET /user/repos` read with the caller's TENANT's token, one GitHub page of
PAGE_SIZE per call, and never past MAX_READABLE_PAGES: a token that can read
more says `capped`, and the person registers the rest by typing
`owner/repo`, which POST /v1/repositories accepts either way.
"""

from __future__ import annotations

import pytest

from swarm_api import forge, repositories

from .conftest import auth_header
from .repo_fakes import GitHubRepos, TenantTokens, make_client, repo_entry


@pytest.fixture
def make(db, tokens, group_map, objects):
    def build(listing, secrets_reader=None, **kwargs):
        secrets_reader = secrets_reader or TenantTokens()
        repos = {e["full_name"]: e for e in listing if isinstance(e, dict) and "full_name" in e}
        transport = GitHubRepos(repos=repos, listing=listing, **kwargs)
        client = make_client(
            db, tokens, group_map, objects, forge_tokens=secrets_reader, transport=transport
        )
        return client, secrets_reader, transport
    return build


def _readable(client, user="alice", **params):
    return client.get("/v1/repositories/readable", params=params, headers=auth_header(user))


def _listing(count: int) -> list[dict]:
    return [repo_entry(f"saga-xyz/repo-{i:04d}") for i in range(count)]


def test_the_first_page_is_one_github_page(make):
    client, secrets_reader, transport = make(_listing(250))
    response = _readable(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["repositories"]) == forge.PAGE_SIZE
    assert body["page"] == 1
    assert body["next_page"] == 2
    assert body["capped"] is False
    assert body["per_page"] == forge.PAGE_SIZE
    assert body["max_pages"] == repositories.MAX_READABLE_PAGES
    assert body["secret_name"] == "swarm-tenant-eng-git"
    assert body["token_scope"] == "tenant"
    assert secrets_reader.asked == ["swarm-tenant-eng-git"]
    assert len(transport.calls) == 1
    url, headers = transport.calls[0]
    assert url.startswith("https://api.github.com/user/repos?")
    assert f"per_page={forge.PAGE_SIZE}" in url and "page=1" in url
    first = body["repositories"][0]
    assert first == {
        "repository": "saga-xyz/repo-0000",
        "owner": "saga-xyz",
        "repo": "repo-0000",
        "visibility": "private",
        "default_branch": "main",
        "archived": False,
        "can_push": True,
        "can_admin": False,
        "repo_id": repositories.repo_id_for("eng", "saga-xyz", "repo-0000"),
        "registered": False,
    }


def test_the_last_short_page_has_no_next(make):
    client, _, _ = make(_listing(250))
    body = _readable(client, page=3).json()
    assert len(body["repositories"]) == 50
    assert body["next_page"] is None
    assert body["capped"] is False


def test_the_list_stops_at_the_cap_and_says_so(make):
    total = forge.PAGE_SIZE * repositories.MAX_READABLE_PAGES + 1
    client, _, transport = make(_listing(total))
    last = _readable(client, page=repositories.MAX_READABLE_PAGES).json()
    assert len(last["repositories"]) == forge.PAGE_SIZE
    assert last["next_page"] is None
    assert last["capped"] is True
    beyond = _readable(client, page=repositories.MAX_READABLE_PAGES + 1)
    assert beyond.status_code == 422
    # The refused page never reached the forge.
    assert len(transport.calls) == 1


def test_a_full_last_page_with_nothing_after_it_is_still_capped(make):
    # GitHub gives no total; a full last page may or may not have more after it.
    client, _, _ = make(_listing(forge.PAGE_SIZE * repositories.MAX_READABLE_PAGES))
    last = _readable(client, page=repositories.MAX_READABLE_PAGES).json()
    assert last["capped"] is True and last["next_page"] is None


@pytest.mark.parametrize("page", [0, -1])
def test_a_page_below_one_is_refused(make, page):
    client, _, transport = make(_listing(3))
    assert _readable(client, page=page).status_code == 422
    assert transport.calls == []


def test_registered_repositories_are_marked_for_the_tenant_only(make):
    client, _, _ = make(_listing(3))
    assert client.post(
        "/v1/repositories", json={"repository": "saga-xyz/repo-0001"},
        headers=auth_header("alice"),
    ).status_code == 201
    eng = _readable(client, user="alice").json()["repositories"]
    assert [r["registered"] for r in eng] == [False, True, False]
    research = _readable(client, user="bob").json()["repositories"]
    assert [r["registered"] for r in research] == [False, False, False]
    assert research[1]["repo_id"] == repositories.repo_id_for("research", "saga-xyz", "repo-0001")


def test_entries_that_are_not_a_repository_name_are_skipped_and_counted(make):
    listing = _listing(2) + [
        {"full_name": "bad name/x", "owner": {"login": "bad name"}, "name": "x"},
        {"full_name": "saga-xyz/.hidden", "owner": {"login": "saga-xyz"}, "name": ".hidden"},
        "not a dict",
        {"owner": {"login": "saga-xyz"}},
    ]
    client, _, _ = make(listing)
    body = _readable(client).json()
    assert [r["repository"] for r in body["repositories"]] == [
        "saga-xyz/repo-0000", "saga-xyz/repo-0001"
    ]
    assert body["skipped"] == 4


def test_the_token_is_never_served(make):
    client, secrets_reader, _ = make(_listing(2))
    response = _readable(client)
    assert secrets_reader.issued["swarm-tenant-eng-git"] not in response.text


def test_a_refused_token_is_no_access_naming_the_secret(make):
    client, secrets_reader, _ = make(_listing(2), status={"/user/repos": 401})
    response = _readable(client)
    assert response.status_code == 403
    assert response.json()["code"] == "no_access"
    assert "swarm-tenant-eng-git" in response.json()["message"]
    assert secrets_reader.issued["swarm-tenant-eng-git"] not in response.text


def test_a_failed_read_is_read_failed(make):
    client, _, _ = make(_listing(2), status={"/user/repos": 500})
    response = _readable(client)
    assert response.status_code == 502
    assert response.json()["code"] == "read_failed"


def test_no_git_secret_is_no_forge_credential(make):
    client, _, transport = make(_listing(2), secrets_reader=TenantTokens(have=()))
    response = _readable(client)
    assert response.status_code == 409
    assert response.json()["code"] == "no_forge_credential"
    assert transport.calls == []
