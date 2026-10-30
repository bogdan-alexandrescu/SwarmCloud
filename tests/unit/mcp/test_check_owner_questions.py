"""`swarm_check` lists this session's finished tasks whose questions for the
owner have no answer (PLUGIN-OWNER-QUESTIONS, part of #1904).

MEASURED, 2026-10-22T01:00Z: a sweep found 31 unanswered questions from 17
tasks under one Claude Code session tag and 5 under another. The check
listed AWAITING_INPUT steps and nothing else a person owed an answer to, so a
question from a row the session did not watch end, or one that ended before
a context compaction, was never raised again.

What these tests hold, against the real API over FakeFirestore: a finished
task of this session with an unanswered question is listed with the open
questions whole; an answered one is not, and costs no questions read;
another session's is not; one in another repository is not; one finished
more than 7 days ago is not; a questions read that fails lands in
`unchecked` with its reason; a failed list read too; the cache spares a
second read of a finished task's questions; and the section is never a
stuck or stale flag. Since #2835 the tasks come from one server-side read
(`GET /v1/tasks/open-questions`), so a busy session's old question is
listed; the old walk is only the fallback for an API without the route,
and says so under `unchecked`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from swarm_mcp import check, progress, session

from test_against_the_real_api import api, db, swarm  # noqa: F401

_REPO = "https://github.com/acme/app"


def _question(n: int) -> dict:
    return {"question": f"Wake the parks on a hold release? ({n})",
            "options": [{"label": "wake", "description": "wake them"}, {"label": "wait", "description": "leave them"}],
            "recommended": "wake", "context": f"QUOTA-PARK-RECHECK ({n})"}


def _finished(db, task_id: str, sid: str, *, asked: int = 2, answered=(), days_ago: float = 1,  # noqa: F811
              repository: str = _REPO, workflow: bool = True, state: str = "SUCCEEDED") -> None:
    now = datetime.now(timezone.utc)
    ended = now - timedelta(days=days_ago)
    metadata = {"claude_session": sid, "title": "SC · QUOTA-PARK-RECHECK · 3 steps", "unit": "quota-park"}
    if answered:
        metadata["owner_answers"] = [{"question_index": i, "choice": "wake", "text": None, "by": "alice@example.com",
                                      "at": ended, "history": []} for i in answered]
    doc = {
        "id": task_id, "tenant_id": "eng", "state": state,
        "created_at": ended - timedelta(minutes=30), "updated_at": ended, "started_at": ended - timedelta(minutes=29),
        "completed_at": ended, "runner_profile": "mock", "resource_class": "standard", "input": {},
        "metadata": metadata, "repository_url": repository, "submitted_by": "alice@example.com",
        "result_summary": {"questions": asked},
    }
    if workflow:
        doc.update({"workflow_id": "wf_q", "step_id": "implement"})
    db.docs[f"tasks/{task_id}"] = doc


@pytest.fixture(autouse=True)
def _cold_cache():
    # Looked up when called, so a bridge without the cache fails the tests'
    # assertions, not their setup.
    getattr(check, "_ASKED", {}).clear()
    yield
    getattr(check, "_ASKED", {}).clear()


@pytest.fixture
def sid(monkeypatch):
    mine = str(uuid.uuid4())
    monkeypatch.setenv(session.SESSION_ENV, mine)
    return mine


@pytest.fixture
def reads(monkeypatch):
    """Every questions read, by task id; `task_broken`'s read fails."""
    seen: list[str] = []

    def owner_questions(client, task):  # noqa: ARG001
        tid = str(task.get("id") or task.get("task_id"))
        seen.append(tid)
        if tid == "task_broken":
            return {"questions": [], "questions_unavailable_because": "the artifact route says missing"}
        count = progress.questions_count(task)
        return {"questions": [_question(i) for i in range(count)]}

    monkeypatch.setattr(progress, "owner_questions", owner_questions)
    return seen


def _check(swarm, repository="all"):  # noqa: F811
    return check.run(swarm, repository=repository, names=lambda _c: {})


