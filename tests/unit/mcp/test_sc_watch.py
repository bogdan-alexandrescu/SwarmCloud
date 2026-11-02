"""`/sc check` and `/sc watch` (SC-WATCH, owner request 2026-10-14).

"add a feature to the sc plugin to have a check every set interval (default
30 min) and see if there are any workflows or tasks that are stuck or stale
and to attach all workflows for a given claude code session running at the
time for that project". Held here, over RECORDED API answers (the shapes
swarm-api's routes serve -- `codec.task_to_api`, `workflow_to_api`,
`waiting.waiting_for`, `_event_to_api`):

* each classification rule flags its case, and a control beside it that is
  NOT flagged -- so a rule that flagged everything goes red too;
* the check is scoped to one repository, and reads only (every call a GET);
* `{attach: "all", new_only}` never hands a session a second row for a
  workflow it already has one for;
* the interval parses, clamps to 5 minutes and rounds to what cron can say;
* the SessionStart hook finds the saved watch by the same key the bridge
  saves it under, and re-arms it.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from swarm_mcp import check, launch, sc, server, watch
from swarm_mcp.client import SwarmError

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
#: The Claude Code session every fixture here was submitted by: the check and
#: the attach consider only that session's work (SC-SESSION-SCOPE,
#: tests/unit/mcp/test_sc_session_scope.py holds the scope itself).
SESSION = "11111111-2222-4333-8444-555555555555"
APP = "https://github.com/acme/app"
OTHER = "https://github.com/acme/other"


def ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _this_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


def task(task_id: str, state: str, *, repo: str | None = APP, created: float = 180,
         session: str | None = SESSION, **extra) -> dict:
    doc = {
        "id": task_id,
        "tenant_id": "eng",
        "state": state,
        "runner_profile": "claude-code",
        "resource_class": "standard",
        "created_at": ago(created),
        "updated_at": ago(1),
        "repository_url": repo,
        "park_reason": None,
        "blocked_by": [],
        "heartbeat_at": None,
        "heartbeat": None,
        "links": {"console": f"https://console.test/agents/{task_id}"},
        "metadata": {"claude_session": session} if session else {},
        "claude_session": session,
    }
    doc.update(extra)
    return doc


def event(kind: str, minutes: float) -> dict:
    return {"type": kind, "at": ago(minutes)}


def workflow(wid: str, steps: list[tuple[str, str, str, list[str]]], *, state: str = "RUNNING",
             stored: str | None = None, drift: dict | None = None,
             session: str | None = SESSION) -> dict:
    doc = {
        "claude_session": session,
        "workflow_id": wid,
        "tenant_id": "eng",
        "state": state,
        "stored_state": stored or state,
        "state_source": "derived",
        "created_at": ago(200),
        "steps": [
            {"step_id": sid, "task_id": tid, "runner_profile": profile, "depends_on": deps,
             "links": {"console": f"https://console.test/agents/{tid}"}}
            for sid, tid, profile, deps in steps
        ],
        "links": {"console": f"https://console.test/workflows/{wid}"},
    }
    if drift is not None:
        doc["drift"] = drift
    return doc


class Api:
    """Recorded answers, routed by path. Anything but a GET fails the test:
    the check is read-only."""

    tenant = None

    def __init__(self, *, running=(), recent=(), tasks=(), singles=(), details=None, events=None,
                 capacity=None):
        self.running = list(running)
        self.recent = list(recent)
        self.tasks = {t["id"]: t for t in tasks}
        self.singles = list(singles)
        self.details = details or {}
        self.events_of = events or {}
        self.capacity = capacity
        self.seen: list[str] = []

    def tenants_mine(self):
        return []

    def workflows(self, *, states=(), active=False, limit=None, page_token=None,
                  claude_session=None, repository=None):
        # The session and repository filters are ignored, as a deployment
        # older than CHECK-FILTER ignores them: the check filters each row itself.
        query = [("state", s) for s in states] + ([("active", "true")] if active else [])
        return self.request("GET", "/v1/workflows?" + "&".join(f"{k}={v}" for k, v in query))

    def request(self, method, path, **_):
        assert method == "GET", f"the check wrote: {method} {path}"
        self.seen.append(path)
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        route = parts.path
        if route == "/v1/workflows":
            rows = self.running if "active" in query else self.recent
            return {"workflows": copy.deepcopy(rows), "next_page_token": None, "tenant_id": "eng",
                    "filter": {"stored_states": ["QUEUED", "RUNNING"]} if "active" in query else {}}
        if route.startswith("/v1/workflows/"):
            wid = route.rsplit("/", 1)[1]
            doc = next(w for w in self.running + self.recent if w["workflow_id"] == wid)
            ids = {s["task_id"] for s in doc["steps"]}
            return {"workflow": copy.deepcopy(doc), "tenant_id": "eng",
                    "tasks": [copy.deepcopy(t) for t in self.tasks.values() if t["id"] in ids]}
        if route == "/v1/tasks":
            state = query["state"][0]
            return {"tasks": [copy.deepcopy(t) for t in self.singles if t["state"] == state],
                    "next_page_token": None, "tenant_id": "eng"}
        if route.endswith("/events"):
            task_id = route.split("/")[3]
            assert query.get("order") == ["desc"]
            return {"task_id": task_id, "events": list(self.events_of.get(task_id, [])),
                    "next_page_token": None}
        if route == check.OPEN_QUESTIONS_ROUTE:
            # Owner questions (#2835): no finished task here asked any.
            return {"tasks": [], "complete": True, "tenant_id": "eng"}
        if route.startswith("/v1/tasks/"):
            task_id = route.split("/")[3]
            found = self.details.get(task_id) or self.tasks.get(task_id) or next(
                t for t in self.singles if t["id"] == task_id)
            return {"task": copy.deepcopy(found)}
        if route == "/v1/capacity":
            if self.capacity is None:
                raise SwarmError("GET /v1/capacity -> 503")
            return self.capacity
        if route == "/v1/tenants/mine":
            return []
        raise AssertionError(f"unexpected read {path}")


def run(api: Api, **kwargs) -> dict:
    kwargs.setdefault("repository", APP)
    return check.run(api, now=NOW, names=lambda _c: {}, **kwargs)


def flagged(result: dict) -> dict[str, dict]:
    """task id (or workflow id for a workflow-level flag) -> its item."""
    return {(i.get("task_id") or i.get("workflow_id")): i for i in result["flagged"]}


# --------------------------------------------------------------------------
# (a) RUNNING/STARTING with no heartbeat or event
# --------------------------------------------------------------------------


def test_a_quiet_running_task_is_stale_and_one_that_beats_or_reports_is_not():
    quiet = task("task_quiet", "RUNNING", heartbeat_at=ago(45), heartbeat="read", heartbeat_grace_seconds=600)
    beating = task("task_beating", "RUNNING", heartbeat_at=ago(2), heartbeat="read")
    talking = task("task_talking", "STARTING", heartbeat_at=ago(45), heartbeat="read")
    young = task("task_young", "RUNNING", created=10, heartbeat="read")
    api = Api(singles=[quiet, beating, talking, young], events={
        "task_quiet": [event("heartbeat", 41), event("running", 90)],
        "task_talking": [event("checkpoint_completed", 3)],
    })

    result = run(api)

    got = flagged(result)
    assert set(got) == {"task_quiet"}, got
    item = got["task_quiet"]
    assert (item["kind"], item["rule"]) == ("STALE", "no_heartbeat")
    assert item["evidence"]["quiet_minutes"] == 41
    assert item["evidence"]["last_event"] == {"type": "heartbeat", "at": ago(41)}
    assert item["evidence"]["window_minutes"] == 20
    assert item["console"] == "https://console.test/agents/task_quiet"
    assert item["next"] == "/sc status task_quiet"
    # The beating task cost no events read: its beat cleared it.
    assert not any("task_beating/events" in p for p in api.seen)


def test_a_workflow_step_reads_its_heartbeat_from_the_task_route():
    """A workflow read serves its steps without the lease's beat; the check
    reads the task to get it, rather than calling every step quiet."""
    doc = workflow("wf_run", [("build", "task_step", "claude-code", []), ("ok", "task_ok", "claude-code", [])])
    step = task("task_step", "RUNNING")
    fine = task("task_ok", "RUNNING")
    api = Api(running=[doc], tasks=[step, fine], details={
        "task_step": {**step, "heartbeat_at": ago(30), "heartbeat": "read"},
        "task_ok": {**fine, "heartbeat_at": ago(1), "heartbeat": "read"},
    }, events={"task_step": [event("running", 60)]})

    got = flagged(run(api))

    assert set(got) == {"task_step"}
    assert got["task_step"]["workflow_id"] == "wf_run" and got["task_step"]["step_id"] == "build"
    assert got["task_step"]["next"] == "/sc status wf_run"
    assert "/v1/tasks/task_step" in api.seen


def test_the_window_is_the_callers():
    quiet = task("task_q", "RUNNING", heartbeat_at=ago(25), heartbeat="read")
    api = Api(singles=[quiet], events={"task_q": [event("heartbeat", 25)]})
    assert set(flagged(run(api))) == {"task_q"}
    assert flagged(run(api, stale_minutes=30)) == {}
    with pytest.raises(SwarmError, match="stale_minutes"):
        run(api, stale_minutes=0)


# --------------------------------------------------------------------------
# (b) READY/QUEUED longer than the window, with the binding pool
# --------------------------------------------------------------------------

_LEAD = {"pool": "tenant:eng", "state": "full", "active": 4, "limit": 4, "units": 1, "reason": "POOL_FULL"}


def test_a_long_ready_task_names_the_pool_that_binds_it_and_a_fresh_one_is_not_flagged():
    waiting = task("task_wait", "READY", waiting_for={"admissible_now": False, "holds_capacity": False,
                                                      "lead": _LEAD, "pools": [_LEAD], "complete": True})
    # Created three hours ago, but it entered READY five minutes ago.
    fresh = task("task_fresh", "READY", waiting_for={"admissible_now": False, "lead": _LEAD})
    api = Api(singles=[waiting, fresh], events={
        "task_wait": [event("ready", 50), event("queued", 52)],
        "task_fresh": [event("ready", 5), event("retrying", 6)],
    })

    got = flagged(run(api))

    assert set(got) == {"task_wait"}
    item = got["task_wait"]
    assert (item["kind"], item["rule"]) == ("STUCK", "waiting_for_capacity")
    evidence = item["evidence"]
    assert (evidence["age_minutes"], evidence["age_is"]) == (50, "exact")
    assert evidence["binding"]["pool"] == "tenant:eng"
    assert evidence["binding"]["pool_state"] == "full"
    assert "tenant:eng" in item["reason"]


def test_a_long_queued_task_is_bound_by_the_capacity_read_when_no_pass_recorded_one():
    queued = task("task_q", "QUEUED")
    capacity = {
        "pools": [{"name": "global", "effective_limit": 10, "active": 10, "available": 0, "enabled": True},
                  {"name": "tenant:eng", "effective_limit": 8, "active": 2, "available": 6, "enabled": True}],
        "runner_profiles": {"claude-code": {"pools": ["global", "tenant:eng"], "units": 1}},
    }
    api = Api(singles=[queued], events={"task_q": [event("queued", 40)]}, capacity=capacity)

    item = flagged(run(api))["task_q"]

    binding = item["evidence"]["binding"]
    assert binding["pool"] == "global" and binding["room"] == 0
    assert binding["source"].startswith("/v1/capacity")
    assert sum(1 for p in api.seen if p == "/v1/capacity") == 1


def test_an_age_beyond_the_events_read_is_a_lower_bound():
    beats = [event("heartbeat", m) for m in range(30, 30 + check.EVENT_PAGE)]
    queued = task("task_old", "QUEUED", blocked_by=[{"pool": "tenant:eng", "reason": "POOL_FULL"}])
    api = Api(singles=[queued], events={"task_old": beats})
    item = flagged(run(api))["task_old"]
    assert item["evidence"]["age_is"] == "at least"
    assert item["evidence"]["age_minutes"] == 30 + check.EVENT_PAGE - 1
    assert item["evidence"]["binding"]["source"] == "the scheduler's last pass (blocked_by)"


# --------------------------------------------------------------------------
# (c) PARKED
# --------------------------------------------------------------------------


def test_parked_flags_human_required_long_ci_waits_and_failed_dependencies_only():
    human = task("task_human", "PARKED", park_reason="HUMAN_REQUIRED")
    ci_long = task("task_ci_long", "PARKED", park_reason="CI_PENDING")
    ci_short = task("task_ci_short", "PARKED", park_reason="CI_PENDING")
    quota = task("task_quota", "PARKED", park_reason="PROVIDER_QUOTA_EXHAUSTED")
    api = Api(singles=[human, ci_long, ci_short, quota], events={
        "task_human": [event("parked", 12)],
        "task_ci_long": [event("parked", 120)],
        "task_ci_short": [event("parked", 30)],
    })

    got = flagged(run(api))

    assert set(got) == {"task_human", "task_ci_long"}, got
    assert got["task_human"]["evidence"]["park_reason"] == "HUMAN_REQUIRED"
    assert got["task_human"]["evidence"]["age_minutes"] == 12
    assert got["task_ci_long"]["evidence"]["age_minutes"] == 120
    assert all(i["rule"] == "parked" and i["kind"] == "STUCK" for i in got.values())


def test_a_dependency_park_is_flagged_only_when_its_dependency_failed():
    failed_dep = workflow("wf_dead", [("a", "task_a", "claude-code", []), ("b", "task_b", "claude-code", ["a"])])
    live_dep = workflow("wf_live", [("c", "task_c", "claude-code", []), ("d", "task_d", "claude-code", ["c"])])
    tasks = [
        task("task_a", "FAILED"),
        task("task_b", "PARKED", park_reason="DEPENDENCY_INCOMPLETE"),
        task("task_c", "RUNNING", created=5),
        task("task_d", "PARKED", park_reason="DEPENDENCY_INCOMPLETE"),
    ]
    got = flagged(run(Api(running=[failed_dep, live_dep], tasks=tasks)))
    assert set(got) == {"task_b"}
    assert got["task_b"]["evidence"]["dependencies"] == {"a": "FAILED"}


# --------------------------------------------------------------------------
# (d) a merge step refused
# --------------------------------------------------------------------------


def test_a_refused_merge_step_is_flagged_with_its_pr_and_another_failure_is_not():
    refused = workflow("wf_merge", [("fix", "task_fix", "claude-code", []),
                                    ("merge", "task_merge", "merge", ["fix"])], state="FAILED")
    other = workflow("wf_other_fail", [("merge", "task_merge2", "merge", [])], state="FAILED")
    done = workflow("wf_done", [("merge", "task_merge3", "merge", [])], state="SUCCEEDED")
    tasks = [
        task("task_fix", "SUCCEEDED"),
        task("task_merge", "FAILED", runner_profile="merge", last_error="head_moved: the head moved",
             result_summary={"pull_request": 41, "refusal": {"code": "head_moved", "message": "the head moved"}}),
        task("task_merge2", "FAILED", runner_profile="merge", last_error="worker_unprotected: no",
             result_summary={"refusal": {"code": "worker_unprotected", "message": "no"}}),
        task("task_merge3", "SUCCEEDED", runner_profile="merge"),
    ]
    api = Api(recent=[refused, other, done], tasks=tasks)

    got = flagged(run(api))

    assert set(got) == {"task_merge"}, got
    item = got["task_merge"]
    assert (item["kind"], item["rule"]) == ("STUCK", "merge_refused")
    assert item["pull_request"] == "https://github.com/acme/app/pull/41"
    assert item["evidence"]["refusal"] == "head_moved"
    assert item["evidence"]["pull_request_state"] is None
    assert "not read" in item["evidence"]["pull_request_state_unread_because"]
    assert "https://github.com/acme/app/pull/41" in item["next"]
    # A SUCCEEDED workflow is not even read.
    assert "/v1/workflows/wf_done" not in api.seen


@pytest.mark.parametrize("code", ["merge_conflict", "checks_failed"])
def test_every_named_merge_refusal_is_flagged_from_last_error_alone(code):
    doc = workflow("wf_m", [("merge", "task_m", "merge", [])], state="FAILED")
    refused = task("task_m", "FAILED", runner_profile="merge", last_error=f"{code}: PR #7 cannot merge")
    assert set(flagged(run(Api(recent=[doc], tasks=[refused])))) == {"task_m"}


#: The real GitHub reader, kept before the suite's offline stub replaces it
#: (tests/unit/mcp/conftest.py `_no_github_reads`).
_REAL_PULL_STATE = getattr(check, "github_pull_state", None)


def _refused_merges(*numbers: int) -> Api:
    docs, tasks = [], []
    for n, number in enumerate(numbers):
        docs.append(workflow(f"wf_m{n}", [("merge", f"task_m{n}", "merge", [])], state="FAILED"))
        tasks.append(task(f"task_m{n}", "FAILED", runner_profile="merge", last_error="head_moved: moved",
                          result_summary={"pull_request": number,
                                          "refusal": {"code": "head_moved", "message": "moved"}}))
    return Api(recent=docs, tasks=tasks)


def _answer_github(monkeypatch, states: dict[int, str]) -> list[tuple[str, int]]:
    asked: list[tuple[str, int]] = []

    def reader(key, number):
        asked.append((key, number))
        return states[number]

    monkeypatch.setattr(check, "github_pull_state", reader)
    return asked


@pytest.mark.parametrize("settled", ["MERGED", "CLOSED"])
def test_a_refused_merge_whose_pr_has_since_settled_is_not_flagged(monkeypatch, settled):
    # Epic #1334: PRs 1328, 1317, 1312, 1316 were MERGED and stayed flagged.
    asked = _answer_github(monkeypatch, {41: settled, 42: "OPEN"})

    got = flagged(run(_refused_merges(41, 42)))

    assert set(got) == {"task_m1"}, got  # the OPEN one is the control
    assert got["task_m1"]["evidence"]["pull_request_state"] == "OPEN"
    assert "pull_request_state_unread_because" not in got["task_m1"]["evidence"]
    assert sorted(asked) == [("github.com/acme/app", 41), ("github.com/acme/app", 42)]


def test_a_pr_named_by_two_merge_steps_is_read_once_per_check(monkeypatch):
    asked = _answer_github(monkeypatch, {41: "OPEN"})
    assert set(flagged(run(_refused_merges(41, 41)))) == {"task_m0", "task_m1"}
    assert asked == [("github.com/acme/app", 41)]


def test_a_failed_pr_read_keeps_the_flag_and_says_the_state_was_not_read(monkeypatch):
    def refused(key, number):
        raise SwarmError("GitHub answered HTTP 403")

    monkeypatch.setattr(check, "github_pull_state", refused)

    item = flagged(run(_refused_merges(41)))["task_m0"]

    assert item["rule"] == "merge_refused"
    assert item["evidence"]["pull_request_state"] is None
    why = item["evidence"]["pull_request_state_unread_because"]
    assert why.startswith("PR state not read") and "HTTP 403" in why


class _Response:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(("body", "state"), [
    ({"state": "closed", "merged": True, "merged_at": "2026-10-15T00:30:00Z"}, "MERGED"),
    ({"state": "closed", "merged": False, "merged_at": None}, "CLOSED"),
    ({"state": "open", "merged": False, "merged_at": None}, "OPEN"),
])
def test_the_pr_state_is_read_from_githubs_public_api_with_no_credential(monkeypatch, body, state):
    sent = []

    def urlopen(request, timeout=None):
        sent.append(request)
        return _Response(body)

    monkeypatch.setattr(check.urllib.request, "urlopen", urlopen)

    assert _REAL_PULL_STATE("github.com/acme/app", 41) == state

    (request,) = sent
    assert request.full_url == "https://api.github.com/repos/acme/app/pulls/41"
    assert request.get_method() == "GET"
    headers = {k.lower() for k, _ in request.header_items()}
    assert "authorization" not in headers, headers


def test_a_pr_read_that_fails_or_names_another_forge_raises(monkeypatch):
    import urllib.error

    def refused(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "rate limited", {}, None)

    monkeypatch.setattr(check.urllib.request, "urlopen", refused)
    with pytest.raises(SwarmError, match="HTTP 403"):
        _REAL_PULL_STATE("github.com/acme/app", 41)
    with pytest.raises(SwarmError, match="not a GitHub repository"):
        _REAL_PULL_STATE("gitlab.com/acme/app", 41)


# --------------------------------------------------------------------------
# (e) stored state disagrees with derived state
# --------------------------------------------------------------------------


def test_the_stale_running_cache_is_flagged_and_a_lagging_queued_cache_is_not():
    stale = workflow("wf_stale", [("a", "task_s", "claude-code", [])], state="SUCCEEDED", stored="RUNNING",
                     drift={"agrees": False, "stored": "RUNNING", "derived": "SUCCEEDED"})
    lagging = workflow("wf_lag", [("a", "task_l", "claude-code", [])], state="RUNNING", stored="QUEUED")
    agreeing = workflow("wf_ok", [("a", "task_o", "claude-code", [])], state="SUCCEEDED")
    tasks = [task("task_s", "SUCCEEDED"), task("task_l", "RUNNING", created=5), task("task_o", "SUCCEEDED")]
    api = Api(running=[lagging], recent=[stale, lagging, agreeing], tasks=tasks)

    got = flagged(run(api))

    assert set(got) == {"wf_stale"}, got
    item = got["wf_stale"]
    assert (item["kind"], item["rule"]) == ("STALE", "state_disagrees")
    assert item["evidence"]["stored_state"] == "RUNNING"
    assert item["evidence"]["derived_state"] == "SUCCEEDED"
    assert item["next"] == "/sc status wf_stale"
    assert item["console"] == "https://console.test/workflows/wf_stale"


def test_a_workflow_served_without_derivation_is_flagged():
    doc = workflow("wf_raw", [("a", "task_r", "claude-code", [])])
    doc["state_source"] = "stored"
    api = Api(running=[doc], tasks=[task("task_r", "RUNNING", created=5)])
    item = flagged(run(api))["wf_raw"]
    assert item["rule"] == "state_disagrees" and "without deriving" in item["reason"]


# --------------------------------------------------------------------------
# Scope, reads, ordering
# --------------------------------------------------------------------------


def _two_projects() -> Api:
    ours = workflow("wf_ours", [("a", "task_ours", "claude-code", [])])
    theirs = workflow("wf_theirs", [("a", "task_theirs", "claude-code", [])])
    tasks = [task("task_ours", "PARKED", park_reason="HUMAN_REQUIRED"),
             task("task_theirs", "PARKED", park_reason="HUMAN_REQUIRED", repo=OTHER)]
    singles = [task("task_single_theirs", "PARKED", park_reason="HUMAN_REQUIRED", repo=OTHER),
               task("task_single_none", "PARKED", park_reason="HUMAN_REQUIRED", repo=None)]
    return Api(running=[ours, theirs], tasks=tasks, singles=singles,
               events={t: [event("parked", 15)] for t in
                       ("task_ours", "task_theirs", "task_single_theirs", "task_single_none")})


def test_the_check_is_scoped_to_one_repository():
    result = run(_two_projects())
    assert set(flagged(result)) == {"task_ours"}
    assert [w["workflow_id"] for w in result["running"]["workflows"]] == ["wf_ours"]
    assert result["running"]["single_tasks"] == []
    assert result["out_of_scope"] == {"workflows": 1, "single_tasks": 2}
    assert (result["repository"], result["repository_source"]) == ("github.com/acme/app", "given")


def test_all_repositories_drops_the_scope():
    result = run(_two_projects(), repository="all")
    assert set(flagged(result)) == {"task_ours", "task_theirs", "task_single_theirs", "task_single_none"}
    assert result["repository"] is None


@pytest.mark.parametrize("spelling", [
    "git@github.com:Acme/App.git", "ssh://git@github.com/acme/app", "https://x-access:masked@github.com/acme/app.git",
    "acme/app", "https://github.com/acme/app/",
])
def test_every_spelling_of_one_repository_is_one_scope(spelling):
    assert check.repo_key(spelling) == "github.com/acme/app"
    assert check.repo_key(OTHER) != check.repo_key(spelling)


#: Every form a person or the check's own answer writes one repository in
#: (epic #1334: `github.com/owner/repo` reached the API as
#: `github.com/github.com/owner`).
_FORMS = [
    "acme/app",
    "github.com/acme/app",
    "github.com/acme/app.git",
    "https://github.com/acme/app",
    "https://github.com/acme/app.git",
    "https://github.com/acme/app/",
    "git@github.com:acme/app.git",
    "ssh://git@github.com/acme/app.git",
]


@pytest.mark.parametrize("form", _FORMS + ["origin"])
def test_every_accepted_repository_form_scopes_the_check_to_one_repository(form, tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/acme/app.git"],
                   check=True)
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    assert check.resolve_scope(form)["key"] == "github.com/acme/app"
    result = run(_two_projects(), repository=form)
    assert result["repository"] == "github.com/acme/app"
    assert set(flagged(result)) == {"task_ours"}


@pytest.mark.parametrize("form", _FORMS)
def test_every_accepted_repository_form_attaches_the_same_workflows(form, launcher):
    reply = json.loads(server._call(_project_api(), "swarm_workflow_launch",
                                    {"attach": "all", "repository": form, "new_only": True}))
    assert reply["repository"] == "github.com/acme/app"
    assert _launched(reply) == (["wf_ours"], ["task_mine"])


def test_the_default_scope_is_this_checkouts_origin(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:acme/app.git"], check=True)
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    result = run(_two_projects(), repository=None)
    assert (result["repository"], result["repository_source"]) == ("github.com/acme/app", "origin")
    assert set(flagged(result)) == {"task_ours"}


def test_no_origin_is_an_unscoped_check_that_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    result = run(_two_projects(), repository=None)
    assert result["repository"] is None
    assert "NOT scoped" in result["repository_note"]


def test_a_repository_that_names_none_is_refused():
    with pytest.raises(SwarmError, match="names no repository"):
        run(Api(), repository="not a repo at all")


def test_the_check_only_reads():
    api = _two_projects()
    run(api, repository="all")  # Api.request fails on anything but a GET
    assert api.seen and all(p.startswith("/v1/") for p in api.seen)


def test_a_spent_read_budget_is_reported_never_a_clean_bill(monkeypatch):
    monkeypatch.setattr(check, "MAX_TASK_READS", 0)
    result = run(_two_projects())
    assert result["flagged"] == []
    assert [u["task_id"] for u in result["unchecked"]] == ["task_ours"]
    assert "read budget" in result["unchecked"][0]["why"]


def test_what_needs_a_person_is_reported_first():
    quiet = task("task_quiet", "RUNNING", heartbeat_at=ago(45), heartbeat="read")
    human = task("task_human", "PARKED", park_reason="HUMAN_REQUIRED")
    api = Api(singles=[quiet, human], events={"task_quiet": [event("heartbeat", 45)],
                                              "task_human": [event("parked", 30)]})
    assert [i["rule"] for i in run(api)["flagged"]] == ["parked", "no_heartbeat"]
    assert run(api)["count"] == {"flagged": 2, "stuck": 1, "stale": 1}


def test_the_tool_is_served_and_refuses_an_unknown_argument():
    names = {t["name"] for t in server.TOOLS}
    assert {"swarm_check", "swarm_check_schedule"} <= names
    api = _two_projects()
    reply = json.loads(server._call(api, "swarm_check", {"repository": APP}))
    assert set(flagged(reply)) == {"task_ours"}
    with pytest.raises(SwarmError, match="does not take"):
        server._call(api, "swarm_check", {"cancel": True})


# --------------------------------------------------------------------------
# Attaching: this project's workflows, never twice
# --------------------------------------------------------------------------


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.delenv("SWARM_MCP_CONFIG_FROM", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    launch.forget_attached()
    yield
    launch.forget_attached()


def _project_api() -> Api:
    ours = workflow("wf_ours", [("a", "task_ours", "claude-code", [])])
    theirs = workflow("wf_theirs", [("a", "task_theirs", "claude-code", [])])
    return Api(running=[ours, theirs],
               tasks=[task("task_ours", "RUNNING"), task("task_theirs", "RUNNING", repo=OTHER)],
               singles=[task("task_mine", "RUNNING"), task("task_elsewhere", "RUNNING", repo=OTHER)])


def _launched(reply: dict) -> tuple[list[str], list[str]]:
    workflows = [entry["workflow_id"] for entry in reply["launches"] if entry.get("workflow_id")]
    tasks = [t for entry in reply["launches"] for t in entry.get("task_ids") or []]
    return workflows, tasks


def test_attach_all_for_a_project_launches_only_its_workflows_and_tasks(launcher):
    reply = launch.for_all(_project_api(), repository=APP, new_only=True)
    assert _launched(reply) == (["wf_ours"], ["task_mine"])
    assert reply["repository"] == "github.com/acme/app"
    assert reply["already_attached"] == []


def test_a_second_check_never_attaches_twice(launcher):
    first = launch.for_all(_project_api(), repository=APP, new_only=True)
    assert _launched(first) == (["wf_ours"], ["task_mine"])

    second = launch.for_all(_project_api(), repository=APP, new_only=True)

    assert _launched(second) == ([], [])
    assert sorted(second["already_attached"]) == ["task_mine", "wf_ours"]


def test_a_row_the_session_already_shows_is_excluded_by_id(launcher):
    reply = launch.for_all(_project_api(), repository=APP, exclude=["wf_ours"])
    assert _launched(reply) == ([], ["task_mine"])
    assert reply["already_attached"] == ["wf_ours"]


def test_attach_by_hand_still_attaches_after_a_check(launcher):
    launch.for_all(_project_api(), repository=APP, new_only=True)
    again = launch.for_all(_project_api())  # `/sc attach --all`, unfiltered: unchanged
    launched, tasks = _launched(again)
    # Both, in the board order (BOARD-ORDER): two tasks of the same age are
    # ordered by id, which is not what this test is about.
    assert (launched, sorted(tasks)) == (["wf_ours", "wf_theirs"], ["task_elsewhere", "task_mine"])
    assert "already_attached" not in again


def test_the_launch_tool_takes_the_filters_only_with_attach_all(launcher):
    api = _project_api()
    reply = json.loads(server._call(api, "swarm_workflow_launch",
                                    {"attach": "all", "repository": APP, "new_only": True}))
    assert _launched(reply) == (["wf_ours"], ["task_mine"])
    with pytest.raises(SwarmError, match="only with attach"):
        server._call(api, "swarm_workflow_launch", {"attach": "wf_ours", "new_only": True})


# --------------------------------------------------------------------------
# The interval
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "minutes"), [
    (None, 30), ("", 30), ("30m", 30), ("30", 30), ("15 min", 15), ("1h", 60), ("2h", 120),
    ("10minutes", 10), ("1h30m", 120), ("45m", 60), ("40m", 30),
])
def test_an_interval_parses_to_one_cron_can_say(given, minutes):
    assert watch.parse_interval(given)["minutes"] == minutes


@pytest.mark.parametrize("given", ["1m", "0", "4m", "2"])
def test_an_interval_below_five_minutes_is_held_to_five(given):
    parsed = watch.parse_interval(given)
    assert parsed["minutes"] == 5 and "minimum" in parsed["clamped"]


def test_an_interval_past_a_day_is_held_to_a_day():
    assert watch.parse_interval("48h")["minutes"] == 24 * 60


@pytest.mark.parametrize("given", ["soon", "-5m", "30s", "m", "1d"])
def test_a_typo_is_not_an_interval(given):
    with pytest.raises(SwarmError, match="not an interval"):
        watch.parse_interval(given)


@pytest.mark.parametrize(("minutes", "cron"), [
    (30, "7,37 * * * *"), (5, "2,7,12,17,22,27,32,37,42,47,52,57 * * * *"), (20, "7,27,47 * * * *"),
    (60, "7 * * * *"), (120, "7 0,2,4,6,8,10,12,14,16,18,20,22 * * *"), (1440, "7 0 * * *"),
])
def test_the_cron_fires_every_interval_off_the_hour_and_half_hour(minutes, cron):
    assert watch.cron_for(minutes) == cron


# --------------------------------------------------------------------------
# The preference, and the hook that re-arms it
# --------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    config_dir = tmp_path / "config"
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root / "sub"))
    return root


def test_watch_on_saves_the_preference_and_hands_back_the_cron_call(project):
    reply = json.loads(server._call(None, "swarm_check_schedule", {"interval": "2m"}))
    assert reply["interval_minutes"] == 5 and "clamped" in reply
    assert reply["arm_with"] == {"tool": "CronCreate",
                                 "args": {"cron": watch.cron_for(5), "prompt": "/sc check", "recurring": True}}
    assert reply["without_cron"] == "/loop 5m /sc check"
    # A subdirectory of the project is the project.
    assert reply["project"] == str(project.resolve())
    assert watch.read(project.resolve())["interval_minutes"] == 5

    status = json.loads(server._call(None, "swarm_check_schedule", {}))
    assert (status["watch"], status["interval_minutes"]) == ("on", 5)

    off = json.loads(server._call(None, "swarm_check_schedule", {"off": True}))
    assert (off["watch"], off["was_on"]) == ("off", True)
    assert watch.read(project.resolve()) is None


def _fake_uv(bin_dir: Path, body: str) -> None:
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\n" + body + "\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _hook(tmp_path: Path, project: Path, uv_body: str, **env: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_uv(bin_dir, uv_body)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL": "https://swarm.example.test",
        "SWARM_CONFIG_DIR": os.environ["SWARM_CONFIG_DIR"],
        "CLAUDE_PROJECT_DIR": str(project / "sub"),
        "SWARM_ENV_LOG": str(tmp_path / "uv-env"),
    }
    base.update(env)
    return subprocess.run([bash, str(_HOOK)], input="{}", capture_output=True, text=True,
                          timeout=60, env=base, check=False)


def _context(done: subprocess.CompletedProcess) -> str:
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def test_the_hook_finds_the_watch_the_bridge_saved_and_re_arms_it_when_nothing_runs(tmp_path, project):
    watch.save(watch.project_dir(), 15)
    context = _context(_hook(tmp_path, project, 'env > "$SWARM_ENV_LOG"; exit 0'))
    assert "`/sc watch 15m`" in context
    # And the bridge was told, for when something does run.
    assert "SWARM_SC_WATCH_MINUTES=15" in (tmp_path / "uv-env").read_text()


def test_the_hook_re_arms_even_with_auto_attach_off_and_without_asking_the_bridge(tmp_path, project):
    watch.save(watch.project_dir(), 30)
    context = _context(_hook(tmp_path, project, 'env > "$SWARM_ENV_LOG"; exit 0',
                             CLAUDE_PLUGIN_OPTION_AUTO_ATTACH="false"))
    assert "`/sc watch 30m`" in context
    assert not (tmp_path / "uv-env").exists()


def test_the_hook_says_nothing_of_a_watch_never_turned_on(tmp_path, project):
    done = _hook(tmp_path, project, "exit 0")
    assert (done.returncode, done.stdout) == (0, "")
    off = _hook(tmp_path, project, "exit 0", CLAUDE_PLUGIN_OPTION_AUTO_ATTACH="false")
    assert (off.returncode, off.stdout) == (0, "")


def test_another_projects_watch_is_not_this_ones(tmp_path, project):
    watch.save(tmp_path.resolve(), 30)
    done = _hook(tmp_path, project, "exit 0")
    assert done.stdout == ""


def test_the_bridge_names_the_re_arm_beside_the_attach(monkeypatch):
    listing = {"tenant_id": "eng", "workflows": [{"workflow_id": "wf_a", "state": "RUNNING", "steps_total": 1,
                                                  "current_steps": [], "age_seconds": 60}]}
    text = sc.session_start_context(listing, None, watch_minutes=30)
    assert "/sc attach --all" in text and "`/sc watch 30m`" in text
    assert sc.session_start_context(None, None, watch_minutes=30).startswith("The sc plugin's watch is on")
    assert sc.session_start_context(None, None) is None
    monkeypatch.setenv(watch.REARM_ENV, "30")
    assert watch.rearm_minutes() == 30
    monkeypatch.setenv(watch.REARM_ENV, "7; rm -rf /")
    assert watch.rearm_minutes() is None


# --------------------------------------------------------------------------
# The skill
# --------------------------------------------------------------------------


def test_the_skill_documents_both_verbs_and_may_schedule():
    text = _SKILL.read_text()
    assert "| `check` |" in text and "| `watch [interval]` |" in text and "| `watch off` |" in text
    assert "### `check`" in text and "### `watch [interval]`" in text
    for named in ("swarm_check", "swarm_check_schedule", "new_only", "CronCreate", "/loop 30m /sc check"):
        assert named in text, named
    front = text.split("---", 2)[1]
    for tool in ("CronCreate", "CronList", "CronDelete"):
        assert f"  - {tool}\n" in front, tool
