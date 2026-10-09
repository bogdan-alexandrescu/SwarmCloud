"""D6's opt-in push test: verify writes only when the person asks it to
(docs/onboarding.md §6 D6; #780).

What is held here, every case offline against the access API's forge fake:

  * verify without `push_test` makes no write call -- the default checks
    read, as D6 (a) decided;
  * `push_test` on a write grant reads the default branch head with the
    person's token, creates exactly one
    `refs/heads/swarmcloud/onboarding-check-<16 hex>` at that head, deletes
    it, and records `checks.push_test` ok;
  * `push_test` on a read grant is refused before any forge call: SwarmCloud
    enforces read (D9), so it never pushes where the person chose read;
  * a 403 on create records `missing` with PERMISSION_MISSING copy, a 503
    records `unknown` (never `missing`) and still tries the delete, since an
    unanswered create may have been made, and a delete that fails answers
    `leftover: true` with the branch name, so the person can delete it.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

from swarm_api import forgeapp
from swarm_api.onboarding import recovery_copy

from .conftest import auth_header
from .test_access_api import (  # noqa: F401  (fixtures)
    LOGIN,
    ORG,
    AccessGitHub,
    _connect,
    _enable,
    _grant,
    _no_value_anywhere,
    _rid,
    api,
    clock,
    issues_transport,
    slots,
    tenant_tokens,
)
from .test_forgeapp import _json

REPOSITORY = f"{ORG}/repo-0001"
HEAD = "a" * 40
BRANCH = re.compile(r"^swarmcloud/onboarding-check-[0-9a-f]{16}$")


class RefsGitHub(AccessGitHub):
    """AccessGitHub plus the refs a push test reads, creates and deletes.
    `create_status` / `delete_status` override GitHub's answer; 503 is
    GitHub not answering."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: dict[str, str] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.create_status: int | None = None
        self.delete_status: int | None = None

    def __call__(self, method, url, headers, body, timeout):  # noqa: ANN001
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.netloc != "api.github.com" or len(parts) < 5 or parts[0] != "repos" \
                or parts[3] != "git" or parts[4] not in ("ref", "refs"):
            return super().__call__(method, url, headers, body, timeout)
        sent = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": sent})
        token = self._bearer(headers)
        if token is None or token not in self.access_tokens() or token in self.dead:
            return _json(401, {"message": "Bad credentials"})
        ref = unquote("/".join(parts[5:]))
        if method == "GET" and parts[4] == "ref":
            if ref != "heads/main":
                return _json(404, {"message": "Not Found"})
            return _json(200, {"ref": "refs/heads/main", "object": {"sha": HEAD}})
        if method == "POST" and parts[4] == "refs":
            self.created.append(sent)
            if self.create_status is not None:
                return forgeapp.HttpAnswer(self.create_status, {}, b'{"message": "no"}')
            self.refs[sent["ref"]] = sent["sha"]
            return _json(201, {"ref": sent["ref"], "object": {"sha": sent["sha"]}})
        if method == "DELETE" and parts[4] == "refs":
            self.deleted.append(ref)
            if self.delete_status is not None:
                return forgeapp.HttpAnswer(self.delete_status, {}, b'{"message": "no"}')
            self.refs.pop("refs/" + ref, None)
            return forgeapp.HttpAnswer(204, {}, b"")
        return _json(404, {"message": "Not Found"})


@pytest.fixture
def github() -> RefsGitHub:
    return RefsGitHub()


def _writes(github: RefsGitHub) -> list[dict[str, Any]]:
    return [c for c in github.calls if c["method"] != "GET"
            and not c["url"].startswith(forgeapp.TOKEN_URL)]


def _ready(api, github, mode: str = "write") -> None:
    _connect(api, github)
    _enable(api)
    assert _grant(api, REPOSITORY, mode=mode).status_code == 200


def _verify(api, checks: list[str] | None = None):
    return api.post(f"/v1/access/grants/{_rid(REPOSITORY)}/verify",
                    json={"checks": checks} if checks else None,
                    headers=auth_header("alice"))


