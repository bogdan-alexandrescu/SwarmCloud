"""A ready pull request boards the merge train on time, whatever GitHub's schedule does.

THE LIVE FINDING (12 November 2026, the owner's pick "Fix: board on time"):
a lane's pull request was green and `ready` at 02:56Z, and the gather ("6
ready or 20 minutes") said it boards at the latest at 03:15:35Z. The train
runs only when merge-train.yml is triggered, and from 03:10Z to 07:57Z
GitHub ran almost none of its scheduled ticks and no other workflow
finished, so nothing boarded it until 07:59Z.

What these tests hold, on swarm-api's merge wake tick (`swarm_api.trainboard`):

  1. A lone ready pull request past its gathering deadline, with no
     merge-train run, gets exactly one `workflow_dispatch` of merge-train.yml
     on the default branch; before the deadline, none.
  2. With a merge-train run queued or in progress, none.
  3. Two ticks in the same window dispatch once (the claim is one
     transaction on one document per repository); the next window may
     dispatch again.
  4. A refused dispatch writes one line naming the reason and backs off,
     and the tenant's other parks are still read; a rate limit holds the
     tick as its other refusals do.
  5. The train off, a pull request no longer `ready`, or a workspace train's
     park dispatches nothing.
  6. The window comes from the one shared source scripts/merge-train.sh
     reads too, or from the repository variable merge-train.yml hands it.
  7. Nothing is admitted, reserved or moved: the park stays PARKED.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from swarm_api import forge_limits, mergewake, trainboard
from swarm_api.validation import IssueRef

from .test_merge_wake import (  # noqa: F401 - the fixtures are used by name
    CHECK, HEAD, NOW, NUMBER, QueueGitHub, _tick, _wait, clock, parked_merge, tenant_tokens,
    wake_client,
)

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
SWITCH = "MERGE_TRAIN"
DISPATCHES = "/repos/example-org/widgets/actions/workflows/merge-train.yml/dispatches"
WINDOW = trainboard.gather_defaults().max_wait_minutes


class TrainGitHub(QueueGitHub):
    """The wake tick's fake GitHub, plus what boarding on time reads and
    writes: each pull request's issue events, merge-train.yml's runs, and its
    dispatches (answered `dispatch_answer`)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: dict[int, list[dict]] = {}
        self.train_runs: list[dict] = []
        self.dispatch_answer = 204
        self.dispatched: list[dict] = []

    def __call__(self, method, url, headers, body, timeout):
        path = urlparse(url).path
        if (method, path) in self.refuse:
            return super().__call__(method, url, headers, body, timeout)
        rest = path.strip("/").split("/")[3:]
        if rest[:1] == ["issues"] and len(rest) == 3 and rest[2] == "events" and method == "GET":
            self.calls.append((method, url, dict(headers), body))
            return 200, json.dumps(self.events.get(int(rest[1]), [])).encode()
        if rest == ["actions", "workflows", "merge-train.yml", "runs"] and method == "GET":
            self.calls.append((method, url, dict(headers), body))
            return 200, json.dumps({"total_count": len(self.train_runs),
                                    "workflow_runs": self.train_runs}).encode()
        if path == DISPATCHES and method == "POST":
            self.calls.append((method, url, dict(headers), body))
            if self.dispatch_answer != 204:
                return self.dispatch_answer, json.dumps({"message": "refused"}).encode()
            self.dispatched.append(json.loads(body.decode()))
            return 204, b""
        return super().__call__(method, url, headers, body, timeout)

    def ready_at(self, number: int, at: datetime, *, removed: datetime | None = None) -> None:
        self.events[number] = [{"event": "labeled", "label": {"name": "ready"},
                                "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}]
        if removed is not None:
            self.events[number].append({"event": "unlabeled", "label": {"name": "ready"},
                                        "created_at": removed.strftime("%Y-%m-%dT%H:%M:%SZ")})

    def dispatch_calls(self) -> list[str]:
        return [url for method, url, _, _ in self.calls
                if method == "POST" and urlparse(url).path == DISPATCHES]


@pytest.fixture
def github() -> TrainGitHub:
    gh = TrainGitHub()
    gh.open_pull(NUMBER, HEAD, ref="swarm/task_int")
    gh.require(CHECK)
    gh.variables[SWITCH] = "on"
    return gh


def _train_park(db, task_id: str = "task_merge", **extra):
    return parked_merge(db, task_id, code=mergewake.MERGE_TRAIN_WAITING, pending=[], **extra)


def _past(github, number: int = NUMBER) -> None:
    """`ready` since a minute past the window: the deadline has passed."""
    github.ready_at(number, NOW - timedelta(minutes=WINDOW + 1))


# --------------------------------------------------------------------------
# 1. past the deadline, one dispatch; before it, none
# --------------------------------------------------------------------------

def test_a_lone_ready_pull_request_past_its_deadline_gets_exactly_one_dispatch(db, wake_client,
                                                                                github):
    _train_park(db)
    _past(github)

    body = _tick(wake_client).json()

    assert github.dispatched == [{"ref": "main"}], body
    assert body["report"]["train_boards"] == {"dispatched": 1, "refused": 0, "gathering": 0}
    # Nothing admitted, reserved or moved: still parked, unmarked (invariants 1, 3).
    task = db.docs["tasks/task_merge"]
    assert task["state"] == "PARKED"
    assert mergewake.WAKE_MARKER not in _wait(db)
    board = db.docs[f"merge_train_boards/{trainboard.board_id_for('eng', 'example-org/widgets')}"]
    assert board["tenant_id"] == "eng" and board["pull_request"] == NUMBER


def test_before_the_deadline_nothing_is_dispatched(db, wake_client, github):
    _train_park(db)
    github.ready_at(NUMBER, NOW - timedelta(minutes=WINDOW - 1))

    body = _tick(wake_client).json()

    assert github.dispatch_calls() == [], body
    assert body["report"]["train_boards"]["gathering"] == 1


def test_the_oldest_ready_pull_request_of_the_parks_sets_the_deadline(db, wake_client, github):
    _train_park(db, "task_young")
    _train_park(db, "task_old", pull_request=NUMBER + 1)
    github.ready_at(NUMBER, NOW - timedelta(minutes=1))
    _past(github, NUMBER + 1)

    _tick(wake_client)

    assert len(github.dispatched) == 1


# --------------------------------------------------------------------------
# 2. a train run queued or in progress
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_a_train_run_queued_or_in_progress_means_no_dispatch(db, wake_client, github, status):
    _train_park(db)
    _past(github)
    github.train_runs = [{"id": 7, "status": status}, {"id": 6, "status": "completed"}]

    _tick(wake_client)

    assert github.dispatch_calls() == []


def test_only_finished_train_runs_still_dispatch(db, wake_client, github):
    _train_park(db)
    _past(github)
    github.train_runs = [{"id": 6, "status": "completed", "conclusion": "success"}]

    _tick(wake_client)

    assert len(github.dispatched) == 1


# --------------------------------------------------------------------------
# 3. at most one dispatch per repository per window
# --------------------------------------------------------------------------

def test_two_ticks_in_the_same_window_dispatch_once(db, wake_client, github, clock):
    _train_park(db)
    _past(github)

    _tick(wake_client)
    clock.now = NOW + timedelta(minutes=1)
    _tick(wake_client)
    clock.now = NOW + timedelta(minutes=WINDOW - 1)
    _tick(wake_client)

    assert len(github.dispatched) == 1

    clock.now = NOW + timedelta(minutes=WINDOW)
    _tick(wake_client)
    assert len(github.dispatched) == 2


def test_two_replicas_claiming_the_same_window_claim_once(db):
    ref = IssueRef(owner="example-org", repo="widgets", number=NUMBER)
    since = NOW - timedelta(minutes=WINDOW + 1)

    first = trainboard._claim(db, "eng", ref, now=NOW, window=WINDOW, since=since, number=NUMBER)
    second = trainboard._claim(db, "eng", ref, now=NOW + timedelta(seconds=1), window=WINDOW,
                               since=since, number=NUMBER)

    assert (first, second) == (True, False)


def test_another_tenants_board_is_never_taken_for_this_ones(db):
    ref = IssueRef(owner="example-org", repo="widgets", number=NUMBER)
    since = NOW - timedelta(minutes=WINDOW + 1)
    assert trainboard.board_id_for("eng", ref.repository) != \
        trainboard.board_id_for("research", ref.repository)
    assert trainboard._claim(db, "eng", ref, now=NOW, window=WINDOW, since=since, number=NUMBER)
    assert trainboard._claim(db, "research", ref, now=NOW, window=WINDOW, since=since,
                             number=NUMBER)


# --------------------------------------------------------------------------
# 4. a refused dispatch: one line, back off, the other parks still read
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status, needs", [(403, "actions: write"), (404, "not found"),
                                           (422, "HTTP 422")])
