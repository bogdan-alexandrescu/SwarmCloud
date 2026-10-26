"""The `pr-shepherd` schedule type (docs/schedules.md §3.6, lane S10b).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT: its file exists, so the catalogue serves it available
  with the caps its module declares, and the tick's loader imports it.
* ONLY STALE, OPEN, NON-DRAFT, UNHELD pull requests are touched, at most
  `max_prs_per_firing`, and the `labels` policy writes only changes.
* UPDATE-BRANCH is pinned to the head that was read. GitHub's answer is kept
  as given: 202 is `updated`, a 422 naming the expected head is `head_moved`
  (not a refusal), and anything else is a REFUSAL that names the permission
  -- `workflows: write` when the base changes `.github/workflows/` (§0.1
  check 2) -- and is not retried at the same head and base by a later firing.
* A CONFLICT on a pull request no issue run opened gets ONE comment per head,
  naming the files both sides changed. On an issue run's pull request, with
  `fix_conflicts` and the run gate passed, it gets a conflict-fix
  continuation of the run's integrator instead -- once per head, never for
  another tenant's run, never while the default `run: approve` gate holds.
* A REFUSED CREDENTIAL is reported: refused with `pr_shepherd_forge_refused`
  when the switch is on, a `failed` refusals item when it is off.
* DRY RUN writes nothing to GitHub or Firestore.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from swarm_api import refusals, schedulefire, scheduletypes
from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.schedtypes import pr_shepherd
from swarm_api.validation import IssueRef

from . import forge_fakes
from .conftest import seed_task, seed_tenant
from .test_schedtypes_issue_sweep import (
    ALICE,
    NOW,
    OWNER,
    REPO,
    SCHEDULE_ID,
    firing_for,
    make_context,
    make_schedule,
    register,
    tasks,
)

INTEGRATOR = "task_" + "ab" * 10
OTHER_TASK = "task_" + "cd" * 10
HEAD = "1" * 40
NEW_HEAD = "2" * 40


def _iso(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class PullsGitHub:
    """api.github.com's pull-request routes this type calls, in memory.

    A `forgewrite.WriteSend`: every request goes through the shipped
    `GitHubWriter`, so status mapping and the pinned host are the real ones.
    """

    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        #: (base, head) of a comparison -> {ahead_by, files}.
        self.compares: dict[tuple[str, str], dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.check_runs: dict[str, list[dict[str, Any]]] = {}
        self.update_answer: tuple[int, dict[str, Any]] = (202, {"message": "Updating pull request branch."})
        self.status: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str, str, bytes | None]] = []

    def add_pull(self, number: int, *, hours_idle: float = 72, head: str = HEAD, labels=(),
                 draft: bool = False, mergeable: bool | None = True, state: str = "clean",
                 branch: str | None = None, ahead_by: int = 0, base_files=(), head_files=(),
                 fork: bool = False) -> None:
        repo = f"{OWNER}/{REPO}"
        self.pulls[number] = {
            "number": number, "title": f"pull {number}", "draft": draft,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "updated_at": _iso(NOW - timedelta(hours=hours_idle)),
            "created_at": _iso(NOW - timedelta(days=10)),
            "labels": [{"name": label} for label in labels],
            "head": {"ref": branch or f"feature-{number}", "sha": head,
                     "repo": {"full_name": "someone/fork" if fork else repo}},
            "base": {"ref": "main", "repo": {"full_name": repo}},
            "body": "",
            "mergeable": mergeable, "mergeable_state": state,
        }
        self.compares[(head, "main")] = {"ahead_by": ahead_by,
                                         "files": [{"filename": f} for f in base_files]}
        self.compares[("main", head)] = {"ahead_by": 1, "files": [{"filename": f} for f in head_files]}

    def labels(self, number: int) -> list[str]:
        return [label["name"] for label in self.pulls[number]["labels"]]

    def writes(self) -> list[tuple[str, str]]:
        return [(m, urlparse(u).path) for m, u, _ in self.calls if m != "GET"]

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, body))
        assert urlparse(url).hostname == "api.github.com"
        parsed = urlparse(url)
        path = parsed.path.split(f"/repos/{OWNER}/{REPO}/", 1)[-1]
        query = parse_qs(parsed.query)
        refused = self.status.get((method, path.split("/")[0]))
        if refused:
            return refused, b'{"message": "refused"}'

        def ok(data, status=200):
            return status, json.dumps(data).encode()

        if method == "GET" and path == "pulls":
            rows = sorted(self.pulls.values(), key=lambda p: p["updated_at"])
            page = int(query["page"][0])
            listed = [{k: v for k, v in p.items() if k not in ("mergeable", "mergeable_state")}
                      for p in rows[(page - 1) * 100: page * 100]]
            return ok(listed)
        if match := re.fullmatch(r"pulls/(\d+)", path):
            return ok(self.pulls[int(match.group(1))])
        if match := re.fullmatch(r"pulls/(\d+)/update-branch", path):
            status, data = self.update_answer
            return ok(data, status)
        if match := re.fullmatch(r"compare/(.+)\.\.\.(.+)", path):
            return ok(self.compares[(unquote(match.group(1)), unquote(match.group(2)))])
        if match := re.fullmatch(r"issues/(\d+)/labels", path):
            number = int(match.group(1))
            for name in json.loads(body)["labels"]:
                self.pulls[number]["labels"].append({"name": name})
            return ok(self.pulls[number]["labels"])
        if match := re.fullmatch(r"issues/(\d+)/labels/(.+)", path):
            number, name = int(match.group(1)), unquote(match.group(2))
            before = self.pulls[number]["labels"]
            self.pulls[number]["labels"] = [l for l in before if l["name"] != name]
            return ok([]) if len(before) != len(self.pulls[number]["labels"]) else (404, b"{}")
        if match := re.fullmatch(r"issues/(\d+)/comments", path):
            number = int(match.group(1))
            if method == "POST":
                self.comments.setdefault(number, []).append(json.loads(body)["body"])
                return ok({"id": 7000 + len(self.comments[number]), "html_url": "",
                           "user": {"login": "swarm-bot"}}, 201)
            return ok([{"id": 7000 + i, "body": text, "user": {"login": "swarm-bot"}}
                       for i, text in enumerate(self.comments.get(number, []))])
        if re.fullmatch(r"rules/branches/.+", path):
            return ok([])
        if match := re.fullmatch(r"commits/([0-9a-f]+)/check-runs", path):
            runs = self.check_runs.get(match.group(1), [])
            return ok({"total_count": len(runs), "check_runs": runs})
        if re.fullmatch(r"commits/[0-9a-f]+/status", path):
            return ok({"state": "success", "statuses": []})
        return 404, b"{}"


@pytest.fixture
def gh() -> PullsGitHub:
    return PullsGitHub()


@pytest.fixture
def ctx(db, tokens, group_map, objects, gh, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    monkeypatch.delenv("REFUSAL_PR_SHEPHERD_FORGE_REFUSED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, group_map, objects, writes=gh)


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def shepherd(db, repo_id, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("max_concurrent", 12)
    return make_schedule(db, type_="pr-shepherd", repo_ids=(repo_id,), cron="*/30 * * * *", **kwargs)


def item(work, name) -> dict[str, Any]:
    (found,) = [w for w in work if w.get("kind") == "api_action" and w["id"] == name]
    return found


def seed_issue_run(db, *, tenant_id="eng", number=5, state=RunState.DONE, pr_task_id=INTEGRATOR):
    run = IssueRun(
        id=f"run_{tenant_id}_{number}", tenant_id=tenant_id, created_by=ALICE,
        created_at=NOW - timedelta(days=3), updated_at=NOW - timedelta(days=3), state=state,
        issue=IssueRef(owner=OWNER, repo=REPO, number=41), plan_approval="auto",
        auto_merge=False, fix_rounds=2, planner_task_id="task_" + "ef" * 10,
        pull_request={"number": number, "url": f"https://github.com/{OWNER}/{REPO}/pull/{number}"},
        pr_task_id=pr_task_id,
    )
    IssueRuns(db, now=lambda: NOW).create(run)
    seed_task(db, task_id=pr_task_id, tenant_id=tenant_id, state="SUCCEEDED", runner_profile="claude-code")
    db.docs[f"tasks/{pr_task_id}"]["repository_url"] = f"https://github.com/{OWNER}/{REPO}"
    db.docs[f"tasks/{pr_task_id}"]["metadata"] = {
        "dispatch": {"strategy": "integrate", "carrier": "branches", "role": "integrator",
                     "integrates": []},
    }
    return run


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("pr-shepherd")
    assert scheduletypes.availability(entry) == (True, "")
    assert scheduletypes.budget_caps(entry) == pr_shepherd.BUDGET_CAPS
    assert schedulefire.load_executor(entry) is pr_shepherd


def test_its_refusal_ships_switched_off() -> None:
    switch = refusals.SWITCHES[pr_shepherd.PrShepherdForgeRefused.code]
    assert switch.default is False
    assert refusals.enforced(switch.code, environ={}) is False


# ---------------------------------------------------------------------------
# Which pull requests, and their labels
# ---------------------------------------------------------------------------


def test_a_stale_red_pull_request_is_labelled_and_a_fresh_one_untouched(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("ready",))
    gh.check_runs[HEAD] = [{"name": "unit", "status": "completed", "conclusion": "failure"}]
    gh.add_pull(2, hours_idle=1, labels=("ready",), head=NEW_HEAD)

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    # The default policy: `stale` adds stale, `ci_red` removes ready.
    assert gh.labels(1) == ["stale"]
    assert gh.labels(2) == ["ready"]
    (pull,) = item(work, "actions")["detail"]["pulls"]
    assert pull["pull"] == f"{OWNER}/{REPO}#1" and pull["conditions"]["ci_red"] is True
    assert pull["labels_added"] == ["stale"] and pull["labels_removed"] == ["ready"]
    assert item(work, "actions")["outcome"] == "succeeded"
    assert not [w for w in work if w["id"] == "refusals"]


def test_only_changes_are_written(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",))

    pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    assert gh.writes() == []


def test_drafts_and_held_pull_requests_are_passed_over(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, draft=True)
    gh.add_pull(2, labels=("Hold",))

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    assert gh.writes() == []
    assert item(work, "actions")["detail"]["passed_over"] == [
        {"pull": f"{OWNER}/{REPO}#1", "reason": "draft"},
        {"pull": f"{OWNER}/{REPO}#2", "reason": "held"},
    ]


def test_no_more_than_max_prs_per_firing_are_examined(db, ctx, gh, repo_id) -> None:
    for number in (1, 2, 3):
        gh.add_pull(number, hours_idle=100 - number, head=str(number) * 40)

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id, params={"max_prs_per_firing": 2})))

    detail = item(work, "actions")["detail"]
    assert [p["pull"] for p in detail["pulls"]] == [f"{OWNER}/{REPO}#1", f"{OWNER}/{REPO}#2"]
    assert detail["passed_over"] == [{"pull": f"{OWNER}/{REPO}#3", "reason": "max_prs_per_firing"}]
    assert detail["truncated"] is True


# ---------------------------------------------------------------------------
# Update-branch
# ---------------------------------------------------------------------------


def test_a_behind_mergeable_pull_request_is_updated_pinned_to_its_head(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), ahead_by=3, base_files=("src/a.py",))

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    (put,) = [c for c in gh.calls if c[0] == "PUT"]
    assert urlparse(put[1]).path.endswith("/pulls/1/update-branch")
    assert json.loads(put[2]) == {"expected_head_sha": HEAD}
    (pull,) = item(work, "actions")["detail"]["pulls"]
    assert pull["update_branch"]["outcome"] == "updated"
    assert pull["update_branch"]["base_changes_workflows"] is False


def test_a_pull_request_that_is_not_behind_is_not_updated(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), ahead_by=0)

    pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    assert gh.writes() == []


def test_update_branch_off_never_calls_it(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), ahead_by=3)

    pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id, params={"update_branch": False})))

    assert gh.writes() == []


def test_a_refusal_over_workflow_files_is_reported_with_the_permission(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), ahead_by=2, base_files=(".github/workflows/ci.yml",))
    gh.update_answer = (422, {"message": "Resource not accessible by integration"})

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    refused = item(work, "refusals")
    assert refused["outcome"] == "failed"
    (row,) = refused["detail"]["refused"]
    assert row["action"] == "update_branch" and row["code"] == "update_branch_refused"
    assert row["needs"] == "workflows: write" and row["base_changes_workflows"] is True
    assert row["status"] == 422 and row["github_message"] == "Resource not accessible by integration"
    assert "workflows: write" in row["message"] and "§0.1 check 2" in row["message"]
    # The firing did something and was refused something: `partial` (§2.9).
    assert schedulefire.combine([w["outcome"] for w in work], independent=len(work) > 1) == "partial"


def test_a_refused_update_is_not_retried_at_the_same_head_and_base(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), ahead_by=2, base_files=(".github/workflows/ci.yml",))
    gh.update_answer = (403, {"message": "refused"})
    schedule = shepherd(db, repo_id)
    first = pr_shepherd.create(firing_for(ctx, schedule))
    db.docs[f"schedule_firings/{SCHEDULE_ID}:first"] = {
        "firing_id": f"{SCHEDULE_ID}:first", "schedule_id": SCHEDULE_ID, "tenant_id": "eng",
        "fired_at": NOW, "state": "done", "work": first,
    }
    gh.calls.clear()

    again = pr_shepherd.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert [c for c in gh.calls if c[0] == "PUT"] == []
    (pull,) = item(again, "actions")["detail"]["pulls"]
    assert pull["passed_over"][0]["reason"].startswith("refused_before")
    assert not [w for w in again if w["id"] == "refusals"]

    # The base moved on: a new reading, so GitHub is asked again.
    gh.compares[(HEAD, "main")]["ahead_by"] = 3
    pr_shepherd.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:third"))
    assert len([c for c in gh.calls if c[0] == "PUT"]) == 1


def test_a_head_that_moved_is_not_a_refusal(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), ahead_by=2)
    gh.update_answer = (422, {"message": "expected head sha didn't match current head ref."})

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    assert not [w for w in work if w["id"] == "refusals"]
    (pull,) = item(work, "actions")["detail"]["pulls"]
    assert pull["update_branch"]["outcome"] == "head_moved"


@pytest.mark.parametrize(("status", "message", "outcome"), [
    (202, "Updating pull request branch.", "updated"),
    (422, "merge conflict between base and head", "conflict"),
    (422, "expected head sha didn't match current head ref.", "head_moved"),
    (422, "Resource not accessible by integration", "refused"),
    (422, "", "refused"),
    (403, "Resource not accessible by integration", "refused"),
    (404, "Not Found", "refused"),
])
def test_update_branch_answers_are_classified_without_guessing(status, message, outcome) -> None:
    assert pr_shepherd.classify_update(status, message) == outcome


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_a_conflict_gets_one_comment_per_head_naming_both_sides_files(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("stale",), mergeable=False, state="dirty",
                base_files=("src/a.py", "src/b.py"), head_files=("src/b.py", "src/c.py"))
    schedule = shepherd(db, repo_id)

    pr_shepherd.create(firing_for(ctx, schedule))
    work = pr_shepherd.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    (comment,) = gh.comments[1]
    assert comment.startswith(pr_shepherd.conflict_marker(HEAD))
    assert "`src/b.py`" in comment and "src/a.py" not in comment and "src/c.py" not in comment
    assert "does not say which files conflict" in comment
    (pull,) = item(work, "actions")["detail"]["pulls"]
    assert pull["conflict_comment"] == "already_posted"
    assert not [c for c in gh.calls if c[0] == "PUT"]
    assert tasks(db) == []


def test_an_issue_runs_conflict_gets_a_fix_continuing_its_integrator(db, ctx, gh, repo_id) -> None:
    run = seed_issue_run(db)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}",
                base_files=("src/b.py",), head_files=("src/b.py",))
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})
    firing = firing_for(ctx, schedule)

    work = pr_shepherd.create(firing)

    (fix,) = [w for w in work if w["kind"] == "workflow"]
    (task,) = [t for t in tasks(db) if t["id"] != INTEGRATOR]
    assert task["workflow_id"] == fix["id"]
    # By NAME (invariant 10), as the schedule's owner, marked, waiting for admission.
    assert task["runner_profile"] == "claude-code" and task["submitted_by"] == ALICE
    assert task["metadata"]["schedule"] == firing.mark
    assert task["metadata"]["dispatch"]["continues"] == INTEGRATOR
    assert task["metadata"]["merge"] == "off"
    assert task["metadata"]["pr_shepherd"] == {
        "run_id": run.id, "repository": f"{OWNER}/{REPO}", "pull_request": 5,
        "head_sha": HEAD, "base_ref": "main",
    }
    assert task["state"] in ("QUEUED", "READY") and task.get("current_lease_id") is None
    assert "src/b.py" in task["input"]["prompt"] and "merge it into this branch" in task["input"]["prompt"]
    assert 5 not in gh.comments
    assert item(work, "actions")["detail"]["conflict_fixes"] == [f"{OWNER}/{REPO}#5"]

    # The same head again: no second fix.
    again = pr_shepherd.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))
    assert not [w for w in again if w["kind"] == "workflow"]


def test_the_default_run_gate_holds_the_fix_and_comments_instead(db, ctx, gh, repo_id) -> None:
    seed_issue_run(db)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}")
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True})
    assert schedule["gate"]["run"] == "approve"

    work = pr_shepherd.create(firing_for(ctx, schedule))

    assert not [w for w in work if w["kind"] == "workflow"]
    assert len(gh.comments[5]) == 1
    (pull,) = item(work, "actions")["detail"]["pulls"]
    assert pull["passed_over"][0]["reason"].startswith("run_gate")


def test_an_approved_firing_may_fix(db, ctx, gh, repo_id) -> None:
    seed_issue_run(db)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}")
    firing = firing_for(ctx, shepherd(db, repo_id, params={"fix_conflicts": True}))
    firing.firing = {**firing.firing, "run_approval": {"by": ALICE}}

    work = pr_shepherd.create(firing)

    assert [w for w in work if w["kind"] == "workflow"]


def test_another_tenants_issue_run_is_never_continued(db, ctx, gh, repo_id) -> None:
    seed_issue_run(db, tenant_id="research", pr_task_id=OTHER_TASK)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{OTHER_TASK}")
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})

    work = pr_shepherd.create(firing_for(ctx, schedule))

    assert not [w for w in work if w["kind"] == "workflow"]
    assert len(gh.comments[5]) == 1


def test_a_run_in_a_ci_fix_round_is_left_to_it(db, ctx, gh, repo_id) -> None:
    seed_issue_run(db, state=RunState.FIXING)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}")
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})

    work = pr_shepherd.create(firing_for(ctx, schedule))

    assert not [w for w in work if w["kind"] == "workflow"] and 5 not in gh.comments


CI_FIX_TASK = "task_" + "9a" * 10


def seed_ci_fix(db, *, tenant_id="eng", state="RUNNING", pull_request=5, continues=INTEGRATOR):
    """A standalone CI fix (scripts/ci-fix.sh) continuing the run's integrator."""
    seed_task(db, task_id=CI_FIX_TASK, tenant_id=tenant_id, state=state,
              runner_profile="claude-code", workflow_id="wf_" + "9a" * 4)
    db.docs[f"tasks/{CI_FIX_TASK}"]["metadata"] = {
        "dispatch": {"strategy": "direct-pr", "continues": continues},
        "ci_fix": {"pull_request": pull_request, "attempt": 1, "max_attempts": 2,
                   "postback": "pending"},
    }


