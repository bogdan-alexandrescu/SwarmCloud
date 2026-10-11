"""The `issue-sweep` schedule type: SWEEP's module as the executor (docs/schedules.md §3.1, §8.1, lane S6).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT. Its file exists, so the catalogue serves it available,
  and the tick's real loader (`schedulefire.load_executor`) imports it.
* SWEEP'S RULES, UNCHANGED, ARE THE TYPE'S. The executor's selection is
  `issuesweep.collect_candidates`: an outsider's issue, a `security`, `epic`
  or `blocked` label, a live run and an open pull request's claim each pass
  an issue over through the executor exactly as through the route. SWEEP's
  own file (`test_issue_sweep.py`) passes unchanged against the refactor.
* §3.1'S PARAMETERS: `labels_include`, `labels_exclude`, `max_new_per_firing`,
  `max_live_runs` (the TENANT's live runs, SWEEP's count), the NOT_READY
  cooldown, and `fix_rounds`.
* THE GATE DECIDES THE RUN (§4.2): `plan: approve` makes the plan wait,
  `merge: approve` makes the merge wait in the inbox, `merge: off` never
  merges, `merge: auto` merges unasked.
* AS WHOM AND MARKED (§2.7): the schedule's owner submits, the run records
  `schedule:<id>` as its creator and carries `metadata.schedule`.
* EXACTLY ONCE (§2.2): a retried firing adopts the run it made and does not
  start a second.
* DRY RUN (§2.8): lists what would start and why each other issue is passed
  over; creates nothing in Firestore and writes nothing to GitHub.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import forge, forgewrite, issuesweep, schedulefire, scheduletypes
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import AppContext, build_context
from swarm_api.errors import Forbidden
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRuns, RunState
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.schedtypes import issue_sweep
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, seed_tenant

OWNER, REPO = "saga-xyz", "widgets"
REPOSITORY = f"{OWNER}/{REPO}"
ALICE = "alice@saga.xyz"
SCHEDULE_ID = "sch_000000000001"

_now = datetime.now(timezone.utc)
#: A quarter-hour slot, ten seconds in: a `*/15` schedule is due at it.
NOW = _now.replace(minute=_now.minute - _now.minute % 15, second=10, microsecond=0)
SLOT = NOW.replace(second=0)


# ---------------------------------------------------------------------------
# Helpers the other test_schedtypes_* files share
# ---------------------------------------------------------------------------


def make_context(db, tokens, group_map, objects, *, github=None, writes=None, forge_tokens=None,
                 transport=None) -> AppContext:
    context = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens or forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=transport or github or forge_fakes.GitHub()),
        forge_writer=forgewrite.GitHubWriter(send=writes or forge_fakes.GitHubWrites()),
    )
    context.now = lambda: NOW
    return context


def register(db, *, tenant_id="eng", owner=OWNER, repo=REPO, **extra: Any) -> str:
    repo_id = repo_id_for(tenant_id, owner, repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "archived": False,
        "repository_url": f"https://github.com/{owner}/{repo}", "default_branch": "main",
        "created_by": ALICE, "created_at": NOW, "updated_at": NOW, **extra,
    }
    return repo_id


def make_schedule(db, *, type_: str, repo_ids: tuple[str, ...] = (), params: dict | None = None,
                  gate: dict | None = None, policy: dict | None = None, scope_mode: str = "repos",
                  schedule_id: str = SCHEDULE_ID, tenant_id: str = "eng", owner: str = ALICE,
                  cron: str = "*/15 * * * *", max_concurrent: int = 8) -> dict[str, Any]:
    entry = scheduletypes.get(type_)
    doc = {
        "schedule_id": schedule_id,
        "tenant_id": tenant_id,
        "name": schedule_id,
        "type": type_,
        "scope": ({"mode": "repos", "repo_ids": list(repo_ids)} if scope_mode == "repos"
                  else {"mode": scope_mode}),
        "cron": cron,
        "timezone": "UTC",
        "params": dict(params or {}),
        "gate": {**entry.default_gate.as_dict(), "approvers": "members", "approval_ttl_hours": 72,
                 **(gate or {})},
        "budget": {"per_run_usd": 15.0, "per_day_usd": 120.0, "max_concurrent": max_concurrent},
        "policy": {"overlap": "skip", "catch_up": "skip", "jitter": False, "dry_run": False,
                   **(policy or {})},
        "state": "enabled",
        "pause": None,
        "owner": owner,
        "created_by": owner,
        "created_at": NOW - timedelta(days=1),
        "updated_by": owner,
        "updated_at": NOW - timedelta(days=1),
        "next_run_at": SLOT,
        "next_slot": SLOT,
        "last_firing": None,
        "consecutive_failures": 0,
    }
    db.docs[f"schedules/{schedule_id}"] = doc
    return doc


def firing_for(ctx, schedule: dict[str, Any], *, room: int = 8, fid: str | None = None,
               recorded: list | None = None) -> schedulefire.Firing:
    fid = fid or schedulefire.firing_id(schedule["schedule_id"], SLOT)
    sink = recorded if recorded is not None else []
    return schedulefire.Firing(
        ctx=ctx, schedule=schedule, firing={"firing_id": fid, "slot": SLOT},
        owner=schedulefire.schedule_owner_auth(ctx, schedule),
        repo_ids=list((schedule.get("scope") or {}).get("repo_ids") or []),
        room=room, now=NOW, record=sink.append,
    )


def issue(number: int, *, minutes_ago: float = 60, labels: tuple[str, ...] = (),
          author: str = "MEMBER") -> dict[str, Any]:
    updated = NOW - timedelta(minutes=minutes_ago)
    return {
        "number": number, "title": f"issue {number}",
        "labels": [{"name": label} for label in labels],
        "updated_at": updated.isoformat().replace("+00:00", "Z"),
        "author_association": author,
    }


def issue_runs(db) -> list[dict[str, Any]]:
    return sorted((d for p, d in db.docs.items() if p.startswith("issue_runs/")),
                  key=lambda d: d["issue"]["number"])


def tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def firings(db) -> dict[str, dict[str, Any]]:
    return {p.split("/", 1)[1]: d for p, d in db.docs.items() if p.startswith("schedule_firings/")}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def github():
    return forge_fakes.GitHub(issues=[issue(42)], pulls=[], files={})


@pytest.fixture
def writes():
    return forge_fakes.GitHubWrites()


@pytest.fixture
def ctx(db, tokens, group_map, objects, github, writes, monkeypatch) -> AppContext:
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, group_map, objects, github=github, writes=writes)


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def sweep(db, repo_id, **kwargs: Any) -> dict[str, Any]:
    return make_schedule(db, type_="issue-sweep", repo_ids=(repo_id,), **kwargs)


def started(work: list[dict[str, Any]], db) -> list[int]:
    by_id = {d["id"]: d for d in issue_runs(db)}
    return [by_id[w["id"]]["issue"]["number"] for w in work]


# ---------------------------------------------------------------------------
# The type is built, and the tick's real loader finds it
# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("issue-sweep")
    assert scheduletypes.availability(entry) == (True, "")
    module = schedulefire.load_executor(entry)
    assert module is issue_sweep
    assert callable(module.create) and callable(module.dry_run)


def test_a_tick_fires_the_type_through_the_real_loader(db, ctx, repo_id) -> None:
    sweep(db, repo_id)

    report = schedulefire.Ticker(ctx).tick()

    assert report.fired == 1, report.to_api()
    (firing,) = firings(db).values()
    assert firing["state"] != "refused", firing
    (run,) = issue_runs(db)
    assert run["issue"]["number"] == 42
    assert [w["id"] for w in firing["work"]] == [run["id"]]
    assert firing["work"][0]["kind"] == "issue_run" and firing["work"][0]["repo_id"] == repo_id


# ---------------------------------------------------------------------------
# As whom, marked, and what the gate makes of the run
# ---------------------------------------------------------------------------


def test_the_run_is_the_owners_marked_and_follows_the_default_gate(db, ctx, repo_id) -> None:
    schedule = sweep(db, repo_id)
    firing = firing_for(ctx, schedule)

    work = issue_sweep.create(firing)

    (run,) = issue_runs(db)
    assert work == [{"kind": "issue_run", "id": run["id"], "repo_id": repo_id}]
    assert run["tenant_id"] == "eng"
    assert run["created_by"] == f"schedule:{SCHEDULE_ID}" and run["on_behalf_of"] == ALICE
    assert run["metadata"]["schedule"] == firing.mark
    # The §3 default gate: plan automatic, merge asks first.
    assert run["plan_approval"] == "auto"
    assert run["auto_merge"] is True and run["merge_approval"] == "required"
    assert run["fix_rounds"] == 3  # §3.1's default, not SWEEP's constant 2
    planner = db.docs[f"tasks/{run['planner_task_id']}"]
    assert planner["submitted_by"] == ALICE and planner["tenant_id"] == "eng"
    assert planner["state"] in ("QUEUED", "READY") and planner.get("current_lease_id") is None


@pytest.mark.parametrize("gate, plan_approval, auto_merge, merge_approval", [
    ({"plan": "approve", "merge": "approve"}, "required", True, "required"),
    ({"plan": "auto", "merge": "off"}, "auto", False, None),
    ({"plan": "auto", "merge": "auto"}, "auto", True, None),
])
def test_the_gate_is_the_authority_for_each_run(db, ctx, repo_id, gate, plan_approval,
                                                auto_merge, merge_approval) -> None:
    issue_sweep.create(firing_for(ctx, sweep(db, repo_id, gate=gate)))

    (run,) = issue_runs(db)
    assert (run["plan_approval"], run["auto_merge"], run["merge_approval"]) == (
        plan_approval, auto_merge, merge_approval)


def test_fix_rounds_is_the_parameter(db, ctx, repo_id) -> None:
    issue_sweep.create(firing_for(ctx, sweep(db, repo_id, params={"fix_rounds": 5})))
    assert issue_runs(db)[0]["fix_rounds"] == 5


# ---------------------------------------------------------------------------
# SWEEP's rules, through the executor
# ---------------------------------------------------------------------------


def test_sweeps_skip_rules_pass_the_same_issues_over(db, ctx, repo_id, github) -> None:
    github.issues = [
        issue(42),                               # the control: a candidate
        issue(43, labels=("security",)),         # SKIP_LABELS, and §3.1's default exclude
        issue(44, labels=("epic",)),             # SKIP_LABELS
        issue(45, author="NONE"),                # an outsider's issue
        issue(46, labels=("wontfix",)),          # §3.1's default labels_exclude
        issue(47),                               # claimed by an open pull request
    ]
    github.pulls = [{"number": 9, "title": "Sort", "body": "Closes #47", "draft": False,
                     "head": {"ref": "sort"}, "user": {"login": "x"}}]
    schedule = sweep(db, repo_id)

    preview = issue_sweep.dry_run(firing_for(ctx, schedule))
    work = issue_sweep.create(firing_for(ctx, schedule))

    assert started(work, db) == [42]
    reasons = {row["issue"]: row["reason"] for row in preview["passed_over"]}
    assert reasons == {
        f"{REPOSITORY}#43": "label: security",
        f"{REPOSITORY}#44": "label: epic",
        f"{REPOSITORY}#45": "author: NONE",
        f"{REPOSITORY}#46": "excluded: label wontfix",
        f"{REPOSITORY}#47": "open_pull_request: #9",
    }


def test_an_issue_with_a_live_run_is_not_started_twice(db, ctx, repo_id) -> None:
    schedule = sweep(db, repo_id)
    issue_sweep.create(firing_for(ctx, schedule))

    again = issue_sweep.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:other"))

    assert again == [] and len(issue_runs(db)) == 1


def test_labels_include_takes_only_issues_carrying_one(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1, labels=("Ready",)), issue(2, labels=("bug",)), issue(3)]
    schedule = sweep(db, repo_id, params={"labels_include": ["ready"]})

    work = issue_sweep.create(firing_for(ctx, schedule))

    assert started(work, db) == [1]


def test_security_stays_skipped_when_a_member_drops_it_from_labels_exclude(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1, labels=("security",)), issue(2)]
    schedule = sweep(db, repo_id, params={"labels_exclude": []})

    work = issue_sweep.create(firing_for(ctx, schedule))

    assert started(work, db) == [2]


# ---------------------------------------------------------------------------
# The room: max_new_per_firing, max_live_runs, max_concurrent
# ---------------------------------------------------------------------------


def test_max_new_per_firing_starts_the_oldest_first(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 6)]
    schedule = sweep(db, repo_id, params={"max_new_per_firing": 2})

    preview = issue_sweep.dry_run(firing_for(ctx, schedule))
    work = issue_sweep.create(firing_for(ctx, schedule))

    assert started(work, db) == [1, 2]
    assert [row["issue"] for row in preview["would_start"]] == [f"{REPOSITORY}#1", f"{REPOSITORY}#2"]
    assert preview["passed_over_by_reason"] == {"room": 3}
    assert preview["limits"]["max_new_per_firing"] == 2


def test_max_live_runs_counts_every_live_run_of_the_tenant(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 6)]
    # Another schedule's run holds a tenant slot too, as a person's does.
    other = sweep(db, repo_id, params={"max_new_per_firing": 1}, schedule_id="sch_000000000002")
    issue_sweep.create(firing_for(ctx, other))
    schedule = sweep(db, repo_id, params={"max_live_runs": 3, "max_new_per_firing": 8})

    work = issue_sweep.create(firing_for(ctx, schedule))

    assert len(work) == 2 and len(issue_runs(db)) == 3


def test_an_ended_run_frees_its_place(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 4)]
    schedule = sweep(db, repo_id, params={"max_live_runs": 1})
    (first,) = issue_sweep.create(firing_for(ctx, schedule))
    IssueRuns(db).transition("eng", first["id"], RunState.CANCELLED, by="t")

    second = issue_sweep.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))

    assert started(second, db) == [2]


def test_the_firing_never_starts_past_its_room(db, ctx, repo_id, github) -> None:
    github.issues = [issue(n, minutes_ago=100 - n) for n in range(1, 6)]
    schedule = sweep(db, repo_id)

    work = issue_sweep.create(firing_for(ctx, schedule, room=1))

    assert len(work) == 1


def test_the_cap_of_eight_is_the_parameters_upper_bound() -> None:
    model = scheduletypes.get("issue-sweep").params_model
    with pytest.raises(Exception):
        model.model_validate({"max_live_runs": 9})
    assert model().max_live_runs == issuesweep.DEFAULT_MAX_LIVE_RUNS == 8


# ---------------------------------------------------------------------------
# The NOT_READY cooldown (§3.1)
# ---------------------------------------------------------------------------


def _ended_not_ready(db, number: int, *, hours_ago: float) -> None:
    at = NOW - timedelta(hours=hours_ago)
    run = issuesweep.IssueRun(
        id=f"run_old{number}", tenant_id="eng", created_by=ALICE, created_at=at, updated_at=at,
        state=RunState.NOT_READY, issue=issuesweep.IssueRef(owner=OWNER, repo=REPO, number=number),
        plan_approval="auto", auto_merge=False, fix_rounds=2, planner_task_id="t_old",
    )
    IssueRuns(db, now=lambda: at).create(run)


def test_an_unchanged_not_ready_issue_waits_out_the_cooldown_then_is_planned_again(
        db, ctx, repo_id, github) -> None:
    # Both issues were last edited before their NOT_READY verdict.
    github.issues = [issue(1, minutes_ago=60 * 80), issue(2, minutes_ago=60 * 80)]
    _ended_not_ready(db, 1, hours_ago=73)   # past the 72 h default
    _ended_not_ready(db, 2, hours_ago=10)   # inside it
    schedule = sweep(db, repo_id)

    preview = issue_sweep.dry_run(firing_for(ctx, schedule))
    work = issue_sweep.create(firing_for(ctx, schedule))

    assert started(work, db) == [1]
    reasons = {row["issue"]: row["reason"] for row in preview["passed_over"]}
    assert reasons == {f"{REPOSITORY}#2": "not_ready_unchanged: run_old2"}


def test_sweeps_own_route_keeps_not_ready_until_the_issue_changes() -> None:
    """The cooldown is the schedule's: SWEEP's call, with no cooldown, still waits."""
    at = NOW - timedelta(days=30)
    previous = issuesweep.IssueRun(
        id="run_prev", tenant_id="eng", created_by=ALICE, created_at=at, updated_at=at,
        state=RunState.NOT_READY, issue=issuesweep.IssueRef(owner=OWNER, repo=REPO, number=7),
        plan_approval="auto", auto_merge=True, fix_rounds=2, planner_task_id="t1",
    )
    old = forge.SweepIssue(number=7, title="t", labels=(), updated_at=at - timedelta(hours=1),
                           author_association="MEMBER")
    kwargs = dict(repository=REPOSITORY, config=issuesweep.SweepConfig(enabled=True), live={},
                  claimed={}, last_run=lambda number: previous)
    assert issuesweep.skip_reason(old, **kwargs) == "not_ready_unchanged: run_prev"
    assert issuesweep.skip_reason(old, **kwargs, not_ready_cooldown=timedelta(hours=72), now=NOW) is None


# ---------------------------------------------------------------------------
# Exactly once (§2.2), refusals (§2.7)
# ---------------------------------------------------------------------------


def test_a_retried_firing_adopts_the_run_it_made(db, ctx, repo_id, github) -> None:
    github.issues = [issue(1, minutes_ago=99), issue(2, minutes_ago=98)]
    schedule = sweep(db, repo_id, params={"max_new_per_firing": 1})
    (first,) = issue_sweep.create(firing_for(ctx, schedule))

    # The same firing again, as a finisher after a crash before the record.
    recorded: list = []
    again = issue_sweep.create(firing_for(ctx, schedule, recorded=recorded))

    assert again == [first] and recorded == [first]
    assert len(issue_runs(db)) == 1


def test_another_tenants_run_with_the_same_firing_id_is_not_adopted(db, ctx, repo_id) -> None:
    schedule = sweep(db, repo_id)
    fid = schedulefire.firing_id(SCHEDULE_ID, SLOT)
    db.docs["issue_runs/run_theirs"] = {
        "id": "run_theirs", "tenant_id": "research",
        "metadata": {"schedule": {"schedule_id": SCHEDULE_ID, "firing_id": fid}},
    }

    work = issue_sweep.create(firing_for(ctx, schedule))

    assert [w["id"] for w in work] != ["run_theirs"] and len(work) == 1


def test_a_firing_whose_every_start_is_refused_is_refused_with_the_code(db, ctx, repo_id, monkeypatch) -> None:
    class Refused(Forbidden):
        code = "WORKSPACE_NOT_READY"

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise Refused("the workspace is not ready")

    monkeypatch.setattr(issue_sweep, "start_run", refuse)

    with pytest.raises(Refused):
        issue_sweep.create(firing_for(ctx, sweep(db, repo_id)))
    assert issue_runs(db) == []


# ---------------------------------------------------------------------------
# Dry run (§2.8)
# ---------------------------------------------------------------------------


def test_a_dry_run_lists_candidates_and_creates_nothing(db, ctx, repo_id, github, writes) -> None:
    github.issues = [issue(42), issue(43, labels=("blocked",))]
    sweep(db, repo_id, policy={"dry_run": True})

    schedulefire.Ticker(ctx).tick()

    (firing,) = firings(db).values()
    assert firing["state"] == "skipped" and firing["skip"]["code"] == "DRY_RUN"
    output = firing["dry_run"]
    assert [row["issue"] for row in output["would_start"]] == [f"{REPOSITORY}#42"]
    assert output["passed_over"] == [{"issue": f"{REPOSITORY}#43", "reason": "label: blocked"}]
    assert output["runs"] == {"plan_approval": "auto", "auto_merge": True,
                              "merge_approval": "required", "fix_rounds": 3}
    assert issue_runs(db) == [] and tasks(db) == []
    assert writes.comments == {} and writes.pulls == {}
    # Only reads reached GitHub.
    assert github.calls and all("/issues" in u or "/pulls" in u for u, _ in github.calls)