def test_a_finished_task_with_an_unanswered_question_is_listed_whole(swarm, db, sid, reads, monkeypatch):  # noqa: F811
    _finished(db, "task_open", sid, asked=2, answered=(1,))
    # The test API is configured with no console origin, so it serves no
    # link; the check's one reader of it stands in for one that does.
    monkeypatch.setattr(check, "console_link", lambda doc: "https://c.test/agents/" + str(doc.get("id")))
    result = _check(swarm)
    # MUTATION: no `owner_questions` section (the #1904 defect): KeyError.
    (entry,) = result["owner_questions"]
    assert entry["task_id"] == "task_open" and entry["workflow_id"] == "wf_q" and entry["step_id"] == "implement"
    # MUTATION: naming the workflow by its id only -- the title the lane was launched under is what a person knows.
    assert entry["workflow"] == "SC · QUOTA-PARK-RECHECK · 3 steps"
    assert entry["workspace"] == "eng"
    # MUTATION: listing every question, answered or not: index 1 was answered in the console.
    assert entry["open"] == 1
    assert entry["questions"] == [{"index": 0, **_question(0)}], entry["questions"]
    # MUTATION: dropping the console link the API served.
    assert entry.get("console") == "https://c.test/agents/task_open", entry
    # MUTATION: counting it as stuck or stale -- a question is information.
    assert result["count"]["flagged"] == 0 and result["flagged"] == []


def test_an_answered_one_is_not_listed_and_costs_no_read(swarm, db, sid, reads):  # noqa: F811
    _finished(db, "task_done", sid, asked=2, answered=(0, 1))
    _finished(db, "task_none", sid, asked=0, workflow=False)
    result = _check(swarm)
    # MUTATION: ignoring `owner_answers` lists the answered task.
    assert result["owner_questions"] == []
    # MUTATION: reading the questions of a task whose every question is
    # answered, or of one that asked none.
    assert reads == [], reads


def test_another_sessions_task_and_another_repositorys_are_not_listed(swarm, db, sid, reads):  # noqa: F811
    _finished(db, "task_theirs", str(uuid.uuid4()))
    _finished(db, "task_elsewhere", sid, repository="https://github.com/acme/other")
    _finished(db, "task_here", sid, workflow=False)
    result = _check(swarm, repository="acme/app")
    # MUTATION: dropping the session test (or the API's session filter
    # together with the ownership test) lists task_theirs; dropping the
    # repository scope lists task_elsewhere.
    assert [e["task_id"] for e in result["owner_questions"]] == ["task_here"], result["owner_questions"]
    # A single task is named by its title, not a workflow and step.
    (entry,) = result["owner_questions"]
    assert entry["title"] == "SC · QUOTA-PARK-RECHECK · 3 steps" and entry.get("workflow_id") is None
    assert "task_theirs" not in reads


def test_a_task_finished_more_than_seven_days_ago_is_not_listed(swarm, db, sid, reads):  # noqa: F811
    _finished(db, "task_old", sid, days_ago=8)
    _finished(db, "task_new", sid, days_ago=6, state="FAILED")
    result = _check(swarm)
    # MUTATION: no 7-day window lists task_old; reading only SUCCEEDED misses
    # the FAILED task_new, whose questions are as much the owner's.
    assert [e["task_id"] for e in result["owner_questions"]] == ["task_new"], result["owner_questions"]


def test_a_failed_questions_read_lands_in_unchecked(swarm, db, sid, reads):  # noqa: F811
    _finished(db, "task_broken", sid)
    result = _check(swarm)
    assert result["owner_questions"] == []
    # MUTATION: dropping the task whose read failed: it vanishes, and reads as "nothing asked".
    (miss,) = [u for u in result["unchecked"] if u.get("task_id") == "task_broken"]
    assert "the artifact route says missing" in miss["why"], miss
    # MUTATION: caching the failure: the next check would never read it again.
    _check(swarm)
    assert reads.count("task_broken") == 2, reads


def test_a_busy_sessions_old_question_is_listed_from_one_read(swarm, db, sid, reads, monkeypatch):  # noqa: F811
    """#2835: 600 succeeded tasks of this session in the last hours -- twelve
    pages of 50, past the old 10-page walk -- and, six days ago, one task
    whose question nobody answered."""
    for i in range(600):
        _finished(db, f"task_busy_{i:03d}", sid, asked=0, days_ago=i / 86400 * 10)
    _finished(db, "task_old_open", sid, asked=1, days_ago=6)
    paths: list[str] = []
    real = type(swarm).request

    def counting(self, method, path, **kwargs):
        if path.startswith("/v1/tasks"):
            paths.append(path)
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(type(swarm), "request", counting)
    result = _check(swarm)
    # MUTATION: page the session's finished tasks (the #2835 defect) and the
    # six-day-old task is never reached: `unchecked` says "more than 10 pages".
    assert [e["task_id"] for e in result["owner_questions"]] == ["task_old_open"], result["owner_questions"]
    assert not [u for u in result["unchecked"] if u.get("section") == "owner_questions"], result["unchecked"]
    # ONE read for (g), not a walk of every terminal state.
    asked = [p for p in paths if p.startswith(check.OPEN_QUESTIONS_ROUTE)]
    assert len(asked) == 1, asked
    assert not [p for p in paths if "state=SUCCEEDED" in p], paths