def test_a_refused_dispatch_is_logged_backs_off_and_the_other_parks_are_read(
        db, wake_client, github, clock, caplog, status, needs):
    _train_park(db)
    _past(github)
    parked_merge(db, "task_ci", pull_request=NUMBER)
    github.check(HEAD, CHECK, "success")
    github.dispatch_answer = status

    with caplog.at_level(logging.WARNING, logger="swarm_api.trainboard"):
        body = _tick(wake_client).json()

    assert body["report"]["train_boards"]["refused"] == 1, body
    assert body["report"]["woken"] == 1, body
    assert _wait(db, "task_ci")[mergewake.WAKE_MARKER]
    lines = [r.getMessage() for r in caplog.records if r.name == "swarm_api.trainboard"]
    assert len(lines) == 1 and needs in lines[0], lines
    assert not any(c.isdigit() and len(c) > 30 for c in lines[0].split())

    clock.now = NOW + timedelta(minutes=1)
    _tick(wake_client)
    assert len(github.dispatch_calls()) == 1


def test_a_rate_limited_dispatch_holds_the_tick_and_asks_again_after(db, wake_client, github,
                                                                     clock):
    _train_park(db)
    _past(github)
    github.dispatch_answer = 429

    _tick(wake_client)
    held = forge_limits.QUOTAS.held(
        forge_limits.sweep_key(mergewake.MERGE_WAKE_SWEEP, "eng"), now=NOW)

    assert held is not None
    assert len(github.dispatch_calls()) == 1

    # The reset GitHub named has passed: the credential's own hold, which
    # the ledger keeps by the wall clock, is gone with the tick's.
    github.dispatch_answer = 204
    forge_limits.QUOTAS.clear()
    clock.now = held + timedelta(seconds=1)
    _tick(wake_client)
    assert github.dispatched == [{"ref": "main"}]


