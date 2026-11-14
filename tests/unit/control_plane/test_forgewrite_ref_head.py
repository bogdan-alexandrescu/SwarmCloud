"""`GitHubWriter.ref_head`: admission's read of a full refname
`repository_ref` (#3566).

WHAT IS HELD HERE
-----------------
* THE ROUTE IS `GET /repos/{owner}/{repo}/git/ref/{ref without refs/}`: a
  pull request's head (`refs/pull/<n>/head`), which the worker clones since
  #3347, is asked as that exact ref, and a 200 answers the object it names.
* A REF GITHUB DOES NOT HAVE (a 404) IS None.
* EVERY OTHER ANSWER IS THE WRITER'S OWN MAPPING, which admission reads as
  "not checked" and admits.

No cloud: GitHub is a recorded `send` under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlparse

import pytest

from swarm_api import forgewrite
from swarm_api.validation import IssueRef

from . import forge_fakes

REF = IssueRef(owner="example-org", repo="widgets", number=0)
SHA = secrets.token_hex(20)


def _send(answers: dict[str, tuple[int, bytes]], calls: list[tuple[str, str]]):
    def send(method, url, headers, body, timeout):
        path = urlparse(url).path
        calls.append((method, path))
        return answers.get(path, (404, b"{}"))
    return send


def test_a_pull_request_head_ref_is_read_as_that_exact_ref() -> None:
    calls: list[tuple[str, str]] = []
    path = "/repos/example-org/widgets/git/ref/pull/12/head"
    answer = json.dumps({"ref": "refs/pull/12/head", "object": {"sha": SHA}}).encode()
    writer = forgewrite.GitHubWriter(send=_send({path: (200, answer)}, calls))

    assert writer.ref_head(REF, "refs/pull/12/head", forge_fakes.make_token()) == SHA
    assert calls == [("GET", path)]


def test_a_ref_github_does_not_have_is_none() -> None:
    calls: list[tuple[str, str]] = []
    writer = forgewrite.GitHubWriter(send=_send({}, calls))

    assert writer.ref_head(REF, "refs/pull/99/head", forge_fakes.make_token()) is None
    assert calls == [("GET", "/repos/example-org/widgets/git/ref/pull/99/head")]


def test_a_name_that_is_not_a_full_refname_is_an_error() -> None:
    writer = forgewrite.GitHubWriter(send=_send({}, []))

    with pytest.raises(ValueError):
        writer.ref_head(REF, "pull/12/head", forge_fakes.make_token())


def test_a_200_naming_no_object_is_an_error() -> None:
    path = "/repos/example-org/widgets/git/ref/pull/12/head"
    writer = forgewrite.GitHubWriter(send=_send({path: (200, b'{"ref": "x"}')}, []))

    with pytest.raises(forgewrite.ForgeWriteError):
        writer.ref_head(REF, "refs/pull/12/head", forge_fakes.make_token())


@pytest.mark.parametrize(("status", "error"), [
    (401, forgewrite.ForgeWriteUnauthorized),
    (403, forgewrite.ForgeWriteForbidden),
    (502, forgewrite.ForgeUnreachable),
    (429, forgewrite.ForgeRateLimited),
])
def test_a_refusal_is_the_writers_usual_error(status, error) -> None:
    path = "/repos/example-org/widgets/git/ref/pull/12/head"
    writer = forgewrite.GitHubWriter(send=_send({path: (status, b"{}")}, []))

    with pytest.raises(error):
        writer.ref_head(REF, "refs/pull/12/head", forge_fakes.make_token())