def test_a_failed_open_questions_read_lands_in_unchecked(swarm, db, sid, reads, monkeypatch):  # noqa: F811
    _finished(db, "task_open", sid)
    real = type(swarm).request

    def failing(self, method, path, **kwargs):
        if path.startswith(check.OPEN_QUESTIONS_ROUTE):
            raise check.SwarmError("503 Service Unavailable", status=503)
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(type(swarm), "request", failing)
    result = _check(swarm)
    assert result["owner_questions"] == []
    # MUTATION: swallowing the failed read -- "no questions" over a list never read.
    assert any(u.get("section") == "owner_questions" and "503" in u["why"]
               for u in result["unchecked"]), result["unchecked"]


def _without_the_route(swarm, monkeypatch, fail_state: str | None = None):  # noqa: F811
    """A deployment that predates the route: it answers 404 from the API."""
    real = type(swarm).request

    def old_api(self, method, path, **kwargs):
        if path.startswith(check.OPEN_QUESTIONS_ROUTE):
            raise check.SwarmError("404 not_found: no such task", status=404)
        if fail_state and path.startswith("/v1/tasks?") and f"state={fail_state}" in path:
            raise check.SwarmError("503 Service Unavailable", status=503)
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(type(swarm), "request", old_api)


def test_an_api_without_the_route_falls_back_to_paging_and_says_so(swarm, db, sid, reads, monkeypatch):  # noqa: F811
    _finished(db, "task_open", sid)
    _finished(db, "task_done", sid, asked=1, answered=(0,))
    _without_the_route(swarm, monkeypatch)
    result = _check(swarm)
    # The walk still finds the question, and still drops the answered one.
    assert [e["task_id"] for e in result["owner_questions"]] == ["task_open"]
    # MUTATION: fall back silently, and a session cannot tell a bounded walk
    # from the one read that sees every task.
    assert check.OPEN_QUESTIONS_FALLBACK in [u["why"] for u in result["unchecked"]], result["unchecked"]


def test_a_failed_list_read_in_the_fallback_lands_in_unchecked(swarm, db, sid, reads, monkeypatch):  # noqa: F811
    _finished(db, "task_open", sid)
    _without_the_route(swarm, monkeypatch, fail_state="FAILED")
    result = _check(swarm)
    # The other states were still read.
    assert [e["task_id"] for e in result["owner_questions"]] == ["task_open"]
    # MUTATION: swallowing the failed read -- "no questions" over a list never read.
    assert any("FAILED" in u["why"] and "503" in u["why"] for u in result["unchecked"]), result["unchecked"]


def test_the_cache_spares_a_second_read(swarm, db, sid, reads):  # noqa: F811
    _finished(db, "task_open", sid)
    first = _check(swarm)
    second = _check(swarm)
    assert first["owner_questions"] == second["owner_questions"] != []
    # MUTATION: no cache -- a check every 30 minutes re-reads every finished
    # task's questions.json: 2 reads here.
    assert reads == ["task_open"], reads


def test_the_cache_still_honours_an_answer_given_since(swarm, db, sid, reads):  # noqa: F811
    _finished(db, "task_open", sid, asked=2)
    assert _check(swarm)["owner_questions"][0]["open"] == 2
    _finished(db, "task_open", sid, asked=2, answered=(0,))
    # MUTATION: caching the OPEN questions instead of the questions: the
    # answer given in the console since never takes one off.
    assert _check(swarm)["owner_questions"][0]["questions"] == [{"index": 1, **_question(1)}]


def test_the_tools_description_tells_the_session_to_ask_the_person():
    from swarm_mcp import server

    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_check"]
    # MUTATION: a description that never names the section, or how to raise
    # it: a session reads the check and leaves the questions in its answer.
    for word in ("owner_questions", "AskUserQuestion", "swarm_task_answer", "NEVER a flag"):
        assert word in tool["description"], word
