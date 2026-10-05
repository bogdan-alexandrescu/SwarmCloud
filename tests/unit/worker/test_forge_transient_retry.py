"""A forge that blinked is retried; a forge that answered "no" is not (F1, 2026-10-04).

THE MEASURED FAILURE. Issue run run_51e2e460eef54d208986 (#72): its review
step task_fbd5bfd38f70431080fc ended FAILED after ONE attempt (08:55:36-
08:56:37Z) with "could not reach api.github.com: timed out", end cause
INPUTS_UNAVAILABLE, and `on_step_failure=fail_workflow` cancelled the fix
step behind it. The same timeout turned release 37017777271's acceptance red
on 2026-10-02 ("no pull request was opened: could not reach the forge").
Nothing in the worker told a network blip from a missing issue.

WHAT IS PINNED HERE, below the lifecycle (the lifecycle's end is pinned in
test_issue_input.py, which has the git fixtures):

  * the classification: a timeout, a reset, a DNS failure, 429, 5xx and
    GitHub's secondary rate limit are TRANSIENT (`ForgeUnavailable`); 404,
    410, 422, a plain 401/403 and a malformed reference are PERMANENT;
  * the bounded in-process retry: a timeout then a success is retried; a
    `Retry-After` is the wait; a wait past the budget is not slept at all;
    each retry is logged once with its attempt number and no query string;
  * the issue fetch, the pinned client's reads (merge, post-verdict) and the
    tenant client's pull request all use it.

MUTATIONS: make `transient_status` answer False for 403 -- the secondary
rate-limit cases fail. Drop the `retry-after` read -- the 429 case sees the
backoff instead of 7. Make `_open` raise `IssueUnavailable` for a timeout --
the issue classification cases fail. Drop `retry=` from the pinned client in
merge/post_verdict -- their one-blip cases end retryable instead of
SUCCEEDED.
"""

from __future__ import annotations

import io
import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

import pytest

from agent_worker import forge as forge_mod
from agent_worker import issue as issue_mod
from agent_worker import merge, post_verdict
from agent_worker.errors import InputUnavailable
from agent_worker.logs import build_logger
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from action_chain import PR, REVIEW_APP_ID, Chain, IDS
from fake_github import FakeGitHub, fresh_token

REPO = "https://github.com/octo/widgets.git"
SECONDARY = "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."


