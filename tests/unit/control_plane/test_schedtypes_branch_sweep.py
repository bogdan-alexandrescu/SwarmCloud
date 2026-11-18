"""The `branch-sweep` schedule type (docs/schedules.md §3.19).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT: its file exists, so the catalogue serves it available
  with the caps its module declares, and the tick's loader imports it.
* THE OWNER'S RULE: a fake forge with a branch of every kind deletes exactly
  the ones whose pull request merged and whose head is that pull request's
  head. An open pull request, a protected branch, the default branch, a
  `keep/` branch, a fork's same-named pull request, a branch a live task
  reads: never deleted.
* `dry_run` deletes nothing and reports the same branches.
* THE CAP stops at `max_deletes` and says so.
* A delete an earlier firing recorded as failed is retried first.
* THE TOKEN never reaches the report.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from swarm_api import forgewrite, schedulefire, scheduletypes
from swarm_api.schedtypes import branch_sweep

from . import forge_fakes
from .conftest import seed_task, seed_tenant
from .test_schedtypes_issue_sweep import (
    NOW,
    OWNER,
    REPO,
    firing_for,
    make_context,
    make_schedule,
    register,
)

FULL = f"{OWNER}/{REPO}"


def sha(n: int) -> str:
    return f"{n:040x}"


class BranchesGitHub:
    """api.github.com's branch and pull-request routes this type calls, in memory."""

    def __init__(self) -> None:
        #: name -> (sha, protected)
        self.branches: dict[str, tuple[str, bool]] = {}
        self.pulls: list[dict[str, Any]] = []
        self.refuse_delete: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def branch(self, name: str, n: int, *, protected: bool = False) -> None:
        self.branches[name] = (sha(n), protected)

    def pull(self, branch: str, n: int, *, state: str = "closed", merged: bool = False,
             fork: bool = False) -> None:
        self.pulls.append({
            "number": len(self.pulls) + 1, "state": state,
            "merged_at": "2026-11-01T00:00:00Z" if merged else None,
            "head": {"ref": branch, "sha": sha(n),
                     "repo": {"full_name": "someone/fork" if fork else FULL}},
        })

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        assert parsed.hostname == "api.github.com"
        self.calls.append((method, parsed.path))
        query = parse_qs(parsed.query)

        def ok(data, status=200):
            return status, json.dumps(data).encode()

        if method == "GET" and parsed.path == f"/repos/{FULL}":
            return ok({"default_branch": "main"})
        path = parsed.path.split(f"/repos/{FULL}/", 1)[-1]
        page = int(query.get("page", ["1"])[0])
        per = int(query.get("per_page", ["100"])[0])
        if method == "GET" and path == "branches":
            rows = [{"name": n, "commit": {"sha": s}, "protected": p}
                    for n, (s, p) in sorted(self.branches.items())]
            return ok(rows[(page - 1) * per: page * per])
        if method == "GET" and path == "pulls":
            state = query["state"][0]
            rows = [p for p in self.pulls if state == "all" or p["state"] == state]
            return ok(rows[(page - 1) * per: page * per])
        if method == "DELETE" and path.startswith("git/refs/heads/"):
            name = unquote(path[len("git/refs/heads/"):])
            if name in self.refuse_delete:
                return self.refuse_delete[name], b'{"message": "refused"}'
            if name not in self.branches:
                return 404, b"{}"
            del self.branches[name]
            self.deleted.append(name)
            return 204, b""
        return 404, b"{}"


@pytest.fixture
def gh() -> BranchesGitHub:
    return BranchesGitHub()