def _conflict_fixes(db) -> list[dict[str, Any]]:
    return [t for t in tasks(db) if "pr_shepherd" in (t.get("metadata") or {})]


def test_a_live_ci_fix_on_the_pull_request_holds_the_conflict_fix(db, ctx, gh, repo_id) -> None:
    """#2456, measured 2026-10-26 on PR #2381: a CI fix (task_2f5b70aa, 01:21Z)
    and this type's conflict fix (task_2239a847, 01:23Z) both cloned 15cc7ea;
    the CI fix pushed first and the conflict fix's push was rejected."""
    seed_issue_run(db)
    seed_ci_fix(db)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}")
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})

    work = pr_shepherd.create(firing_for(ctx, schedule))

    # MUTATION: drop the live-writer read from `_plan_one` -- a second writer
    # clones the branch the CI fix is about to push.
    assert not [w for w in work if w["kind"] == "workflow"]
    assert _conflict_fixes(db) == []
    # MUTATION: leave plan.conflict at "comment" -- the shepherd comments on a
    # pull request a fix is already on.
    assert 5 not in gh.comments
    (pull,) = item(work, "actions")["detail"]["pulls"]
    reasons = [p["reason"] for p in pull["passed_over"] if p["action"] == "conflict_fix"]
    assert len(reasons) == 1 and reasons[0].startswith("fix_live"), pull["passed_over"]
    assert CI_FIX_TASK in reasons[0]


