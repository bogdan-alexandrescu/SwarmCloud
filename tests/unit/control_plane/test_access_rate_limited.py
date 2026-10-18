"""A GitHub rate limit is never reported as a missing permission (#1483).

On 2026-10-17 Work › Access told the owner to "ask for write access" to a
repository they administer: the push test read GitHub's 403 as a refusal,
while GitHub was answering 403 with `X-RateLimit-Remaining: 0` for their
user. What is held here, offline against the access API's forge fakes:

  * `forge_limits.rate_limit` is the one classification: a 429; a 403 with
    `X-RateLimit-Remaining: 0`; a 403 or 429 with `Retry-After`; a 403 whose
    message says "rate limit" (primary or secondary). A 403 "Resource not
    accessible by integration" with quota left is not one;
  * the push test's head read and its ref create, rate-limited, are
    `unknown` / FORGE_UNREACHABLE with copy naming the reset as a UTC time
    and nothing about permissions; a create's delete is still tried;
  * the real permission case stays PERMISSION_MISSING;
  * the verify reads, the grant read and the owner refusal behave the same;
  * the token probe (`gittokens`) and the checklist (`onboarding`) read the
    same limit: a probe's rate-limited read is FORGE_UNREACHABLE on the
    checklist, with the reset in its copy.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import access, forge_limits, forgeapp, onboarding
from swarm_api.onboarding import COPY, recovery_copy

from .conftest import auth_header
from .test_access_api import (  # noqa: F401  (fixtures)
    LOGIN,
    ORG,
    _connect,
    _enable,
    _grant,
    _rid,
    api,
    clock,
    issues_transport,
    slots,
    tenant_tokens,
)
from .test_access_push_test import REPOSITORY, RefsGitHub
from .test_git_token_probe import ProbeForge, classic_value, run

#: The issue's reading: `X-RateLimit-Reset: 1792203190`.
RESET_EPOCH = 1792203190
RESET_AT = datetime.fromtimestamp(RESET_EPOCH, tz=timezone.utc).strftime("%H:%M UTC")


def _limited(status: int = 403, *, reset: bool = True, retry_after: str | None = None,
             message: str = "API rate limit exceeded for user ID 5000393.",
             remaining: str | None = "0") -> forgeapp.HttpAnswer:
    headers = {"content-type": "application/json", "x-ratelimit-resource": "core"}
    if remaining is not None:
        headers["x-ratelimit-remaining"] = remaining
    if reset:
        headers["x-ratelimit-reset"] = str(RESET_EPOCH)
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return forgeapp.HttpAnswer(status, headers, json.dumps({"message": message}).encode())


def _not_accessible() -> forgeapp.HttpAnswer:
    return forgeapp.HttpAnswer(
        403, {"content-type": "application/json", "x-ratelimit-remaining": "4321"},
        json.dumps({"message": "Resource not accessible by integration"}).encode())


class LimitedGitHub(RefsGitHub):
    """RefsGitHub that answers a chosen read with a chosen answer: `answers`
    maps (method, path suffix or URL substring) to it."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[tuple[str, str], forgeapp.HttpAnswer] = {}

    def __call__(self, method, url, headers, body, timeout):  # noqa: ANN001
        parsed = urlparse(url)
        if parsed.netloc in ("api.github.com", "github.com") \
                and not url.startswith(forgeapp.TOKEN_URL):
            for (want_method, where), answer in self.answers.items():
                if method == want_method and (parsed.path.endswith(where) or where in url):
                    self.calls.append({"method": method, "url": url, "headers": dict(headers),
                                       "body": json.loads(body) if body else None})
                    if method == "POST":
                        self.created.append(json.loads(body) if body else None)
                    return answer
        return super().__call__(method, url, headers, body, timeout)


@pytest.fixture
def github() -> LimitedGitHub:
    return LimitedGitHub()


def _ready(api, github, mode: str = "write") -> None:
    _connect(api, github)
    _enable(api)
    assert _grant(api, REPOSITORY, mode=mode).status_code == 200


