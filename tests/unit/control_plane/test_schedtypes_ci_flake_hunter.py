"""The `ci-flake-hunter` schedule type (docs/schedules.md §3.7, lane S10c).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT: its file exists, the catalogue serves it available with
  its own `BUDGET_CAPS`, and the tick's loader imports it.
* A FLAKE IS A FAIL THEN A PASS AT ONE SHA, from every check run there
  (`filter=all`): a pass then a fail, a failure alone, and a cancelled run
  are not flakes. `min_occurrences` and `max_flakes` bound what is filed.
* ONE ISSUE PER FLAKE, ONCE: filed with label `bug` and its marker; a second
  firing finds it by the marker and comments only for a new newest sha; a
  retried firing writes nothing twice. A found issue's body is never edited.
* PERMISSIONS ARE SAID, NOT SWALLOWED: a credential without `checks: read`
  is that repository's failure with `checks_forbidden`; every record says
  workflow runs were not read and why; every repository failing is refused
  with `ci_flake_unreadable` when its switch is on, and recorded failed when
  it is off (the default).
* DIAGNOSES: `diagnose` submits one `claude-code` task per filed flake at the
  flaky sha, pushing nothing; a later firing posts its `diagnosis.md` once.
* DRY RUN writes nothing to GitHub or Firestore.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from swarm_api import refusals, schedulefire, scheduletypes
from swarm_api.errors import NotFound
from swarm_api.schedtypes import ci_flake_hunter as hunter

from .conftest import seed_tenant
from .test_schedtypes_issue_sweep import (
    ALICE,
    NOW,
    SCHEDULE_ID,
    firing_for,
    make_context,
    make_schedule,
    register,
    tasks,
)


def sha(n: int) -> str:
    return f"{n:040x}"


class FlakeGitHub:
    """api.github.com for one repository: commits, check runs, issues, comments."""

    def __init__(self) -> None:
        self.commits: list[str] = []
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.issues: list[dict[str, Any]] = []
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.forbidden: set[str] = set()
        #: Repositories whose every read answers 403.
        self.forbidden_repos: set[str] = set()
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.reads: list[str] = []
        self._next_id = 1000

    def run(self, at: str, name: str, conclusion: str, run_id: int) -> None:
        self.runs.setdefault(at, []).append({
            "id": run_id, "name": name, "conclusion": conclusion, "status": "completed",
            "html_url": f"https://github.com/example-org/widgets/runs/{run_id}",
        })

    def flake(self, at: str, name: str = "unit", base: int = 1) -> None:
        self.run(at, name, "failure", base)
        self.run(at, name, "success", base + 1)

    def _ok(self, data: Any, status: int = 200) -> tuple[int, bytes]:
        return status, json.dumps(data).encode()

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 timeout: float) -> tuple[int, bytes]:
        parsed = urlparse(url)
        _owner, repo, path = parsed.path.split("/repos/", 1)[1].split("/", 2)
        if repo in self.forbidden_repos:
            return 403, b"{}"
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        payload = json.loads(body) if body else {}
        if method == "GET":
            self.reads.append(path)
        kind = path.split("/")[0]
        if kind in self.forbidden or (kind == "commits" and "check-runs" in path and "checks" in self.forbidden):
            return 403, b"{}"
        if method == "GET" and path == "commits":
            assert query["sha"] == ["main"] and "since" in query
            return self._ok([{"sha": s} for s in self.commits] if page == 1 else [])
        if method == "GET" and path.endswith("/check-runs"):
            assert query["filter"] == ["all"]
            at = path.split("/")[1]
            return self._ok({"check_runs": self.runs.get(at, []) if page == 1 else []})
        if method == "GET" and path.startswith("check-runs/") and path.endswith("/annotations"):
            return self._ok([{"path": "tests/test_x.py", "start_line": 12, "title": "test_x timed out"}])
        if method == "GET" and path == "issues":
            assert query["state"] == ["open"]
            return self._ok(self.issues if page == 1 else [])
        if method == "POST" and path == "issues":
            self._next_id += 1
            issue = {"number": len(self.issues) + 1, "title": payload["title"], "body": payload["body"],
                     "labels": [{"name": n} for n in payload["labels"]], "state": "open"}
            self.issues.append(issue)
            self.writes.append((method, path, payload))
            return self._ok(issue, 201)
        if path.startswith("issues/") and path.endswith("/comments"):
            number = int(path.split("/")[1])
            if method == "GET":
                return self._ok(self.comments.get(number, []) if page == 1 else [])
            self._next_id += 1
            comment = {"id": self._next_id, "body": payload["body"], "user": {"login": "swarm-bot"},
                       "html_url": f"https://github.com/example-org/widgets/issues/{number}#c{self._next_id}"}
            self.comments.setdefault(number, []).append(comment)
            self.writes.append((method, path, payload))
            return self._ok(comment, 201)
        if method == "PATCH":
            self.writes.append((method, path, payload))
        return 404, b"{}"


@pytest.fixture
def gh() -> FlakeGitHub:
    return FlakeGitHub()


@pytest.fixture
def ctx(db, tokens, group_map, objects, gh, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    monkeypatch.delenv(refusals.env_name(hunter.CiFlakeUnreadable.code), raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, group_map, objects, writes=gh)


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def hunt(db, *repo_ids: str, **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_="ci-flake-hunter", repo_ids=repo_ids, cron="0 */6 * * *", **kwargs)


def twice_flaky(gh: FlakeGitHub, name: str = "unit") -> None:
    gh.commits = [sha(3), sha(2), sha(1)]
    gh.flake(sha(3), name, base=30)
    gh.flake(sha(1), name, base=10)
    gh.run(sha(2), name, "success", 20)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists_with_its_own_caps() -> None:
    entry = scheduletypes.get("ci-flake-hunter")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is hunter
    assert scheduletypes.budget_caps(entry) is hunter.BUDGET_CAPS
    assert scheduletypes.describe(entry)["budget_caps"]["max_concurrent"] == hunter.BUDGET_CAPS.max_concurrent


def test_its_refusal_ships_off() -> None:
    switch = refusals.SWITCHES[hunter.CiFlakeUnreadable.code]
    assert switch.default is False and not refusals.enforced(switch.code, {})
    assert refusals.enforced(switch.code, {switch.env: "on"})


# ---------------------------------------------------------------------------
# What a flake is
# ---------------------------------------------------------------------------


def _runs(*rows: tuple[str, str, int]) -> list[dict[str, Any]]:
    return [{"name": n, "conclusion": c, "id": i} for n, c, i in rows]


def test_a_failure_then_a_later_pass_at_one_sha_is_a_flake() -> None:
    found = hunter.flaky_checks(_runs(("unit", "failure", 1), ("unit", "success", 2),
                                      ("lint", "timed_out", 3), ("lint", "success", 4)))
    assert sorted(found) == ["lint", "unit"] and found["unit"]["id"] == 1


@pytest.mark.parametrize("rows", [
    (("unit", "success", 1), ("unit", "failure", 2)),      # passed, then broke: not a flake
    (("unit", "failure", 1),),                              # failed only
    (("unit", "cancelled", 1), ("unit", "success", 2)),    # superseded, not failed
    (("unit", "failure", 1), ("other", "success", 2)),     # a different check passed
])
def test_what_is_not_a_flake(rows) -> None:
    assert hunter.flaky_checks(_runs(*rows)) == {}


def test_rank_keeps_at_least_min_occurrences_most_first_up_to_max() -> None:
    per_sha = [(sha(3), {"a": {"id": 1}, "b": {"id": 2}}), (sha(2), {"a": {"id": 3}, "c": {"id": 4}}),
               (sha(1), {"a": {"id": 5}, "c": {"id": 6}})]
    flakes, below = hunter.rank(per_sha, min_occurrences=2, max_flakes=1)
    assert [(f.check, f.occurrences, f.shas[0]) for f in flakes] == [("a", 3, sha(3))]
    assert flakes[0].failed_run == {"id": 1}  # the newest sha's
    assert below == 1  # `b`, seen once; `c` met the floor and was cut by max_flakes


# ---------------------------------------------------------------------------
# Filing and updating
# ---------------------------------------------------------------------------


def test_it_files_one_bug_issue_per_flake_with_its_marker_and_records_the_repository(db, ctx, gh, repo_id) -> None:
    twice_flaky(gh)
    gh.commits.append(sha(9))
    gh.run(sha(9), "lint", "failure", 90)
    gh.run(sha(9), "lint", "success", 91)  # one occurrence: below min_occurrences
    recorded: list = []
    firing = firing_for(ctx, hunt(db, repo_id), recorded=recorded)

    work = hunter.create(firing)

    (issue,) = gh.issues
    assert issue["labels"] == [{"name": "bug"}]
    assert issue["title"] == "The check `unit` on main fails and then passes at the same commit (a CI flake)"
    assert issue["body"].startswith(hunter.ISSUE_MARKER.format(key=hunter.flake_key(repo_id, "unit")))
    assert "2 times on `main` in the last 7 days" in issue["body"]
    assert sha(3)[:12] in issue["body"] and sha(1)[:12] in issue["body"]
    assert "`tests/test_x.py:12` `test_x timed out`" in issue["body"]
    assert "actions: read" in issue["body"]
    (item,) = work
    assert item["kind"] == "api_action" and item["outcome"] == "succeeded" and item["repo_id"] == repo_id
    assert recorded == [item]  # recorded the moment it was done (§2.2)
    detail = item["detail"]
    assert detail["flakes"] == [{"check": "unit", "occurrences": 2, "sha": sha(3), "issue": 1, "action": "filed"}]
    assert detail["below_min_occurrences"] == 1 and detail["commits_read"] == 4
    assert "actions: read" in detail["workflow_runs"]
    assert tasks(db) == []  # diagnose is off by default


def test_a_second_firing_comments_once_for_a_new_occurrence_and_never_edits_the_body(db, ctx, gh, repo_id) -> None:
    twice_flaky(gh)
    schedule = hunt(db, repo_id)
    hunter.create(firing_for(ctx, schedule))
    body = gh.issues[0]["body"]

    # Nothing new: no write at all.
    again = hunter.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:b"))
    assert again[0]["detail"]["flakes"][0]["action"] == "unchanged"
    assert len(gh.writes) == 1

    # A new newest flaky sha: one comment, and a retry of that firing posts no second.
    gh.commits.insert(0, sha(4))
    gh.flake(sha(4), base=40)
    for _ in range(2):
        work = hunter.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:c"))
    assert [w[0] for w in gh.writes] == ["POST", "POST"] and len(gh.issues) == 1
    (comment,) = gh.comments[1]
    assert comment["body"].startswith(hunter.OCCURRENCE_MARKER.format(
        key=hunter.flake_key(repo_id, "unit"), sha=sha(4)))
    assert "3 occurrences" in comment["body"]
    assert work[0]["detail"]["flakes"][0]["action"] == "unchanged"
    assert gh.issues[0]["body"] == body and not [w for w in gh.writes if w[0] == "PATCH"]


def test_another_repositorys_marker_or_a_pull_request_is_not_the_flakes_issue(db, ctx, gh, repo_id) -> None:
    twice_flaky(gh)
    gh.issues.append({"number": 7, "body": hunter.ISSUE_MARKER.format(key=hunter.flake_key("other", "unit"))})
    gh.issues.append({"number": 8, "pull_request": {}, "body": hunter.ISSUE_MARKER.format(
        key=hunter.flake_key(repo_id, "unit"))})

    work = hunter.create(firing_for(ctx, hunt(db, repo_id)))

    assert work[0]["detail"]["flakes"][0]["action"] == "filed" and len(gh.issues) == 3


def test_a_check_name_cannot_break_out_of_its_code_span() -> None:
    flake = hunter.Flake(check="x` <img src=y> `", shas=[sha(1)])
    assert hunter.title(flake, "main").count("`") == 2


# ---------------------------------------------------------------------------
# Permissions and failures: said, never swallowed
# ---------------------------------------------------------------------------


def test_no_checks_read_is_that_repositorys_failure_with_its_code(db, ctx, gh, repo_id) -> None:
    twice_flaky(gh)
    gh.forbidden.add("checks")

    (item,) = hunter.create(firing_for(ctx, hunt(db, repo_id)))

    assert item["outcome"] == "failed" and item["detail"]["code"] == "checks_forbidden"
    assert "checks: read" in item["detail"]["message"] and "actions: read" in item["detail"]["workflow_runs"]
    assert gh.issues == []


def test_every_repository_failing_is_refused_when_the_switch_is_on(db, ctx, gh, repo_id, monkeypatch) -> None:
    monkeypatch.setenv(refusals.env_name(hunter.CiFlakeUnreadable.code), "on")
    gh.forbidden.add("commits")
    recorded: list = []

    with pytest.raises(hunter.CiFlakeUnreadable) as caught:
        hunter.create(firing_for(ctx, hunt(db, repo_id), recorded=recorded))

    assert caught.value.code == "ci_flake_unreadable" and caught.value.status_code == 409
    failure = caught.value.detail["repositories"][repo_id]
    assert failure["code"] == "writeback_forbidden" and "contents: read" in failure["message"]
    assert recorded == []


def test_one_repository_failing_does_not_hide_anothers_flakes(db, ctx, gh, repo_id, monkeypatch) -> None:
    # On, the refusal is still only for EVERY repository failing.
    monkeypatch.setenv(refusals.env_name(hunter.CiFlakeUnreadable.code), "on")
    twice_flaky(gh)
    refused = register(db, repo="gadgets")
    gh.forbidden_repos.add("gadgets")

    work = hunter.create(firing_for(ctx, hunt(db, refused, repo_id)))

    assert [(w["repo_id"], w["outcome"]) for w in work] == [(repo_id, "succeeded"), (refused, "failed")]
    assert work[1]["detail"]["code"] == "writeback_forbidden" and len(gh.issues) == 1


def test_it_reads_no_repository_past_its_room(db, ctx, gh, repo_id) -> None:
    other = register(db, repo="gadgets")
    twice_flaky(gh)

    work = hunter.create(firing_for(ctx, hunt(db, repo_id, other), room=1))

    assert [w["repo_id"] for w in work] == [repo_id]


# ---------------------------------------------------------------------------
# Diagnoses
# ---------------------------------------------------------------------------


def test_diagnose_submits_one_task_per_filed_flake_at_its_sha_pushing_nothing(db, ctx, gh, repo_id) -> None:
    twice_flaky(gh)
    firing = firing_for(ctx, hunt(db, repo_id, params={"diagnose": True}))

    work = hunter.create(firing)

    (task,) = tasks(db)
    assert [w["kind"] for w in work] == ["api_action", "task"] and work[1]["id"] == task["id"]
    assert task["runner_profile"] == "claude-code" and task["repository_ref"] == sha(3)
    assert task["metadata"]["schedule"] == firing.mark and task["submitted_by"] == ALICE
    assert task["metadata"]["ci_flake"] == {"repo_id": repo_id, "issue": 1, "sha": sha(3), "check": "unit"}
    assert task["metadata"].get("strategy", "collect") == "collect"
    prompt = task["input"]["prompt"]
    fid = firing.firing["firing_id"]
    assert prompt.count(f"=== CI FLAKE {fid} ===") == 1 and "diagnosis.md" in prompt
    assert task["state"] in ("QUEUED", "READY") and task.get("current_lease_id") is None


def _earlier_diagnosis(db, repo_id: str, task_id: str = "task_diag1", *, age=timedelta(hours=1)) -> None:
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": "eng", "runner_profile": "claude-code", "state": "SUCCEEDED",
        "created_at": NOW - age - timedelta(minutes=5), "completed_at": NOW - age,
        "metadata": {"ci_flake": {"repo_id": repo_id, "issue": 1, "sha": sha(3), "check": "unit"},
                     "schedule": {"schedule_id": SCHEDULE_ID, "firing_id": "f_prev", "type": hunter.TYPE}},
    }


def _artifacts(monkeypatch, ctx, files: dict[str, str]) -> None:
    def read_artifact(tenant_id: str, task_id: str, *, submitted_by: Any, name: str, **_: Any) -> dict:
        assert tenant_id == "eng" and name == hunter.DIAGNOSIS_FILE
        if task_id not in files:
            raise NotFound(f"task {task_id} lists no artifact {name}")
        return {"status": "ok", "content": files[task_id], "truncated": False}

    monkeypatch.setattr(ctx.inspection, "read_artifact", read_artifact)


def test_an_earlier_diagnosis_is_posted_on_its_issue_once(db, ctx, gh, repo_id, monkeypatch) -> None:
    _earlier_diagnosis(db, repo_id)
    _earlier_diagnosis(db, repo_id, "task_old", age=timedelta(days=8))
    _artifacts(monkeypatch, ctx, {"task_diag1": "The fixture shares a port.", "task_old": "stale"})
    schedule = hunt(db, repo_id)

    hunter.create(firing_for(ctx, schedule))
    hunter.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:b"))

    (comment,) = gh.comments[1]
    assert comment["body"].startswith(hunter.DIAGNOSIS_MARKER.format(task_id="task_diag1"))
    assert "The fixture shares a port." in comment["body"] and "stale" not in comment["body"]


def test_another_schedules_or_tenants_diagnosis_is_not_posted(db, ctx, gh, repo_id, monkeypatch) -> None:
    _earlier_diagnosis(db, repo_id)
    db.docs["tasks/task_diag1"]["metadata"]["schedule"]["schedule_id"] = "sch_other"
    _earlier_diagnosis(db, repo_id, "task_research")
    db.docs["tasks/task_research"]["tenant_id"] = "research"
    _artifacts(monkeypatch, ctx, {"task_diag1": "x", "task_research": "y"})

    hunter.create(firing_for(ctx, hunt(db, repo_id)))

    assert gh.comments == {}


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_says_what_it_would_do_and_writes_nothing(db, ctx, gh, repo_id, monkeypatch) -> None:
    twice_flaky(gh)
    _earlier_diagnosis(db, repo_id)
    _artifacts(monkeypatch, ctx, {"task_diag1": "cause"})
    before = len(tasks(db))

    output = hunter.dry_run(firing_for(ctx, hunt(db, repo_id, params={"diagnose": True})))

    (repo,) = output["repositories"]
    assert repo["flakes"] == [{"check": "unit", "occurrences": 2, "sha": sha(3), "issue": None, "would": "file"}]
    assert output["diagnoses_to_submit"] == 1 and output["diagnoses_to_attach"]["attached"] == 1
    assert output["reads"] == ["contents: read", "checks: read"] and "actions: read" in output["workflow_runs"]
    assert gh.writes == [] and gh.issues == [] and len(tasks(db)) == before
