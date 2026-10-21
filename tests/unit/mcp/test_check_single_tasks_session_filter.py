"""The check asks `GET /v1/tasks` for this session's single tasks
(CHECK-FILTER-TASKS, part of #1749).

THE DEFECT. CHECK-FILTER moved the session filter for workflows onto the
server; the single-task read (`sc._callers_tasks_in`) still read every page of
the caller's live tasks, one walk per live state, and dropped other sessions'
rows after reading them. A caller with 250 live tasks of which this session
owns 3 paid five pages for three rows.

THE CHANGE. The walk sends `claude_session=` whenever the session is known,
and the check adds the API's `filter.left_out_single` to its own
`session.left_out`, so the report reads the same whichever side left a row
out. An API that ignores the parameter still works: the check's own
ownership test is the safety net. Held against the real API over
FakeFirestore.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from swarm_mcp import check, sc, session

from test_against_the_real_api import api, db, swarm  # noqa: F401

TENANT_SIZE = 250
OWN = {"task_mine_new", "task_mine_mid", "task_mine_oldest"}


def _single(db, task_id: str, at: datetime, sid: str) -> None:  # noqa: F811
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": "eng", "state": "RUNNING",
        "created_at": at, "updated_at": at, "started_at": at, "runner_profile": "mock",
        "resource_class": "standard", "input": {}, "metadata": {"claude_session": sid},
        "repository_url": "https://github.com/acme/app", "submitted_by": "alice@example.com",
    }


@pytest.fixture
def tenant(db, monkeypatch):  # noqa: F811
    """250 live single tasks of the caller's; this session owns 3, one of
    them the OLDEST of all. The other 247 are another session's, newer."""
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    for i in range(TENANT_SIZE - 3):
        _single(db, f"task_theirs_{i:03d}", now - timedelta(seconds=i), theirs)
    _single(db, "task_mine_new", now - timedelta(seconds=10, milliseconds=5), mine)
    _single(db, "task_mine_mid", now - timedelta(seconds=100, milliseconds=5), mine)
    _single(db, "task_mine_oldest", now - timedelta(hours=5), mine)
    monkeypatch.setenv(session.SESSION_ENV, mine)
    return {"mine": mine, "theirs": theirs}


def _recording(swarm, monkeypatch, *, ignore_session: bool = False) -> list[str]:  # noqa: F811
    """Every `GET /v1/tasks?` path the bridge asks; with `ignore_session`, the
    API answers as a deployment older than the filter: the parameter is
    dropped before the route sees it, so no `filter` block comes back."""
    asked: list[str] = []
    real = type(swarm).request

    def recording(self, method, path, **kwargs):
        if path.startswith("/v1/tasks?"):
            asked.append(path)
            if ignore_session:
                path = re.sub(r"&?claude_session=[^&]*", "", path).replace("?&", "?")
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(type(swarm), "request", recording)
    return asked


def test_the_check_reads_this_sessions_single_tasks_in_one_page(swarm, tenant, monkeypatch):  # noqa: F811
    asked = _recording(swarm, monkeypatch)
    result = check.run(swarm, repository="all", names=lambda _c: {})
    assert {t["task_id"] for t in result["running"]["single_tasks"]} == OWN
    running_reads = [p for p in asked if "state=RUNNING" in p]
    # MUTATION: drop `claude_session` from `_callers_tasks_in` and the
    # RUNNING walk is 5 pages of 50 -- everyone's tasks, read to be dropped.
    assert len(running_reads) == 1, running_reads
    # MUTATION: send it only for one state and another state's walk reads
    # every other session's rows again.
    assert all(f"claude_session={tenant['mine']}" in p for p in asked), asked
    assert "single_tasks_incomplete_because" not in result


def test_the_apis_left_out_counts_reach_the_checks_session_report(swarm, tenant):  # noqa: F811
    result = check.run(swarm, repository="all", names=lambda _c: {})
    # MUTATION: ignore `filter.left_out_single` and these read 0: the API
    # left the 247 out, and the bridge never saw them to count.
    assert result["session"]["left_out"]["single_tasks"] == {
        "other_session": TENANT_SIZE - 3, "untagged": 0}


def test_an_api_that_ignores_the_parameter_still_yields_the_sessions_three(swarm, tenant, monkeypatch):  # noqa: F811
    asked = _recording(swarm, monkeypatch, ignore_session=True)
    result = check.run(swarm, repository="all", names=lambda _c: {})
    # MUTATION: trust the API to have filtered (drop the after-read
    # ownership test) and the other session's 247 are checked as this one's.
    assert {t["task_id"] for t in result["running"]["single_tasks"]} == OWN
    # The bridge counted them itself, once. MUTATION: add a missing filter
    # block's counts as well as its own and this doubles.
    assert result["session"]["left_out"]["single_tasks"] == {
        "other_session": TENANT_SIZE - 3, "untagged": 0}
    # The whole walk was read: 5 pages of the RUNNING state.
    assert len([p for p in asked if "state=RUNNING" in p]) == 5


def test_the_walk_returns_the_apis_echo_summed_over_its_pages(swarm, tenant, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sc, "SINGLE_TASK_PAGE_SIZE", 1)
    tasks, tenant_id, gap, filtered = sc._callers_tasks_in(swarm, "RUNNING", claude_session=tenant["mine"])
    # MUTATION: follow the store page's token and a row is skipped or repeated.
    assert [t["id"] for t in tasks] == ["task_mine_new", "task_mine_mid", "task_mine_oldest"]
    assert tenant_id == "eng" and gap is None
    # MUTATION: keep only the last page's counts and this is short.
    assert filtered == {"claude_session": tenant["mine"],
                        "left_out": {"other_session": TENANT_SIZE - 3, "untagged": 0},
                        "left_out_single": {"other_session": TENANT_SIZE - 3, "untagged": 0}}


def test_the_walk_without_a_session_asks_for_none_and_reports_no_echo(swarm, tenant, monkeypatch):  # noqa: F811
    asked = _recording(swarm, monkeypatch)
    tasks, _, _, filtered = sc._callers_tasks_in(swarm, "RUNNING")
    # MUTATION: send an empty `claude_session=` and the API refuses it (422).
    assert not any("claude_session" in p for p in asked)
    assert len(tasks) == TENANT_SIZE and filtered is None