def _verify(api, checks: list[str] | None = None) -> dict[str, Any]:
    answer = api.post(f"/v1/access/grants/{_rid(REPOSITORY)}/verify",
                      json={"checks": checks} if checks else None,
                      headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    return answer.json()


def _says_nothing_about_permissions(copy: str) -> None:
    lowered = copy.lower()
    assert "write access" not in lowered and "permission" not in lowered
    assert "ask for" not in lowered


# -- the one classification ------------------------------------------------------


@pytest.mark.parametrize("answer", [
    _limited(),
    _limited(reset=False),
    _limited(429, remaining=None, reset=False, message="slow down"),
    _limited(429, remaining="12", reset=False, retry_after="60", message="slow down"),
    _limited(403, remaining="12", reset=False, retry_after="60", message="slow down"),
    _limited(403, remaining="12", reset=False,
             message="You have exceeded a secondary rate limit. Please wait a few minutes."),
    _limited(403, remaining=None, reset=False, message="API rate limit exceeded for 1.2.3.4."),
], ids=["403-remaining-0", "403-remaining-0-no-reset", "429", "429-retry-after",
        "403-retry-after", "403-secondary-message", "403-primary-message"])
def test_every_rate_limited_answer_is_recognised(answer):
    assert forge_limits.is_rate_limited(answer)


@pytest.mark.parametrize("answer", [
    _not_accessible(),
    forgeapp.HttpAnswer(403, {}, b'{"message": "Must have admin rights to Repository."}'),
    forgeapp.HttpAnswer(404, {"x-ratelimit-remaining": "0"}, b'{"message": "Not Found"}'),
    forgeapp.HttpAnswer(200, {"x-ratelimit-remaining": "0"}, b"{}"),
    forgeapp.HttpAnswer(403, {}, b"not json"),
], ids=["not-accessible", "admin-rights", "404", "200-last-request", "403-no-body"])
def test_a_refusal_with_quota_left_is_not_a_rate_limit(answer):
    assert not forge_limits.is_rate_limited(answer)


def test_the_reset_is_read_from_the_reset_header_then_retry_after():
    now = datetime(2026, 10, 17, 2, 1, 0, tzinfo=timezone.utc)
    limit = forge_limits.rate_limit(_limited(), now=now)
    assert limit is not None and limit.reset == datetime.fromtimestamp(RESET_EPOCH,
                                                                       tz=timezone.utc)
    after = forge_limits.rate_limit(_limited(429, reset=False, retry_after="120"), now=now)
    assert after is not None and after.reset == now + timedelta(seconds=120)
    dated = forge_limits.rate_limit(
        _limited(429, reset=False, retry_after="Sat, 17 Oct 2026 03:13:10 GMT"), now=now)
    assert dated is not None and forge_limits.until(dated) == "03:13 UTC"
    assert forge_limits.until(forge_limits.rate_limit(_limited(reset=False))) is None


def test_the_copy_names_the_reset_and_nothing_about_permissions():
    copy = forge_limits.limited_copy("03:13 UTC")
    assert copy == ("GitHub is limiting requests for this account until 03:13 UTC; nothing "
                    "was checked. Try again after that.")
    _says_nothing_about_permissions(copy)
    _says_nothing_about_permissions(forge_limits.limited_copy(None))


# -- the push test --------------------------------------------------------------


def test_a_rate_limited_head_read_is_unknown_with_the_reset(api, github):
    _ready(api, github)
    github.answers[("GET", "/git/ref/heads/main")] = _limited()
    body = _verify(api, ["push_test"])
    check = body["grant"]["checks"]["push_test"]
    assert check["state"] == "unknown"
    (failure,) = [f for f in body["failures"] if f["check"] == "push_test"]
    assert failure["code"] == "FORGE_UNREACHABLE"
    assert RESET_AT in failure["copy"]
    assert failure["copy"] == forge_limits.limited_copy(RESET_AT)
    _says_nothing_about_permissions(failure["copy"])
    # Nothing was created, so there is nothing to delete.
    assert github.created == [] and github.deleted == []


def test_a_rate_limited_ref_create_is_unknown_and_its_delete_still_runs(api, github):
    _ready(api, github)
    github.answers[("POST", "/git/refs")] = _limited()
    body = _verify(api, ["push_test"])
    assert body["grant"]["checks"]["push_test"]["state"] == "unknown"
    (failure,) = [f for f in body["failures"] if f["check"] == "push_test"]
    assert failure["code"] == "FORGE_UNREACHABLE"
    assert failure["copy"] == forge_limits.limited_copy(RESET_AT)
    assert body["passed"] is False
    # The delete in `finally` is tried, as for any create GitHub did not
    # accept with an answer: unchanged from before (#1483 point 3).
    assert len(github.created) == 1 and len(github.deleted) == 1


def test_a_429_with_retry_after_on_create_is_unknown_with_the_reset(api, github, clock):
    _ready(api, github)
    github.answers[("POST", "/git/refs")] = _limited(429, reset=False, retry_after="600",
                                                     remaining=None, message="slow down")
    body = _verify(api, ["push_test"])
    assert body["grant"]["checks"]["push_test"]["state"] == "unknown"
    (failure,) = [f for f in body["failures"] if f["check"] == "push_test"]
    assert failure["code"] == "FORGE_UNREACHABLE"
    assert failure["copy"].startswith("GitHub is limiting requests for this account until ")
    assert failure["copy"].endswith(" UTC; nothing was checked. Try again after that.")
    assert len(github.deleted) == 1


def test_a_real_refusal_on_create_is_still_permission_missing(api, github):
    _ready(api, github)
    github.answers[("POST", "/git/refs")] = _not_accessible()
    body = _verify(api, ["push_test"])
    assert body["grant"]["checks"]["push_test"]["state"] == "missing"
    (failure,) = [f for f in body["failures"] if f["check"] == "push_test"]
    assert failure["code"] == "PERMISSION_MISSING"
    assert failure["copy"] == recovery_copy("PERMISSION_MISSING", repo=REPOSITORY, login=LOGIN)
    assert github.deleted == []


def test_a_real_refusal_on_the_head_read_is_still_permission_missing(api, github):
    _ready(api, github)
    github.answers[("GET", "/git/ref/heads/main")] = _not_accessible()
    body = _verify(api, ["push_test"])
    assert body["grant"]["checks"]["push_test"]["state"] == "missing"
    assert {f["code"] for f in body["failures"]} == {"PERMISSION_MISSING"}


# -- the verify reads -------------------------------------------------------------


def test_a_rate_limited_repository_read_leaves_every_check_unknown(api, github):
    _ready(api, github)
    github.answers[("GET", f"/repos/{REPOSITORY}")] = _limited()
    body = _verify(api)
    states = {k: v["state"] for k, v in body["grant"]["checks"].items()}
    assert states == {"clone": "unknown", "push": "unknown", "pull_request": "unknown"}
    assert {f["code"] for f in body["failures"]} == {"FORGE_UNREACHABLE"}
    for failure in body["failures"]:
        assert failure["copy"] == forge_limits.limited_copy(RESET_AT)


def test_a_rate_limited_git_read_is_unknown_not_missing(api, github):
    _ready(api, github)
    github.answers[("GET", "service=git-receive-pack")] = _limited(remaining=None,
                                                                   message="rate limit")
    body = _verify(api, ["push", "pull_request"])
    states = {k: v["state"] for k, v in body["grant"]["checks"].items()}
    assert states["push"] == "unknown" and states["pull_request"] == "unknown"
    assert {f["code"] for f in body["failures"]} == {"FORGE_UNREACHABLE"}
    for failure in body["failures"]:
        _says_nothing_about_permissions(failure["copy"])


def test_a_rate_limited_installations_read_leaves_pull_request_unknown(api, github):
    _ready(api, github)
    github.answers[("GET", "/user/installations?")] = _limited()
    body = _verify(api, ["pull_request"])
    assert body["grant"]["checks"]["pull_request"]["state"] == "unknown"
    assert body["failures"][0]["code"] == "FORGE_UNREACHABLE"
    assert body["failures"][0]["copy"] == forge_limits.limited_copy(RESET_AT)


def test_a_real_git_refusal_stays_permission_missing(api, github):
    _ready(api, github)
    github.no_push_git.add(REPOSITORY)
    body = _verify(api, ["push"])
    assert body["grant"]["checks"]["push"]["state"] == "missing"
    assert body["failures"][0]["code"] == "PERMISSION_MISSING"


# -- the grant read and the owner refusal -----------------------------------------


def test_a_rate_limited_grant_read_is_forge_unreachable_not_a_refusal(api, github):
    _connect(api, github)
    _enable(api)
    github.answers[("GET", f"/repos/{REPOSITORY}")] = _limited()
    answer = _grant(api, REPOSITORY)
    assert answer.status_code == 503, answer.text
    detail = answer.json()["detail"]
    assert detail["failure_code"] == "FORGE_UNREACHABLE"
    assert detail["recovery"] == forge_limits.limited_copy(RESET_AT)


def test_a_rate_limited_repository_listing_is_forge_unreachable(api, github):
    _connect(api, github)
    _enable(api)
    github.answers[("GET", "/repositories?")] = _limited()
    answer = api.get(f"/v1/access/orgs/{ORG}/repositories", headers=auth_header("alice"))
    assert answer.status_code == 503, answer.text
    assert answer.json()["detail"]["failure_code"] == "FORGE_UNREACHABLE"
    assert answer.json()["detail"]["recovery"] == forge_limits.limited_copy(RESET_AT)


@pytest.mark.parametrize("org", [
    {"method": forgeapp.METHOD_PAT, "owner": ORG, "owner_type": "Organization",
     "token_kind": "classic_pat", "forge_login": LOGIN},
    {"method": "app", "owner": ORG, "installation_id": 11},
], ids=["via-token", "via-app"])
def test_the_owner_refusal_asks_the_rate_limit_first(org):
    refused = access.AccessService._owner_refusal(None, _limited(), ORG, "repo-0001", org)
    assert refused.detail["failure_code"] == "FORGE_UNREACHABLE"
    assert refused.status_code == 503
    assert refused.detail["recovery"] == forge_limits.limited_copy(RESET_AT)
    if org["method"] == forgeapp.METHOD_PAT:
        real = access.AccessService._owner_refusal(None, _not_accessible(), ORG, "repo-0001",
                                                   org)
        assert real.detail["failure_code"] == "PERMISSION_MISSING"


# -- the token probe and the checklist ---------------------------------------------


def _probe_errors(result) -> str:
    return " ".join([result.error or ""] + [pair.error or "" for pair in result.pairs])


def test_the_probe_reads_a_rate_limit_message_with_quota_left_as_no_answer():
    limited = ProbeForge(status={"repo": 403},
                         headers={"repo": {"x-ratelimit-remaining": "17",
                                           "retry-after": "60"}})
    result = run(classic_value(), limited)
    assert not result.complete
    assert forge_limits.SPENT in _probe_errors(result)


def test_the_probe_names_the_reset_it_was_given():
    limited = ProbeForge(status={"repo": 403},
                         headers={"repo": {"x-ratelimit-remaining": "0",
                                           "x-ratelimit-reset": str(RESET_EPOCH)}})
    result = run(classic_value(), limited)
    assert not result.complete
    assert f"{forge_limits.SPENT} until {RESET_AT}" in _probe_errors(result)


def test_the_checklist_reads_a_rate_limited_probe_as_unreachable_with_the_reset():
    error = f"repo: GitHub answered HTTP 403 {forge_limits.SPENT} until {RESET_AT}"
    assert onboarding._unanswered(error)
    assert onboarding.unreachable_copy(error) == forge_limits.limited_copy(RESET_AT)
    assert onboarding._unanswered("repo: GitHub answered HTTP 403 " + forge_limits.SPENT)
    # A 5xx is still GitHub not answering, with the copy it always had.
    assert onboarding._unanswered("repo: GitHub answered HTTP 502")
    assert onboarding.unreachable_copy("repo: GitHub answered HTTP 502") == \
        COPY["FORGE_UNREACHABLE"]
    # A refusal is not.
    assert not onboarding._unanswered("repo: GitHub answered HTTP 403")