def test_a_terminal_ci_fix_does_not_hold_the_conflict_fix(db, ctx, gh, repo_id) -> None:
    seed_issue_run(db)
    seed_ci_fix(db, state="SUCCEEDED")
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}")
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})

    work = pr_shepherd.create(firing_for(ctx, schedule))

    # MUTATION: count any ci_fix task as live -- a finished fix holds the
    # conflict fix for ever.
    assert [w for w in work if w["kind"] == "workflow"]
    assert len(_conflict_fixes(db)) == 1


@pytest.mark.parametrize("where", [{"pull_request": 6}, {"tenant_id": "research"}],
                         ids=["another_pull_request", "another_tenant"])
def test_a_ci_fix_on_another_pull_request_or_tenant_does_not_hold_it(db, ctx, gh, repo_id, where) -> None:
    seed_issue_run(db)
    seed_ci_fix(db, **where)
    gh.add_pull(5, labels=("stale",), mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}")
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})

    work = pr_shepherd.create(firing_for(ctx, schedule))

    # MUTATION: drop the pull-request check, or the tenant filter (invariant
    # 9) -- another pull request's or tenant's fix holds this one.
    assert [w for w in work if w["kind"] == "workflow"]
    assert len(_conflict_fixes(db)) == 1


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


@pytest.fixture
def no_token_ctx(db, tokens, group_map, objects, gh, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    missing = forge_fakes.AnyTenantTokens(missing=("swarm-tenant-eng-git",))
    return make_context(db, tokens, group_map, objects, writes=gh, forge_tokens=missing)


def test_no_credential_is_reported_as_a_failed_refusal_while_the_switch_is_off(db, no_token_ctx, gh, repo_id, monkeypatch) -> None:
    monkeypatch.delenv("REFUSAL_PR_SHEPHERD_FORGE_REFUSED", raising=False)
    gh.add_pull(1)

    work = pr_shepherd.create(firing_for(no_token_ctx, shepherd(db, repo_id)))

    assert [w["id"] for w in work] == ["refusals"] and work[0]["outcome"] == "failed"
    assert gh.calls == []


def test_no_credential_refuses_the_firing_when_the_switch_is_on(db, no_token_ctx, gh, repo_id, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_PR_SHEPHERD_FORGE_REFUSED", "on")

    with pytest.raises(pr_shepherd.PrShepherdForgeRefused) as caught:
        pr_shepherd.create(firing_for(no_token_ctx, shepherd(db, repo_id)))

    assert "pull_requests: read" in caught.value.message


def test_a_refused_listing_names_the_permission(db, ctx, gh, repo_id) -> None:
    gh.status[("GET", "pulls")] = 403

    work = pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id)))

    (row,) = item(work, "refusals")["detail"]["refused"]
    assert row["action"] == "list_pulls" and row["needs"] == "pull_requests: read"
    assert row["code"] == "writeback_forbidden"