def test_an_unreachable_forge_is_asked_again_next_tick(db, wake_client, github, clock):
    _train_park(db)
    _past(github)
    github.dispatch_answer = 502

    _tick(wake_client)
    github.dispatch_answer = 204
    clock.now = NOW + timedelta(minutes=1)
    _tick(wake_client)

    assert github.dispatched == [{"ref": "main"}]


def test_a_read_that_fails_never_fails_the_tick(db, wake_client, github):
    _train_park(db)
    _past(github)
    github.refuse[("GET", "/repos/example-org/widgets/actions/workflows/merge-train.yml/runs")] \
        = 403
    parked_merge(db, "task_ci")
    github.check(HEAD, CHECK, "success")

    response = _tick(wake_client)

    assert response.status_code == 200, response.text
    assert response.json()["report"]["woken"] == 1
    assert github.dispatch_calls() == []


# --------------------------------------------------------------------------
# 5. nothing to dispatch for
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "off"])
def test_with_the_train_off_nothing_is_dispatched(db, wake_client, github, value):
    """Off, a dispatch would start a DRY RUN train (merge-train.yml, "on demand")."""
    _train_park(db)
    _past(github)
    github.variables.pop(SWITCH)
    if value is not None:
        github.variables[SWITCH] = value

    _tick(wake_client)

    assert github.dispatch_calls() == []


def test_a_pull_request_no_longer_ready_dispatches_nothing(db, wake_client, github):
    _train_park(db)
    github.ready_at(NUMBER, NOW - timedelta(minutes=WINDOW + 5),
                    removed=NOW - timedelta(minutes=WINDOW + 2))

    _tick(wake_client)

    assert github.dispatch_calls() == []


def test_a_workspace_trains_park_dispatches_nothing(db, wake_client, github):
    doc = _train_park(db)
    doc["metadata"]["dispatch"]["merge_target"]["merge_mode"] = "train"
    _past(github)

    _tick(wake_client)

    assert github.dispatch_calls() == []


def test_the_dispatch_names_only_the_tasks_own_repository(db, wake_client, github):
    _train_park(db)
    _past(github)

    _tick(wake_client)

    hosts = {urlparse(url).netloc for _, url, _, _ in github.calls}
    paths = {urlparse(url).path for _, url, _, _ in github.calls}
    assert hosts == {"api.github.com"}
    assert all(p.startswith("/repos/example-org/widgets") for p in paths), paths


# --------------------------------------------------------------------------
# 6. the window, from one place
# --------------------------------------------------------------------------

def test_the_window_comes_from_the_one_shared_source():
    data = json.loads(trainboard.GATHER_SOURCE.read_text())
    assert trainboard.gather_defaults() == trainboard.Gather(
        min_prs=data["min_prs"], max_wait_minutes=data["max_wait_minutes"])
    # The owner's pick, "6 ready or 20 minutes", unchanged.
    assert (data["min_prs"], data["max_wait_minutes"]) == (6, 20)


def test_the_script_reads_the_same_file_and_states_no_second_copy():
    code = SCRIPT.read_text()
    assert "apps/swarm-api/swarm_api/merge_train_gather.json" in code
    assert trainboard.GATHER_SOURCE == REPO / "apps/swarm-api/swarm_api/merge_train_gather.json"
    for knob in ("MERGE_TRAIN_MIN_PRS", "MERGE_TRAIN_MAX_WAIT_MINUTES"):
        assignment = next(line.strip() for line in code.splitlines() if line.strip().startswith(
            ("MIN_PRS=", "MAX_WAIT_MINUTES=")) and knob in line)
        assert "gather_default" in assignment, assignment
        assert not re.search(r":-\d", assignment), assignment
    source = Path(trainboard.__file__).read_text()
    assert not re.search(r"\b(?:max_wait_minutes|min_prs)\s*=\s*\d", source)


def test_the_repository_variable_sets_the_window_as_merge_train_yml_hands_it(db, wake_client,
                                                                            github):
    _train_park(db)
    github.variables[trainboard.WAIT_VARIABLE] = "5"
    github.ready_at(NUMBER, NOW - timedelta(minutes=6))

    _tick(wake_client)

    assert len(github.dispatched) == 1


@pytest.mark.parametrize("value", ["", "soon", "-1"])
def test_a_window_variable_that_is_not_minutes_is_the_default(db, wake_client, github, value):
    _train_park(db)
    github.variables[trainboard.WAIT_VARIABLE] = value
    github.ready_at(NUMBER, NOW - timedelta(minutes=6))

    _tick(wake_client)

    assert github.dispatch_calls() == []