@pytest.fixture
def ctx(db, tokens, group_map, objects, gh, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    return make_context(db, tokens, group_map, objects, writes=gh)


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def schedule(db, repo_id, **params: Any) -> dict[str, Any]:
    return make_schedule(db, type_="branch-sweep", repo_ids=(repo_id,), params=params,
                         cron="0 6 * * *", max_concurrent=2)


def item(work, name) -> dict[str, Any]:
    (found,) = [w for w in work if w.get("kind") == "api_action" and w["id"] == name]
    return found


def every_kind(gh: BranchesGitHub) -> None:
    gh.branch("main", 1, protected=False)
    gh.pull("main", 1, merged=True)                      # a merged PR from the default branch
    gh.branch("merged-a", 2)
    gh.pull("merged-a", 2, merged=True)                  # merged, head unchanged: DELETE
    gh.branch("swarm/task_aa11", 3)
    gh.pull("swarm/task_aa11", 3, merged=True)           # merged, head unchanged: DELETE
    gh.branch("moved", 4)
    gh.pull("moved", 40, merged=True)                    # pushed to after the merge
    gh.branch("closed", 5)
    gh.pull("closed", 5)                                 # closed unmerged
    gh.branch("lonely", 6)                               # no pull request
    gh.branch("reopened", 7)
    gh.pull("reopened", 7, merged=True)
    gh.pull("reopened", 7, state="open")                 # an open pull request as well
    gh.branch("guarded", 8, protected=True)
    gh.pull("guarded", 8, merged=True)                   # protected
    gh.branch("keep/release", 9)
    gh.pull("keep/release", 9, merged=True)              # under keep/
    gh.branch("forked", 10)
    gh.pull("forked", 10, merged=True, fork=True)        # a fork's same-named branch merged


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("branch-sweep")
    assert scheduletypes.availability(entry) == (True, "")
    assert scheduletypes.budget_caps(entry) == branch_sweep.BUDGET_CAPS
    assert schedulefire.load_executor(entry) is branch_sweep
    row = scheduletypes.describe(entry)
    props = row["params_schema"]["properties"]
    assert props["max_deletes"]["default"] == 300 and props["dry_run"]["default"] is False
    assert props["repositories"]["default"] == []


def test_parameters_refuse_what_is_not_a_repository() -> None:
    model = scheduletypes.get("branch-sweep").params_model
    with pytest.raises(ValueError):
        model.model_validate({"repositories": ["../etc"]})
    with pytest.raises(ValueError):
        model.model_validate({"max_deletes": 0})
    with pytest.raises(ValueError):
        model.model_validate({"image": "x"})


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_only_merged_and_unchanged_branches_are_deleted(db, ctx, gh, repo_id) -> None:
    every_kind(gh)

    work = branch_sweep.create(firing_for(ctx, schedule(db, repo_id)))

    assert sorted(gh.deleted) == ["merged-a", "swarm/task_aa11"]
    report = item(work, "report")
    assert report["outcome"] == "succeeded"
    detail = report["detail"]
    assert detail["deleted"] == {"count": 2, "names": [f"{FULL}:merged-a", f"{FULL}:swarm/task_aa11"]}
    kept = {k: v["count"] for k, v in detail["kept"].items() if v["count"]}
    assert kept == {"no_pull_request": 2, "closed_unmerged": 1, "head_moved_after_merge": 1,
                    "open_pull_request": 1, "protected_or_keep": 3}
    assert detail["kept"]["no_pull_request"]["names"] == [f"{FULL}:forked", f"{FULL}:lonely"]
    assert not [w for w in work if w["id"] == "refusals"]


def test_an_open_pull_request_or_a_protected_branch_is_never_deleted(db, ctx, gh, repo_id) -> None:
    every_kind(gh)

    branch_sweep.create(firing_for(ctx, schedule(db, repo_id, max_deletes=500)))

    deletes = [p for m, p in gh.calls if m == "DELETE"]
    for name in ("main", "reopened", "guarded", "keep/release", "moved", "closed", "lonely", "forked"):
        assert name in gh.branches
        assert not [p for p in deletes if p.endswith(f"/heads/{name}")]


def test_a_branch_a_live_task_reads_is_kept(db, ctx, gh, repo_id) -> None:
    gh.branch("swarm/task_bb22", 2)
    gh.pull("swarm/task_bb22", 2, merged=True)
    gh.branch("swarm/task_cc33", 3)
    gh.pull("swarm/task_cc33", 3, merged=True)
    gh.branch("swarm/task_dd44", 4)
    gh.pull("swarm/task_dd44", 4, merged=True)
    # A queued step hands off from task_bb22; a running continuation continues task_cc33.
    seed_task(db, task_id="task_ee55", tenant_id="eng", state="QUEUED", depends_on=("task_bb22",))
    seed_task(db, task_id="task_ff66", tenant_id="eng", state="RUNNING")
    db.docs["tasks/task_ff66"]["metadata"] = {"dispatch": {"continues": "task_cc33"}}
    # Ended work reads nothing, and another tenant's live task names nothing here.
    seed_task(db, task_id="task_9999", tenant_id="eng", state="SUCCEEDED", depends_on=("task_dd44",))
    seed_tenant(db, "research")
    seed_task(db, task_id="task_8888", tenant_id="research", state="RUNNING", depends_on=("task_dd44",))

    work = branch_sweep.create(firing_for(ctx, schedule(db, repo_id)))

    assert gh.deleted == ["swarm/task_dd44"]
    assert item(work, "report")["detail"]["kept"]["in_use"]["count"] == 2


# ---------------------------------------------------------------------------
# dry_run, the cap, failures
# ---------------------------------------------------------------------------


def test_dry_run_deletes_nothing_and_reports_the_same(db, ctx, gh, repo_id) -> None:
    every_kind(gh)
    real = BranchesGitHub()
    every_kind(real)

    dry = item(branch_sweep.create(firing_for(ctx, schedule(db, repo_id, dry_run=True))), "report")

    assert gh.deleted == [] and not [c for c in gh.calls if c[0] != "GET"]
    ctx.forge_writer = forgewrite.GitHubWriter(send=real)
    wet = item(branch_sweep.create(firing_for(ctx, schedule(db, repo_id))), "report")
    assert dry["detail"]["would_delete"] == wet["detail"]["deleted"]
    assert dry["detail"]["kept"] == wet["detail"]["kept"]
    assert "deleted" not in dry["detail"] and dry["detail"]["dry_run"] is True


def test_the_firings_own_dry_run_writes_nothing(db, ctx, gh, repo_id) -> None:
    every_kind(gh)

    out = branch_sweep.dry_run(firing_for(ctx, schedule(db, repo_id)))

    assert gh.deleted == [] and out["would_delete"]["count"] == 2


def test_the_cap_stops_at_max_deletes_and_says_so(db, ctx, gh, repo_id) -> None:
    for n in range(5):
        gh.branch(f"merged-{n}", 100 + n)
        gh.pull(f"merged-{n}", 100 + n, merged=True)

    work = branch_sweep.create(firing_for(ctx, schedule(db, repo_id, max_deletes=3)))

    assert gh.deleted == ["merged-0", "merged-1", "merged-2"]
    detail = item(work, "report")["detail"]
    assert detail["deleted"]["count"] == 3
    assert detail["capped"] is True and "max_deletes (3)" in detail["capped_message"]
    assert detail["kept"]["deferred"] == {"count": 2, "names": [f"{FULL}:merged-3", f"{FULL}:merged-4"]}


def test_branches_and_pulls_are_read_past_one_page(db, ctx, gh, repo_id) -> None:
    for n in range(130):
        gh.branch(f"b-{n:03d}", 1000 + n)
        gh.pull(f"b-{n:03d}", 1000 + n, merged=True)

    work = branch_sweep.create(firing_for(ctx, schedule(db, repo_id)))

    assert len(gh.deleted) == 130
    assert item(work, "report")["detail"]["repositories"][0]["truncated"] == []


def test_unread_open_pull_requests_delete_nothing(db, ctx, gh, repo_id, monkeypatch) -> None:
    every_kind(gh)
    # The budget ends inside the open pull requests' read.
    monkeypatch.setattr(branch_sweep, "MAX_REQUESTS", 1)

    work = branch_sweep.create(firing_for(ctx, schedule(db, repo_id)))

    assert gh.deleted == []
    detail = item(work, "report")["detail"]
    assert detail["stopped"] == "request_budget"
    assert detail["repositories"][0]["truncated"]


def test_a_refused_delete_is_recorded_and_retried_first_next_time(db, ctx, gh, repo_id) -> None:
    for name, n in (("a-first", 1), ("z-last", 2)):
        gh.branch(name, n)
        gh.pull(name, n, merged=True)
    gh.refuse_delete["z-last"] = 422
    sched = schedule(db, repo_id)

    failed = branch_sweep.create(firing_for(ctx, sched, fid="f1"))

    assert gh.deleted == ["a-first"]
    assert item(failed, "refusals")["outcome"] == "failed"
    assert item(failed, "report")["detail"]["repositories"][0]["delete_failed"]["names"] == ["z-last"]
    db.docs["schedule_firings/f1"] = {"schedule_id": sched["schedule_id"], "tenant_id": "eng",
                                      "fired_at": NOW, "work": failed}
    gh.refuse_delete.clear()
    gh.deleted.clear()
    gh.branch("a-again", 4)
    gh.pull("a-again", 4, merged=True)

    work = branch_sweep.create(firing_for(ctx, dict(sched, params={"max_deletes": 1}), fid="f2"))

    # The failed delete goes first, ahead of a name that sorts before it.
    assert gh.deleted == ["z-last"]
    assert item(work, "report")["detail"]["kept"]["deferred"]["names"] == [f"{FULL}:a-again"]


def test_the_token_never_reaches_the_report(db, ctx, gh, repo_id, monkeypatch) -> None:
    token = secrets.token_hex(20)
    monkeypatch.setattr(forge_fakes.AnyTenantTokens, "token_for", lambda self, tenant, **kw: token)
    every_kind(gh)
    gh.branch(f"leak-{token}", 50)
    gh.pull(f"leak-{token}", 50, merged=True)
    gh.branch(f"kept-{token}", 51)
    gh.refuse_delete[f"leak-{token}"] = 403

    work = branch_sweep.create(firing_for(ctx, schedule(db, repo_id)))

    assert token not in json.dumps(work, default=str)
    assert item(work, "refusals")["detail"]["refused"][0]["needs"] == "contents: write"
