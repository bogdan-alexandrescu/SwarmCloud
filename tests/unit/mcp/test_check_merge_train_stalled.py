"""swarm_check flags a merge step that waits on the merge train over 60 minutes (part of #2688).

On 2026-11-04 the workflow page redesign's pull request was labelled ready
at 13:57Z; at 14:00Z its `queue for auto-merge` check failed on a network
timeout. The train boards only pull requests whose checks are all green, so
it passed over this one on every tick for eight hours, and `/sc check` never
flagged the merge step: a train park (CI_PENDING, code
`merge_train_waiting`) stops the CI clock by design, so rule (c) never aged
it. Rule `merge_train_stalled` now ages the train wait from the first train
park of its unbroken run, across event pages, flags it past 60 minutes with
the pull request's failed or cancelled checks at its head (GitHub's public
API), and points `next` at the pull request.

No credentials, no network: GitHub's read is a fake.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_mcp import check
from swarm_mcp.client import SwarmError

NOW = datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc)
HEAD = "9c1e0f4b2d7a83e65f10c2b9a4d8e7f6031b5a2c"
PR = 3190
REPO = "https://github.com/example-org/example-repo.git"
#: The link the bridge builds (`check.pull_request_link`: its key is lowercased).
PR_URL = f"https://github.com/example-org/example-repo/pull/{PR}"
#: The events one re-check writes after its park, newest first: the lease is
#: released, the step promoted, leased, dispatched and run again.
CYCLE = ("lease_released", "ready", "leased", "starting", "running")


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _park(at: datetime, code: str) -> dict[str, Any]:
    return {"type": "parked", "at": _iso(at),
            "detail": {"reason": "CI_PENDING", "code": code, "head": HEAD, "pull_request": PR}}


def _history(first_train_park: datetime, re_parks: int) -> list[dict[str, Any]]:
    """The whole history, newest first: a CI park that ended when the checks
    went green, then the first train park and `re_parks` re-parks, spread
    evenly up to a minute ago, each re-park after a full re-check cycle."""
    every = (NOW - timedelta(minutes=1) - first_train_park) / re_parks
    events: list[dict[str, Any]] = []
    for i in range(re_parks, -1, -1):
        at = first_train_park + i * every
        events.append(_park(at, check.TRAIN_WAITING))
        for j, kind in enumerate(CYCLE):
            events.append({"type": kind, "at": _iso(at - timedelta(seconds=10 * (j + 1)))})
    ci = first_train_park - timedelta(minutes=5)
    events.append(_park(ci, "checks_pending"))
    events.append({"type": "running", "at": _iso(ci - timedelta(minutes=1))})
    events.append({"type": "created", "at": _iso(ci - timedelta(minutes=10))})
    return events


class _Reads:
    """The task's events, served a page of `check.EVENT_PAGE` at a time."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.pages = [events[i:i + check.EVENT_PAGE] for i in range(0, len(events), check.EVENT_PAGE)]
        self.served = 0

    def events(self, task_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        self.served = 1
        return self.pages[0]

    def older_events(self, task_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        page = self.pages[self.served] if self.served < len(self.pages) else []
        self.served += 1
        return page

    def task(self, task_id: str) -> None:  # noqa: ARG002
        return None

    def capacity(self) -> tuple[None, str]:
        return None, "not read in this test"


def _merge_step(first_train_park: datetime) -> dict[str, Any]:
    # The API's `waiting.since` is the CI clock, paused by the train: the
    # CI park's instant, never the train's.
    ci_clock = first_train_park - timedelta(minutes=5)
    return {"id": "task_8f2d41c07a6b4e1d9c33", "state": "PARKED", "park_reason": "CI_PENDING",
            "runner_profile": "merge", "repository_url": REPO,
            "created_at": _iso(ci_clock - timedelta(minutes=10)),
            "waiting": {"reason": "CI_PENDING", "since": _iso(ci_clock),
                        "summary": f"CI on PR #{PR}: 0 checks pending",
                        "detail": {"code": check.TRAIN_WAITING, "head": HEAD, "pending": [],
                                   "pull_request": {"number": PR, "url": PR_URL}}}}


class _Checks:
    def __init__(self, answer: list[dict[str, Any]] | Exception) -> None:
        self.answer = answer
        self.asked: list[tuple[str, int, str | None]] = []

    def __call__(self, key: str, number: int, head: str | None) -> list[dict[str, Any]]:
        self.asked.append((key, number, head))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _classify(first: datetime, re_parks: int, checks: _Checks) -> dict[str, Any] | None:
    reads = _Reads(_history(first, re_parks))
    return check.classify_task(_merge_step(first), reads, now=NOW, window=20,
                               checks=check._PullChecks(checks))


def test_a_train_park_of_61_minutes_across_five_re_parks_is_flagged_from_its_start():
    first = NOW - timedelta(minutes=61)
    checks = _Checks([])

    flag = _classify(first, 5, checks)

    # MUTATION (check.py `_parked`): no train rule -> rule (c) ages the CI
    # wait, under 90 minutes, and nothing is flagged (None).
    assert flag is not None and flag["rule"] == "merge_train_stalled", flag
    assert flag["kind"] == check.STUCK
    evidence = flag["evidence"]
    # MUTATION (check.py `_train_entered`): read only the newest page -> a
    # lower bound under 60 minutes, not flagged.
    assert evidence["in_state_since"] == _iso(first), evidence
    assert evidence["age_minutes"] == 61 and evidence["age_is"] == "exact", evidence
    assert evidence["window_minutes"] == check.MERGE_TRAIN_MINUTES == 60
    assert evidence["pull_request_url"] == PR_URL and evidence["head"] == HEAD
    assert evidence["failed_checks"] == []
    assert flag["next"] == PR_URL
    assert checks.asked == [("github.com/example-org/example-repo", PR, HEAD)]
    assert "merge train for exact 61 min" in flag["reason"], flag["reason"]


def test_a_train_park_of_59_minutes_is_not_flagged():
    checks = _Checks([])

    flag = _classify(NOW - timedelta(minutes=59), 5, checks)

    assert flag is None, flag
    assert checks.asked == [], "an unflagged train park reads nothing from GitHub"


def test_a_red_check_at_its_head_is_named():
    red = [{"name": "queue for auto-merge", "conclusion": "failure"},
           {"name": "ci-gate", "conclusion": "cancelled"}]

    flag = _classify(NOW - timedelta(hours=8), 30, _Checks(red))

    assert flag is not None and flag["rule"] == "merge_train_stalled", flag
    assert flag["evidence"]["failed_checks"] == red
    assert "queue for auto-merge (failure)" in flag["reason"], flag["reason"]
    assert "ci-gate (cancelled)" in flag["reason"]
    # Eight hours of re-parks span more pages than one check reads: a lower bound.
    assert flag["evidence"]["age_is"] == "at least"
    assert flag["evidence"]["age_minutes"] > 60


def test_an_unreadable_pull_request_says_so():
    flag = _classify(NOW - timedelta(minutes=75), 6,
                     _Checks(SwarmError("GitHub could not be read: TimeoutError")))

    assert flag is not None and flag["rule"] == "merge_train_stalled", flag
    evidence = flag["evidence"]
    assert evidence["failed_checks"] is None
    assert "checks not read: GitHub could not be read: TimeoutError" in evidence["checks_unread_because"]
    assert "checks not read" in flag["reason"]
    assert flag["next"] == PR_URL


def test_train_parks_do_not_age_a_later_ci_wait():
    """A CI park after four hours on the train (the train returned the
    entry and its gate re-ran): the CI rule ages only the CI parks."""
    events = [_park(NOW - timedelta(minutes=2), "checks_pending"),
              {"type": "running", "at": _iso(NOW - timedelta(minutes=3))}]
    events += _history(NOW - timedelta(hours=4), 4)[:check.EVENT_PAGE - 2]
    task = {**_merge_step(NOW - timedelta(hours=4)), "waiting": None}

    flag = check.classify_task(task, _Reads(events), now=NOW, window=20,
                               checks=check._PullChecks(_Checks([])))

    # MUTATION (check.py `_ci_entered`): count a train park as a CI park ->
    # a CI wait of at least 120 minutes, flagged `parked`.
    assert flag is None, flag


class _Client:
    """The events route: two pages of a task's history, then its end."""

    def __init__(self, tokens: bool = True) -> None:
        self.tokens = tokens
        self.paths: list[str] = []

    def request(self, method: str, path: str) -> dict[str, Any]:
        self.paths.append(path)
        older = "page_token=" in path
        page = {"events": [{"type": "parked", "at": _iso(NOW)}] * (1 if older else check.EVENT_PAGE)}
        if self.tokens:
            page["next_page_token"] = None if older else "tok-1"
        return page


def test_the_check_reads_older_event_pages_by_their_token_until_the_history_ends():
    client = _Client()
    reads = check._Reads(client, budget=10)

    assert len(reads.events("task_a")) == check.EVENT_PAGE
    # MUTATION (check.py `_Reads.older_events`): no paging -> None.
    assert reads.older_events("task_a") == [{"type": "parked", "at": _iso(NOW)}]
    assert "page_token=tok-1" in client.paths[-1] and "order=desc" in client.paths[-1]
    assert reads.older_events("task_a") == [], "the history ended: no further read"
    assert len(client.paths) == 2


def test_an_api_that_serves_no_page_token_reads_no_older_page():
    client = _Client(tokens=False)
    reads = check._Reads(client, budget=10)
    reads.events("task_a")

    assert reads.older_events("task_a") is None
    assert len(client.paths) == 1


class _PagedClient:
    """The events route over a whole history, by page token."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.pages = [events[i:i + check.EVENT_PAGE] for i in range(0, len(events), check.EVENT_PAGE)]

    def request(self, method: str, path: str) -> dict[str, Any]:
        query = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
        index = int(query.get("page_token", "p0")[1:])
        more = index + 1 < len(self.pages)
        return {"events": self.pages[index], "next_page_token": f"p{index + 1}" if more else None}


def _board_waits(minutes: int) -> list[dict[str, Any]]:
    from swarm_mcp import buildboard

    first = NOW - timedelta(minutes=minutes)
    reader = buildboard._Reader(_PagedClient(_history(first, 5)), {"tasks": {}}, NOW)
    step = {"step_id": "merge", "runner_profile": "merge"}
    lane = {"id": "wf_3190", "label": "the workflow page redesign"}
    task = {**_merge_step(first), "result_summary": {"pull_request": PR}}
    return buildboard._step_waits(reader, lane, [step], step, task)


def test_the_build_board_lists_a_train_wait_over_60_minutes_from_its_start():
    waits = _board_waits(61)

    # MUTATION (buildboard.py `_step_waits`): no train branch -> the CI rule
    # reads the newest park, a minute old, and lists nothing.
    assert [w["kind"] for w in waits] == ["merge_train_stalled"], waits
    assert waits[0]["since"] == _iso(NOW - timedelta(minutes=61))
    assert waits[0]["next"] == PR_URL
    assert f"PR #{PR} has waited on the merge train for 61 min" == waits[0]["item"]


def test_the_build_board_does_not_list_a_train_wait_under_60_minutes():
    assert _board_waits(59) == []


# ---------------------------------------------------------------------------
# The API's train start (`waiting.detail.train_since`, part of #2688)
# ---------------------------------------------------------------------------


def _served(task: dict[str, Any], entered: datetime) -> dict[str, Any]:
    waiting = task["waiting"]
    return {**task, "waiting": {**waiting, "detail": {**waiting["detail"], "train_since": _iso(entered)}}}


def test_the_served_train_start_ages_the_wait_exactly_without_older_pages():
    # The events alone would age it 61 min; the API says the train began
    # 30 min ago (the earlier parks were another run), and it is believed.
    first = NOW - timedelta(minutes=61)
    reads = _Reads(_history(first, 5))
    task = _served(_merge_step(first), NOW - timedelta(minutes=30))

    flag = check.classify_task(task, reads, now=NOW, window=20, checks=check._PullChecks(_Checks([])))

    # MUTATION (check.py `_train_entered`): ignore `train_since` -> the
    # events' 61 min, flagged.
    assert flag is None, flag
    assert reads.served == 1, "no older page read once the API served the start"


def test_the_served_train_start_flags_a_wait_over_60_minutes_as_exact():
    first = NOW - timedelta(minutes=10)
    task = _served(_merge_step(first), NOW - timedelta(hours=8))

    flag = check.classify_task(task, _Reads(_history(first, 2)), now=NOW, window=20,
                               checks=check._PullChecks(_Checks([])))

    assert flag is not None and flag["rule"] == "merge_train_stalled", flag
    evidence = flag["evidence"]
    assert evidence["in_state_since"] == _iso(NOW - timedelta(hours=8))
    assert evidence["age_minutes"] == 480 and evidence["age_is"] == "exact"
    assert "train_since" in evidence["age_source"]


class _TaskRoute(_Reads):
    """A listing's row: `waiting` without its detail; the task route serves it."""

    def __init__(self, events: list[dict[str, Any]], full: dict[str, Any]) -> None:
        super().__init__(events)
        self.full = full
        self.tasks = 0

    def task(self, task_id: str) -> dict[str, Any]:  # type: ignore[override]  # noqa: ARG002
        self.tasks += 1
        return self.full


def test_a_listing_row_reads_the_task_route_for_the_train_start():
    first = NOW - timedelta(minutes=10)
    full = _served(_merge_step(first), NOW - timedelta(hours=2))
    row = {**full, "waiting": {"reason": "CI_PENDING", "summary": full["waiting"]["summary"]},
           "blocked_by": [{"reason": "CI_PENDING", "code": check.TRAIN_WAITING}]}
    reads = _TaskRoute(_history(first, 2), full)

    flag = check.classify_task(row, reads, now=NOW, window=20, checks=check._PullChecks(_Checks([])))

    # MUTATION (check.py `_train_wait`): no task read -> the events' 10 min,
    # not flagged.
    assert flag is not None and flag["evidence"]["age_minutes"] == 120, flag
    assert reads.tasks == 1


def test_the_build_board_ages_a_train_wait_from_the_served_start():
    from swarm_mcp import buildboard

    first = NOW - timedelta(minutes=10)
    reader = buildboard._Reader(_PagedClient(_history(first, 2)), {"tasks": {}}, NOW)
    step = {"step_id": "merge", "runner_profile": "merge"}
    lane = {"id": "wf_3190", "label": "the workflow page redesign"}
    task = {**_served(_merge_step(first), NOW - timedelta(minutes=90)), "result_summary": {"pull_request": PR}}

    waits = buildboard._step_waits(reader, lane, [step], step, task)

    # MUTATION (buildboard.py `train_wait`): the events only -> 10 min, nothing listed.
    assert [w["kind"] for w in waits] == ["merge_train_stalled"], waits
    assert waits[0]["since"] == _iso(NOW - timedelta(minutes=90))