class _Clock:
    """A monotonic clock that only moves when the retry sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    info = error = warning


def _policy(clock: _Clock, log: Any = None, *, attempts: int = 4, budget: float = 45.0):
    return forge_mod.RetryPolicy(
        attempts=attempts, budget_seconds=budget, sleep=clock.sleep, clock=clock, log=log,
    )


# ---------------------------------------------------------------------------
# the classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "data"),
    [
        (429, {}, {"message": "slow down"}),
        (500, {}, None),
        (502, {}, {"message": "Bad Gateway"}),
        (503, {}, None),
        (504, {}, None),
        (403, {"Retry-After": "60"}, {"message": "forbidden"}),
        (403, {}, {"message": SECONDARY}),
        (403, {"X-RateLimit-Remaining": "0"}, {"message": "API rate limit exceeded"}),
    ],
    ids=["429", "500", "502", "503", "504", "403-retry-after", "403-secondary", "403-primary"],
)
def test_an_outage_or_a_rate_limit_is_transient(status, headers, data):
    assert forge_mod.transient_status(status, headers, data) is True


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (401, {"message": "Bad credentials"}),
        (403, {"message": "Resource not accessible by integration"}),
        (404, {"message": "Not Found"}),
        (410, {"message": "Issues are disabled for this repo"}),
        (422, {"message": "Validation Failed"}),
        (200, {}),
    ],
    ids=["401", "403-plain", "404", "410", "422", "200"],
)
def test_an_answer_is_permanent(status, data):
    assert forge_mod.transient_status(status, {}, data) is False


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(socket.timeout("timed out")),
        urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
        urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")),
        TimeoutError("The read operation timed out"),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
    ids=["connect-timeout", "socket-timeout", "reset", "dns", "read-timeout", "bare-reset"],
)
def test_a_network_failure_is_transient(exc):
    assert forge_mod.transient_network_error(exc) is True


def test_a_certificate_the_host_cannot_prove_is_not_retried():
    exc = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    assert forge_mod.transient_network_error(exc) is False


# ---------------------------------------------------------------------------
# the bounded in-process retry
# ---------------------------------------------------------------------------


def _flaky(failures: list[BaseException], value: Any = "ok"):
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if failures:
            raise failures.pop(0)
        return value

    return call, calls


def test_a_timeout_then_a_success_is_retried_in_process_and_logged_once():
    clock, log = _Clock(), _Log()
    call, calls = _flaky([forge_mod.ForgeUnavailable("could not reach api.github.com: timed out")])

    assert forge_mod.retry_transient(call, policy=_policy(clock, log), what="issue #72") == "ok"

    assert calls["n"] == 2
    assert clock.slept == [forge_mod.BACKOFF_BASE_SECONDS]
    (line,) = log.lines
    assert line[1]["attempt"] == 1 and line[1]["what"] == "issue #72"


def test_persistent_timeouts_end_transient_after_the_bounded_attempts():
    clock, log = _Clock(), _Log()
    failures = [forge_mod.ForgeUnavailable("could not reach api.github.com: timed out")
                for _ in range(10)]
    call, calls = _flaky(failures)

    with pytest.raises(forge_mod.ForgeUnavailable) as raised:
        forge_mod.retry_transient(call, policy=_policy(clock, log, attempts=4), what="x")

    assert calls["n"] == 4
    assert raised.value.tries == 4
    assert [fields["attempt"] for _m, fields in log.lines] == [1, 2, 3]
    assert sum(clock.slept) <= 45


def test_a_retry_after_is_the_wait():
    clock = _Clock()
    call, _ = _flaky([forge_mod.ForgeUnavailable("429", retry_after_seconds=7)])
    forge_mod.retry_transient(call, policy=_policy(clock), what="x")
    assert clock.slept == [7]


def test_a_retry_after_past_the_budget_is_not_slept_in_the_worker():
    """Invariant 4: a long wait is the scheduler's, not this process's."""
    clock = _Clock()
    call, calls = _flaky([forge_mod.ForgeUnavailable("429", retry_after_seconds=600)])
    with pytest.raises(forge_mod.ForgeUnavailable) as raised:
        forge_mod.retry_transient(call, policy=_policy(clock), what="x")
    assert clock.slept == [] and calls["n"] == 1
    assert raised.value.retry_after_seconds == 600


def test_the_budget_counts_the_time_the_failed_calls_took():
    """Each try can spend a whole request timeout; the bound is wall time."""
    clock = _Clock()

    def slow_failure():
        clock.now += 30  # the request timed out after 30 s
        raise forge_mod.ForgeUnavailable("timed out")

    with pytest.raises(forge_mod.ForgeUnavailable):
        forge_mod.retry_transient(slow_failure, policy=_policy(clock, budget=45), what="x")
    # 30 s gone; one 2 s backoff fits (32), the next try ends at 62 and the
    # 4 s after it would pass 45, so it stops there.
    assert clock.slept == [forge_mod.BACKOFF_BASE_SECONDS]


def test_a_permanent_answer_is_never_retried():
    clock = _Clock()
    call, calls = _flaky([issue_mod.IssueUnavailable("could not fetch issue #1: not found (404)")])
    with pytest.raises(issue_mod.IssueUnavailable):
        forge_mod.retry_transient(call, policy=_policy(clock), what="x")
    assert calls["n"] == 1 and clock.slept == []


def test_the_retry_log_line_carries_no_query_string_and_no_token():
    stream = io.StringIO()
    logger = build_logger(task_id="t", attempt_id="a", tenant_id="eng", generation=1,
                          runner_profile="mock", stream=stream)
    token = fresh_token()
    logger.register_secret(token)
    clock = _Clock()
    call, _ = _flaky([forge_mod.ForgeUnavailable(
        f"the forge answered 503 to /repos/o/r/pulls?head=o:swarm/x&access_token={token}")])

    forge_mod.retry_transient(call, policy=_policy(clock, logger), what="the pull request")

    text = stream.getvalue()
    assert '"attempt": 1' in text or "'attempt': 1" in text, text
    assert token not in text
    assert "?head=" not in text and "access_token" not in text


def test_the_bound_is_the_smaller_of_the_platform_wait_and_the_deadline():
    policy = forge_mod.RetryPolicy.bounded(
        attempts=4, max_in_worker_retry_delay_seconds=45, remaining_seconds=12.5,
    )
    assert policy.budget_seconds == 12.5
    policy = forge_mod.RetryPolicy.bounded(
        attempts=4, max_in_worker_retry_delay_seconds=45, remaining_seconds=3600,
    )
    assert policy.budget_seconds == 45


# ---------------------------------------------------------------------------
# the issue fetch
# ---------------------------------------------------------------------------


class _RaisingOpener:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def open(self, req, timeout=None):  # noqa: ANN001
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")),
        ConnectionResetError(104, "Connection reset by peer"),
        TimeoutError("The read operation timed out"),
    ],
    ids=["timeout", "dns", "reset", "read-timeout"],
)
def test_an_issue_fetch_that_cannot_reach_the_forge_is_transient_not_an_unavailable_input(
    monkeypatch, exc
):
    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", _RaisingOpener(exc))
    with pytest.raises(forge_mod.ForgeUnavailable) as raised:
        issue_mod.fetch_issue(repository_url=REPO, number=72, token=None)
    assert not isinstance(raised.value, InputUnavailable)
    assert "api.github.com" in str(raised.value)


class _Answer(io.BytesIO):
    status = 200
    headers: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class _AnsweringOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def open(self, req, timeout=None):  # noqa: ANN001
        return _Answer(self.body)


def test_a_success_whose_body_is_not_json_is_transient_not_an_unavailable_input(monkeypatch):
    """A proxy's page or a body cut short is not GitHub saying the issue is
    missing: retried, then the attempt fails retryably."""
    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", _AnsweringOpener(b"<html>Bad gateway</html>"))
    with pytest.raises(issue_mod.IssueUnreachable) as raised:
        issue_mod.fetch_issue(repository_url=REPO, number=72, token=None)
    assert not isinstance(raised.value, InputUnavailable)
    assert "not JSON" in str(raised.value)


def _answering(monkeypatch, status: int, data: Any, headers: dict[str, str] | None = None):
    monkeypatch.setattr(issue_mod, "_open", lambda req: (status, data, headers or {}))


@pytest.mark.parametrize(
    ("status", "data", "headers"),
    [
        (503, {"message": "unavailable"}, {}),
        (502, None, {}),
        (429, {"message": "slow down"}, {"Retry-After": "7"}),
        (403, {"message": SECONDARY}, {}),
    ],
    ids=["503", "502", "429", "403-secondary"],
)
def test_an_issue_fetch_answered_with_an_outage_is_transient(monkeypatch, status, data, headers):
    _answering(monkeypatch, status, data, headers)
    with pytest.raises(forge_mod.ForgeUnavailable) as raised:
        issue_mod.fetch_issue(repository_url=REPO, number=72, token=None)
    assert not isinstance(raised.value, InputUnavailable)
    if "Retry-After" in headers:
        assert raised.value.retry_after_seconds == 7


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (404, {"message": "Not Found"}),
        (403, {"message": "Resource not accessible by integration"}),
        (401, {"message": "Bad credentials"}),
        (410, {"message": "Gone"}),
        (422, {"message": "Validation Failed"}),
    ],
    ids=["404", "403-plain", "401", "410", "422"],
)
def test_an_issue_fetch_answered_no_is_an_unavailable_input(monkeypatch, status, data):
    _answering(monkeypatch, status, data)
    with pytest.raises(issue_mod.IssueUnavailable) as raised:
        issue_mod.fetch_issue(repository_url=REPO, number=72, token=None)
    assert not isinstance(raised.value, forge_mod.ForgeUnavailable)
    assert f"({status}" in str(raised.value)


def test_a_malformed_reference_is_an_unavailable_input():
    with pytest.raises(issue_mod.IssueUnavailable):
        issue_mod.fetch_issue(repository_url="not a repository", number=72, token=None)


def _issue(number: int = 72) -> issue_mod.Issue:
    return issue_mod.Issue(repository="octo/widgets", number=number, title="t", state="open",
                           author="a", created_at="", url="", body="b")


def _stage(tmp_path, monkeypatch, outcomes: list[Any], *, policy):
    calls = {"n": 0}

    def fetch(*, repository_url, number, token, on_request=None):
        calls["n"] += 1
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    path = issue_mod.stage_issue(
        number=72, repository_url=REPO, token=None, refusal=None, work_dir=tmp_path,
        scrub=lambda text: text, logger=_Log(), retry=policy,
    )
    return path, calls


def test_stage_issue_retries_a_timeout_then_writes_the_issue(tmp_path, monkeypatch):
    clock, log = _Clock(), _Log()
    path, calls = _stage(
        tmp_path, monkeypatch,
        [issue_mod.IssueUnreachable("could not reach api.github.com: timed out"), _issue()],
        policy=_policy(clock, log),
    )
    assert calls["n"] == 2
    assert path.is_file() and "# Issue #72" in path.read_text()
    assert [fields["attempt"] for _m, fields in log.lines] == [1]


def test_stage_issue_waits_out_a_429s_retry_after(tmp_path, monkeypatch):
    clock = _Clock()
    _stage(
        tmp_path, monkeypatch,
        [issue_mod.IssueUnreachable("the forge answered 429", retry_after_seconds=7), _issue()],
        policy=_policy(clock),
    )
    assert clock.slept == [7]


def test_stage_issue_does_not_retry_a_404(tmp_path, monkeypatch):
    clock = _Clock()
    with pytest.raises(issue_mod.IssueUnavailable):
        _stage(
            tmp_path, monkeypatch,
            [issue_mod.IssueUnavailable("could not fetch issue #72 of octo/widgets: (404)"),
             _issue()],
            policy=_policy(clock),
        )
    assert clock.slept == []


def test_stage_issue_ends_transient_when_the_forge_stays_down(tmp_path, monkeypatch):
    clock = _Clock()
    down = [issue_mod.IssueUnreachable("could not reach api.github.com: timed out")
            for _ in range(4)]
    with pytest.raises(forge_mod.ForgeUnavailable) as raised:
        _stage(tmp_path, monkeypatch, down, policy=_policy(clock, attempts=4))
    assert not isinstance(raised.value, InputUnavailable)
    assert "issue #72" in str(raised.value) and "octo/widgets" in str(raised.value)
    assert not (tmp_path / issue_mod.FILE_NAME).exists()


# ---------------------------------------------------------------------------
# the tenant client: probe and pull request
# ---------------------------------------------------------------------------


def test_the_tenant_client_turns_a_read_timeout_into_an_outage(monkeypatch):
    class TimedOut:
        def open(self, req, timeout=None):  # noqa: ANN001
            raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", TimedOut())
    with pytest.raises(forge_mod.ForgeUnavailable):
        forge_mod.probe_repository(url=REPO, token=fresh_token())


def test_the_tenant_client_reads_a_secondary_rate_limit_as_an_outage(monkeypatch):
    class Limited:
        def open(self, req, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {"Retry-After": "30"},
                io.BytesIO(json.dumps({"message": SECONDARY}).encode()),
            )

    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", Limited())
    with pytest.raises(forge_mod.ForgeUnavailable) as raised:
        forge_mod.probe_repository(url=REPO, token=fresh_token())
    assert raised.value.retry_after_seconds == 30


def test_a_plain_403_on_the_probe_is_still_an_answer(monkeypatch):
    monkeypatch.setattr(forge_mod, "_request",
                        lambda url, *, token, method="GET", payload=None:
                        (403, {"message": "Resource not accessible by integration"}))
    access = forge_mod.probe_repository(url=REPO, token=fresh_token())
    assert access is not None and access.can_push is False


def test_a_probe_answered_503_is_an_outage(monkeypatch):
    monkeypatch.setattr(forge_mod, "_request",
                        lambda url, *, token, method="GET", payload=None: (503, None))
    with pytest.raises(forge_mod.ForgeUnavailable):
        forge_mod.probe_repository(url=REPO, token=fresh_token())


# ---------------------------------------------------------------------------
# the pinned client: merge and post-verdict reads
# ---------------------------------------------------------------------------


def _blip_once(chain: Any, path: str, answer: tuple[int, dict[str, str], Any]) -> list[int]:
    """`path` answers `answer` once, then what it answered before."""
    original = chain.github.routes[("GET", path)]
    seen: list[int] = []

    def route(request):
        seen.append(1)
        if len(seen) == 1:
            return answer
        return original(request) if callable(original) else original

    chain.github.route("GET", path, route)
    return seen


def test_the_pinned_client_retries_a_get_and_never_a_post():
    clock = _Clock()
    fake = FakeGitHub()
    answers = [(503, {}, {}), (200, {}, {"ok": True})]
    fake.route("GET", "/repos/acme/widgets", lambda _s: answers.pop(0))
    fake.route("POST", "/repos/acme/widgets/issues/1/comments", (502, {}, {}))
    client = forge_mod.PinnedForgeClient(token=fresh_token(), transport=fake,
                                         retry=_policy(clock))

    assert client.get_ok("/repos/acme/widgets") == {"ok": True}
    with pytest.raises(forge_mod.ForgeUnavailable):
        client.request("POST", "/repos/acme/widgets/issues/1/comments", payload={"body": "x"})
    assert len(fake.calls("POST", "/repos/acme/widgets/issues/1/comments")) == 1


def test_the_pinned_client_reads_a_secondary_rate_limit_as_an_outage():
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets", (403, {}, {"message": SECONDARY}))
    with pytest.raises(forge_mod.ForgeUnavailable):
        forge_mod.PinnedForgeClient(token=fresh_token(), transport=fake).get("/repos/acme/widgets")


def test_the_pinned_client_reads_a_plain_403_as_an_answer():
    fake = FakeGitHub()
    fake.route("GET", "/repos/acme/widgets", (403, {}, {"message": "Must have admin rights"}))
    response = forge_mod.PinnedForgeClient(token=fresh_token(), transport=fake).get(
        "/repos/acme/widgets")
    assert response.status == 403


def test_a_merge_whose_read_blips_once_still_merges(tmp_path):
    from merge_world import PR as MERGE_PR, MergeWorld

    world = MergeWorld(tmp_path)
    seen = _blip_once(world, MERGE_PR, (502, {}, {"message": "Bad Gateway"}))
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(seen) >= 2
    assert forge_mod.BACKOFF_BASE_SECONDS in world.slept


def test_a_merge_whose_read_hits_a_secondary_rate_limit_ends_retryable(tmp_path):
    from merge_world import API, PINNED, MergeWorld

    world = MergeWorld(tmp_path)
    world.github.route("GET", f"{API}/commits/{PINNED}/status",
                       (403, {"Retry-After": "600"}, {"message": SECONDARY}))
    outcome = merge.run_merge(world.context())
    assert outcome.retryable is True
    assert outcome.end_cause is EndCause.MERGE_FAILED
    assert outcome.retry_delay_seconds == 600
    assert world.merge_calls() == []


def test_a_post_verdict_whose_pull_request_read_blips_once_still_posts(tmp_path):
    chain = Chain(tmp_path)
    chain.app_id = REVIEW_APP_ID
    seen = _blip_once(chain, f"{PR}", (504, {}, {}))
    dispatch = {"strategy": "single-pr", "pr_role": "none",
                "verdict_source": {"review": IDS["review"]}}
    outcome = post_verdict.run_post_verdict(chain.context(dispatch=dispatch))
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(seen) == 2
    assert chain.github.calls("POST", f"{PR}/reviews")


def test_an_outage_looking_up_the_existing_pull_request_is_not_a_refusal(monkeypatch):
    """A 422 sends `open_pull_request` to look for the open one; a 503 on that
    lookup is an outage, not "none exists" -- which would read as the forge
    refusing the pull request and fail the step for good."""
    def request(url, *, token, method="GET", payload=None):
        if method == "POST":
            return 422, {"message": "Validation Failed",
                         "errors": [{"message": "A pull request already exists"}]}
        return 503, {"message": "unavailable"}

    monkeypatch.setattr(forge_mod, "_request", request)
    access = forge_mod.RepoAccess(ref=forge_mod.parse_repo(REPO), default_branch="main",
                                  can_push=True, reason="")
    with pytest.raises(forge_mod.ForgeUnavailable):
        forge_mod.open_pull_request(access=access, token=fresh_token(), head="swarm/t",
                                    base="main", title="t", body="b")


def test_an_outage_opening_the_pull_request_is_retried_and_then_adopts(monkeypatch):
    """The POST timed out after GitHub made it: the retry's 422 adopts it."""
    posts: list[int] = []

    def request(url, *, token, method="GET", payload=None):
        if method == "POST":
            posts.append(1)
            if len(posts) == 1:
                raise forge_mod.ForgeUnavailable("could not reach api.github.com: timed out")
            return 422, {"message": "Validation Failed"}
        return 200, [{"number": 5, "html_url": "https://github.com/octo/widgets/pull/5",
                      "state": "open", "title": "t"}]

    monkeypatch.setattr(forge_mod, "_request", request)
    access = forge_mod.RepoAccess(ref=forge_mod.parse_repo(REPO), default_branch="main",
                                  can_push=True, reason="")
    clock = _Clock()
    pr = forge_mod.retry_transient(
        lambda: forge_mod.open_pull_request(access=access, token=fresh_token(),
                                            head="swarm/t", base="main", title="t", body="b"),
        policy=_policy(clock), what="the pull request",
    )
    assert pr.number == 5 and pr.created is False
    assert len(posts) == 2 and len(clock.slept) == 1