# ---------------------------------------------------------------------------
# Room and the dry run
# ---------------------------------------------------------------------------


def test_no_room_makes_nothing(db, ctx, gh, repo_id) -> None:
    gh.add_pull(1, labels=("ready",))

    assert pr_shepherd.create(firing_for(ctx, shepherd(db, repo_id), room=0)) == []
    assert gh.calls == []


def test_the_dry_run_writes_nothing(db, ctx, gh, repo_id) -> None:
    seed_issue_run(db)
    gh.add_pull(1, labels=("ready",), ahead_by=2)
    gh.add_pull(5, mergeable=False, state="dirty", branch=f"swarm/{INTEGRATOR}", head=NEW_HEAD)
    schedule = shepherd(db, repo_id, params={"fix_conflicts": True}, gate={"run": "auto"})
    before = dict(db.docs)

    out = pr_shepherd.dry_run(firing_for(ctx, schedule))

    assert gh.writes() == [] and db.docs == before
    by_pull = {row["pull"]: row for row in out["would"]}
    assert by_pull[f"{OWNER}/{REPO}#1"]["labels_add"] == ["stale"]
    assert by_pull[f"{OWNER}/{REPO}#1"]["update_branch"] is True
    assert by_pull[f"{OWNER}/{REPO}#5"]["conflict"] == "fix"
    assert out["fixes_allowed"] is True