def test_verify_without_push_test_makes_no_write_call(api, github):
    _ready(api, github)
    for checks in (None, ["clone", "push", "pull_request"]):
        answer = _verify(api, checks)
        assert answer.status_code == 200, answer.text
        assert "push_test" not in answer.json()["grant"]["checks"]
    assert _writes(github) == []
    assert github.created == [] and github.deleted == []


def test_push_test_creates_one_branch_at_the_default_head_and_deletes_it(api, github, db,
                                                                         caplog):
    _ready(api, github)
    caplog.set_level(logging.INFO)
    answer = _verify(api, ["push_test"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert len(github.created) == 1
    created = github.created[0]
    assert created["sha"] == HEAD
    assert created["ref"].startswith("refs/heads/")
    branch = created["ref"][len("refs/heads/"):]
    assert BRANCH.match(branch), branch
    assert github.deleted == [f"heads/{branch}"]
    assert github.refs == {}
    check = body["grant"]["checks"]["push_test"]
    assert check["state"] == "ok" and check["checked_at"]
    assert check["leftover"] is False
    assert body["passed"] is True and body["failures"] == []
    stored = db.docs[next(k for k in db.docs if k.startswith("forge_grants/"))]
    assert stored["checks"]["push_test"]["state"] == "ok"
    # Only the opt-in branch was written: one create, one delete.
    assert [c["method"] for c in _writes(github)] == ["POST", "DELETE"]
    # Made with the person's own token, as every access read is.
    tokens = {c["headers"]["Authorization"] for c in _writes(github)}
    assert len(tokens) == 1 and tokens.pop()[len("Bearer "):] in github.access_tokens()
    _no_value_anywhere(db, github, [answer.text, caplog.text])


def test_push_test_on_a_read_grant_is_refused_with_no_forge_call(api, github):
    _ready(api, github, mode="read")
    before = len(github.calls)
    answer = _verify(api, ["push_test"])
    assert answer.status_code == 422, answer.text
    assert "read" in answer.text
    assert github.calls[before:] == []
    assert github.created == []


def test_a_403_on_create_is_missing_with_permission_missing_copy(api, github):
    _ready(api, github)
    github.create_status = 403
    body = _verify(api, ["push_test"]).json()
    assert body["grant"]["checks"]["push_test"]["state"] == "missing"
    assert body["passed"] is False
    failure = next(f for f in body["failures"] if f["check"] == "push_test")
    assert failure["code"] == "PERMISSION_MISSING"
    assert failure["copy"] == recovery_copy("PERMISSION_MISSING", repo=REPOSITORY, login=LOGIN)
    assert github.deleted == []


def test_a_404_on_create_is_missing_too(api, github):
    _ready(api, github)
    github.create_status = 404
    body = _verify(api, ["push_test"]).json()
    assert body["grant"]["checks"]["push_test"]["state"] == "missing"
    assert body["failures"][0]["code"] == "PERMISSION_MISSING"


@pytest.mark.parametrize("status", [503, 429])
def test_a_create_github_did_not_answer_is_unknown_not_missing(api, github, status):
    _ready(api, github)
    github.create_status = status
    body = _verify(api, ["push_test"]).json()
    assert body["grant"]["checks"]["push_test"]["state"] == "unknown"
    assert {f["code"] for f in body["failures"]} == {"FORGE_UNREACHABLE"}
    # A create that was not answered may still have been made: its delete is
    # tried, and a delete GitHub accepted leaves nothing to report.
    assert len(github.deleted) == 1
    assert "push_test" not in body


def test_a_failed_delete_answers_leftover_with_the_branch_name(api, github, db, caplog):
    _ready(api, github)
    github.delete_status = 503
    caplog.set_level(logging.INFO)
    answer = _verify(api, ["push_test"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    branch = github.created[0]["ref"][len("refs/heads/"):]
    check = body["grant"]["checks"]["push_test"]
    assert check["state"] == "ok"
    assert check["leftover"] is True and check["branch"] == branch
    assert body["push_test"] == {"branch": branch, "leftover": True}
    assert branch in caplog.text
    _no_value_anywhere(db, github, [answer.text, caplog.text])
