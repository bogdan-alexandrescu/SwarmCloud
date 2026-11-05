"""`GitHubWriter.commit_head`: admission's one read of a full-sha
`repository_ref` (#3315).

WHAT IS HELD HERE
-----------------
* THE ROUTE IS `GET /repos/{owner}/{repo}/commits/{sha}`, and a 200 answers
  the sha GitHub names.
* A COMMIT GITHUB DOES NOT HAVE IS None: its 422 "No commit found for SHA",
  and a 404.
* EVERY OTHER ANSWER IS THE WRITER'S OWN MAPPING (401, 403, 5xx, a 429, a
  transport error), which admission reads as "not checked" and admits.

No cloud: GitHub is the in-memory fake under the real `GitHubWriter`.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

import pytest

from swarm_api import forgewrite
from swarm_api.validation import IssueRef

from . import forge_fakes

REF = IssueRef(owner="example-org", repo="widgets", number=0)
SHA = secrets.token_hex(20)
COMMIT_PATH = f"/repos/example-org/widgets/commits/{SHA}"


def _writer(writes: forge_fakes.GitHubWrites) -> forgewrite.GitHubWriter:
    return forgewrite.GitHubWriter(send=writes)


def test_a_commit_github_has_answers_its_sha_from_the_commits_route() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.commits.add(SHA)

    assert _writer(writes).commit_head(REF, SHA, forge_fakes.make_token()) == SHA
    assert [(m, urlparse(u).path) for m, u, _, _ in writes.calls] == [("GET", COMMIT_PATH)]


def test_no_commit_found_for_sha_is_none() -> None:
    writes = forge_fakes.GitHubWrites()

    assert _writer(writes).commit_head(REF, SHA, forge_fakes.make_token()) is None


def test_a_404_is_none() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.refuse[("GET", COMMIT_PATH)] = 404

    assert _writer(writes).commit_head(REF, SHA, forge_fakes.make_token()) is None


def test_a_200_naming_no_sha_is_an_error() -> None:
    def send(method, url, headers, body, timeout):
        return 200, b'{"commit": {}}'

    with pytest.raises(forgewrite.ForgeWriteError):
        forgewrite.GitHubWriter(send=send).commit_head(REF, SHA, forge_fakes.make_token())


def test_another_422_is_an_error_not_a_missing_commit() -> None:
    def send(method, url, headers, body, timeout):
        return 422, b'{"message": "Validation Failed"}'

    with pytest.raises(forgewrite.ForgeWriteError):
        forgewrite.GitHubWriter(send=send).commit_head(REF, SHA, forge_fakes.make_token())


@pytest.mark.parametrize(("status", "error"), [
    (401, forgewrite.ForgeWriteUnauthorized),
    (403, forgewrite.ForgeWriteForbidden),
    (500, forgewrite.ForgeUnreachable),
    (502, forgewrite.ForgeUnreachable),
    (429, forgewrite.ForgeRateLimited),
])
def test_a_refusal_is_the_writers_usual_error(status, error) -> None:
    writes = forge_fakes.GitHubWrites()
    writes.commits.add(SHA)
    writes.refuse[("GET", COMMIT_PATH)] = status

    with pytest.raises(error):
        _writer(writes).commit_head(REF, SHA, forge_fakes.make_token())


def test_github_unreachable_is_the_writers_unreachable() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.raise_for[("GET", COMMIT_PATH)] = TimeoutError("timed out")

    with pytest.raises(forgewrite.ForgeUnreachable):
        _writer(writes).commit_head(REF, SHA, forge_fakes.make_token())
