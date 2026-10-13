"""The issue sweeper's selection: which open issues an `issue-sweep` schedule may start.

The `issue-sweep` and `issue-plan-only` schedule types
(`swarm_api/schedtypes/`) read and filter a tenant's open issues through
`issuesweep.collect_candidates` and `issuesweep.skip_reason`. Until
2026-10-12 a per-tenant Cloud Scheduler job did the same through
`POST /v1/admin/issues/sweep`; that job, the route, its SWEEP_ENABLED switch
and the tenant document's `issue_sweep` settings were removed on 2026-10-12
(Schedules S9, second change), and section 5 holds that they stay removed.

Owner decisions 2026-10-08: readiness is decided by the PLANNER (NOT_READY),
not by a label.

What these tests hold:

  1. The candidate filter: every skip rule, each beside a control that the
     same issue without the reason IS a candidate.
  2. The read: oldest-updated first, and a repository that cannot be read
     whole is reported rather than half-swept.
  3. NOT_READY: the planner's verdict ends the run holding nothing, is posted
     on the issue, and the issue is not selected again until it changes.
  4. The territory guard holds an auto approval whose files overlap a live
     run's plan, and releases it when that run ends.
  5. The old job's route, its settings routes and its switch are gone.

No credentials, no network, no emulator: GitHub is a fake transport under the
real `forge.GitHubIssues`, Firestore is the fake.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import auth, forge, forgewrite, issueruns, issuesweep
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge import SweepIssue
from swarm_api.groups import StaticGroups
from swarm_api.issuecomments import render_status_comment
from swarm_api.issueruns import InvalidPlan, IssueRuns, RunState, parse_planner_output
from swarm_api.issuesweep import SweepConfig, claimed_issues, skip_reason
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import Repositories, repo_id_for
from swarm_api.routes import admin as admin_routes
from swarm_api.routes import runs as runs_routes
from swarm_api.settings import ApiSettings
from swarm_api.validation import IssueRef
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_tenant
from .test_issue_runs import _finish_planner

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
OWNER, REPO = "saga-xyz", "widgets"
REPOSITORY = f"{OWNER}/{REPO}"

NOW = datetime.now(timezone.utc)

NOT_READY = {
    "ready": False,
    "kind": "blocked",
    "reason": "The sort path is rewritten by #612, which is still open.",
    "needs": ["depends on #612", "owner decision: keep the legacy order?"],
}


def _issue(number: int, *, minutes_ago: float = 60, labels: tuple[str, ...] = (),
           title: str = "", author: str = "MEMBER") -> dict[str, Any]:
    updated = NOW - timedelta(minutes=minutes_ago)
    return {
        "number": number,
        "title": title or f"issue {number}",
        "labels": [{"name": label} for label in labels],
        "updated_at": updated.isoformat().replace("+00:00", "Z"),
        # GitHub's own field on every issue it lists: what the issue's author
        # is to the repository. The sweep starts work only for an issue an
        # owner, an organisation member or a collaborator opened.
        "author_association": author,
    }


def _plan(*files: str, step: str = "change") -> dict[str, Any]:
    return {
        "summary": "Change the thing.",
        "steps": [{"step_id": step, "title": "Change it", "prompt": "Change it.",
                   "files": list(files)}],
    }


@pytest.fixture
def github():
    return forge_fakes.GitHub(issues=[_issue(42)], pulls=[], files={})


@pytest.fixture
def writes():
    return forge_fakes.GitHubWrites()


def _context(db, tokens, group_map, objects, github, writes):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=writes),
    )


def _register(db, *, tenant_id="eng", owner=OWNER, repo=REPO, created_by="alice@saga.xyz") -> str:
    repo_id = repo_id_for(tenant_id, owner, repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "archived": False,
        "repository_url": f"https://github.com/{owner}/{repo}",
        "created_by": created_by, "created_at": NOW, "updated_at": NOW,
    }
    return repo_id


@pytest.fixture
def client(db, tokens, group_map, objects, github, writes):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _register(db)
    return TestClient(
        create_app(_context(db, tokens, group_map, objects, github, writes)),
        raise_server_exceptions=False,
    )


def _collect(client, *, config: SweepConfig | None = None,
             tenant_id: str = "eng") -> tuple[list[str], issuesweep.SweepReport]:
    """One read and filter of the tenant's registrations, as a schedule's
    selection makes it (`schedtypes.issue_sweep.select`): the candidates, in
    the order they would start, and the report their skips went on."""
    ctx = client.app.state.ctx
    config = config or SweepConfig(enabled=True)
    runs = IssueRuns(ctx.db, now=ctx.now)
    report = issuesweep.SweepReport(tenant_id=tenant_id, enabled=True, cap=config.max_live_runs)
    live_rows, _ = runs.live(tenant_id, limit=issuesweep.LIVE_SCAN)
    live = {(row.issue.repository.lower(), row.issue.number): row for row in live_rows}
    registrations, _ = Repositories(ctx.db, now=ctx.now).list(tenant_id, limit=100)
    candidates = issuesweep.collect_candidates(
        ctx, ctx.store.get_tenant(tenant_id), tenant_id, registrations, config=config,
        report=report, runs=runs, live=live, clock=time.monotonic, started_at=time.monotonic(),
    )
    return [c.ref.short for c in candidates], report


def _tick(client, tenant_id="eng"):
    return client.post(f"/v1/admin/runs/advance?tenant_id={tenant_id}", headers=SWEEPER_HEADERS)


def _runs(db) -> dict[str, dict]:
    return {p.split("/", 1)[1]: d for p, d in db.docs.items() if p.startswith("issue_runs/")}


def _run_of(db, number: int) -> dict:
    rows = sorted(
        (d for d in _runs(db).values() if d["issue"]["number"] == number),
        key=lambda d: d["created_at"],
    )
    assert rows, f"no run for #{number}"
    return rows[-1]


def _reasons(report: issuesweep.SweepReport) -> dict[str, str]:
    return {entry["issue"]: entry["reason"] for entry in report.skipped}


def _auto_run(client, number: int) -> dict:
    created = client.post("/v1/runs", headers=auth_header("alice"),
                          json={"issue": f"{REPOSITORY}#{number}", "plan_approval": "auto"})
    assert created.status_code == 201, created.text
    return created.json()["run"]


# --------------------------------------------------------------------------
# 1. the candidate filter, rule by rule, each with its control
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 1. the candidate filter, rule by rule, each with its control
# --------------------------------------------------------------------------

def _candidate(issue: SweepIssue, *, config=SweepConfig(enabled=True), live=None,
               claimed=None, last=None) -> str | None:
    return skip_reason(
        issue, repository=REPOSITORY, config=config, live=live or {},
        claimed=claimed or {}, last_run=lambda number: last,
    )


def _sweep_issue(number=42, labels=(), minutes_ago=60.0, association="MEMBER") -> SweepIssue:
    return SweepIssue(number=number, title="t", labels=tuple(labels),
                      updated_at=NOW - timedelta(minutes=minutes_ago),
                      author_association=association)


def test_the_control_issue_is_a_candidate():
    assert _candidate(_sweep_issue()) is None


@pytest.mark.parametrize("label", ["epic", "blocked", "security", "Security", "EPIC"])
def test_an_epic_blocked_or_security_label_is_skipped(label):
    assert _candidate(_sweep_issue(labels=(label,))) == f"label: {label.lower()}"
    assert _candidate(_sweep_issue(labels=("bug",))) is None


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "member"])
def test_an_issue_an_owner_member_or_collaborator_opened_is_a_candidate(association):
    assert _candidate(_sweep_issue(association=association)) is None


@pytest.mark.parametrize("association", [
    "NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", "",
])
def test_an_issue_an_outsider_opened_is_skipped(association):
    # Security review 2026-10-09 (docs/security/review-2026-10-09.md, item 1):
    # every swept run is auto-approved and auto-merged with the tenant's
    # credential, so on a public repository anybody's issue would otherwise
    # become a merged change nobody approved. A merged contributor
    # (CONTRIBUTOR) holds no write access either, and an association GitHub
    # did not send is not proof of one.
    assert _candidate(_sweep_issue(association=association)) == (
        f"author: {association or 'unknown'}"
    )


def test_the_author_rule_comes_before_every_rule_that_reads_firestore():
    reads: list[int] = []

    def last_run(number):
        reads.append(number)
        return None

    reason = skip_reason(
        _sweep_issue(association="NONE"), repository=REPOSITORY,
        config=SweepConfig(enabled=True), live={}, claimed={}, last_run=last_run,
    )
    assert reason == "author: NONE"
    assert reads == []


def test_the_tenants_exclusion_list_skips_by_number_and_by_label():
    config = SweepConfig(enabled=True, exclude_issues=[42], exclude_labels=["Deferred"])
    assert _candidate(_sweep_issue(42), config=config) == "excluded: issue"
    assert _candidate(_sweep_issue(43, labels=("deferred",)), config=config) == "excluded: label deferred"
    assert _candidate(_sweep_issue(43, labels=("bug",)), config=config) is None


def _stored_run(state: RunState, *, number=42, updated=None, wrote=None, run_id="run_prev"):
    at = updated or NOW - timedelta(hours=2)
    return issueruns.IssueRun(
        id=run_id, tenant_id="eng", created_by="alice@saga.xyz", created_at=at, updated_at=at,
        state=state, issue=IssueRef(owner=OWNER, repo=REPO, number=number),
        plan_approval="auto", auto_merge=True, fix_rounds=2, planner_task_id="t1",
        last_writeback_at=wrote,
    )


def test_an_issue_with_a_live_run_is_skipped():
    live = {(REPOSITORY.lower(), 42): _stored_run(RunState.RUNNING, run_id="run_live")}
    assert _candidate(_sweep_issue(42), live=live) == "live_run: run_live"
    assert _candidate(_sweep_issue(43), live=live) is None


def test_an_issue_an_open_pull_request_claims_is_skipped():
    assert _candidate(_sweep_issue(42), claimed={42: 9}) == "open_pull_request: #9"
    assert _candidate(_sweep_issue(43), claimed={42: 9}) is None


@pytest.mark.parametrize("text", [
    "Closes #42", "fixes #42", "Resolved: #42", "This is part of #42.",
    f"Fixes {REPOSITORY}#42", f"closes https://github.com/{REPOSITORY}/issues/42",
    "Part   of #42 and closes #7",
])
def test_a_closing_keyword_or_part_of_claims_the_issue(text):
    assert 42 in claimed_issues(text, REPOSITORY)


@pytest.mark.parametrize("text", [
    "Mentions #42 only", "see #42", "Closes other-org/widgets#42",
    "closes https://github.com/other/widgets/issues/42", "prefixes #42", "fixes#42",
])
def test_a_mere_mention_or_another_repositorys_issue_claims_nothing(text):
    assert 42 not in claimed_issues(text, REPOSITORY)


def test_a_not_ready_verdict_holds_until_the_issue_changes_after_the_runs_last_write():
    wrote = NOW - timedelta(minutes=30)
    verdict = _stored_run(RunState.NOT_READY, updated=NOW - timedelta(minutes=31), wrote=wrote)
    # Our own status comment moved updated_at to just after the verdict.
    unchanged = SweepIssue(number=42, title="t", labels=(), updated_at=wrote + timedelta(seconds=5), author_association="MEMBER")
    assert _candidate(unchanged, last=verdict) == "not_ready_unchanged: run_prev"
    edited = SweepIssue(number=42, title="t", labels=(), updated_at=wrote + timedelta(minutes=5), author_association="MEMBER")
    assert _candidate(edited, last=verdict) is None


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.REJECTED, RunState.CANCELLED,
                                   RunState.DONE])
def test_any_ended_run_holds_an_unchanged_issue_too(state):
    ended = _stored_run(state, wrote=NOW - timedelta(minutes=10))
    unchanged = _sweep_issue(minutes_ago=11)
    assert _candidate(unchanged, last=ended) == f"unchanged_since_run: run_prev ({state.value})"
    assert _candidate(_sweep_issue(minutes_ago=1), last=ended) is None


def test_an_issue_github_gave_no_time_for_is_not_called_changed():
    ended = _stored_run(RunState.NOT_READY)
    issue = SweepIssue(number=42, title="t", labels=(), updated_at=None, author_association="MEMBER")
    assert _candidate(issue, last=ended) == "not_ready_unchanged: run_prev"
    assert _candidate(issue, last=None) is None



# --------------------------------------------------------------------------
# 2. the read
# --------------------------------------------------------------------------

def test_pull_requests_in_the_issue_list_are_never_candidates(client, github):
    github.issues = [{"number": 99, "title": "a PR", "pull_request": {}}, _issue(42)]
    started, report = _collect(client)
    assert started == [f"{REPOSITORY}#42"]
    assert all(not entry["issue"].endswith("#99") for entry in report.skipped)


def test_an_outsiders_issue_is_never_a_candidate(client, github):
    github.issues = [_issue(7, author="NONE"), _issue(8, author="CONTRIBUTOR"), _issue(42)]
    started, report = _collect(client)
    assert started == [f"{REPOSITORY}#42"]
    assert _reasons(report) == {
        f"{REPOSITORY}#7": "author: NONE",
        f"{REPOSITORY}#8": "author: CONTRIBUTOR",
    }
    assert report.skipped_by_reason["author"] == 2


def test_the_sweep_listing_carries_each_issues_author_association(github):
    github.issues = [_issue(7, author="NONE"), _issue(42)]
    del github.issues[1]["author_association"]
    listing = forge.GitHubIssues(send=github).sweep_listing(OWNER, REPO, "t" * 8)
    assert [(i.number, i.author_association) for i in listing.issues] == [(7, "NONE"), (42, "")]


def test_each_skip_reaches_the_report_with_its_reason(client, github):
    github.issues = [
        _issue(1, labels=("epic",)), _issue(2, labels=("blocked",)), _issue(3, labels=("security",)),
        _issue(4), _issue(5, labels=("deferred",)), _issue(6), _issue(42),
    ]
    github.pulls = [{"number": 9, "title": "Sort", "body": "Part of #6"}]
    config = SweepConfig(enabled=True, exclude_issues=[4], exclude_labels=["deferred"])

    started, report = _collect(client, config=config)

    assert started == [f"{REPOSITORY}#42"]
    assert _reasons(report) == {
        f"{REPOSITORY}#1": "label: epic",
        f"{REPOSITORY}#2": "label: blocked",
        f"{REPOSITORY}#3": "label: security",
        f"{REPOSITORY}#4": "excluded: issue",
        f"{REPOSITORY}#5": "excluded: label deferred",
        f"{REPOSITORY}#6": "open_pull_request: #9",
    }
    # And once #42 has a live run, the next read finds it.
    _auto_run(client, 42)
    again, report = _collect(client, config=config)
    assert again == []
    assert _reasons(report)[f"{REPOSITORY}#42"].startswith("live_run: run_")


def test_candidates_come_oldest_updated_first(client, github):
    github.issues = [
        _issue(5, minutes_ago=10), _issue(6, minutes_ago=5000), _issue(7, minutes_ago=300),
        {"number": 8, "title": "no time", "author_association": "MEMBER"},
    ]

    started, _ = _collect(client)

    assert started == [f"{REPOSITORY}#6", f"{REPOSITORY}#7", f"{REPOSITORY}#5", f"{REPOSITORY}#8"]
    # And the issue list was asked for in that order, so a cut keeps the oldest.
    url = next(u for u, _ in github.calls if "/issues?" in u)
    assert "sort=updated" in url and "direction=asc" in url


def test_a_repository_with_more_open_pull_requests_than_one_read_is_not_swept(client, github):
    github.pulls = [{"number": n, "title": f"pr {n}", "body": ""} for n in range(1, 302)]
    started, report = _collect(client)
    assert started == []
    assert report.failures == [{"repository": REPOSITORY, "error": "too_many_pull_requests"}]


def test_a_repository_the_token_cannot_read_is_reported_and_the_rest_swept(client, db, github):
    _register(db, owner="saga-xyz", repo="gadgets")
    github.status = {"/gadgets/issues": 403}
    started, report = _collect(client)
    assert started == [f"{REPOSITORY}#42"]
    assert report.failures == [{"repository": "saga-xyz/gadgets", "error": "no_access"}]


# --------------------------------------------------------------------------
# 3. NOT_READY
# --------------------------------------------------------------------------


def test_the_planner_prompt_asks_for_readiness_first():
    prompt = issueruns.planner_prompt(IssueRef(owner=OWNER, repo=REPO, number=42))
    assert '"ready": false' in prompt
    for criterion in ("already done", "owner", "blocked", "too vague", "security", "epic"):
        assert criterion in prompt, criterion
    assert len(prompt.encode()) < issueruns.MAX_PLANNER_PROMPT_BYTES


def test_a_not_ready_verdict_parses_and_a_malformed_one_is_refused():
    kind, verdict = parse_planner_output(json.dumps(NOT_READY))
    assert kind == "not_ready"
    assert verdict == {"kind": "blocked", "reason": NOT_READY["reason"], "needs": NOT_READY["needs"]}
    with pytest.raises(InvalidPlan):
        parse_planner_output(json.dumps({"ready": False}))  # no reason
    with pytest.raises(InvalidPlan):
        parse_planner_output(json.dumps({**NOT_READY, "kind": "bored"}))
    with pytest.raises(InvalidPlan):
        parse_planner_output(json.dumps({**NOT_READY, "ready": "no"}))
    # A plan beside "ready": true is the plan, digested as if it never said so.
    plan = _plan("a.py")
    assert parse_planner_output(json.dumps({**plan, "ready": True})) == ("plan", issueruns.parse_plan(plan))
    assert parse_planner_output(json.dumps(plan)) == ("plan", issueruns.parse_plan(plan))


def test_not_ready_is_terminal_and_reached_only_from_planning():
    assert RunState.NOT_READY in issueruns.TERMINAL_RUN_STATES
    issueruns.assert_run_transition(RunState.PLANNING, RunState.NOT_READY)
    for frm in RunState:
        if frm != RunState.PLANNING:
            assert RunState.NOT_READY not in issueruns.RUN_TRANSITIONS[frm], frm




def test_a_not_ready_run_holds_nothing_and_tells_the_issue_why(client, db, objects, writes):
    _auto_run(client, 42)
    run = _run_of(db, 42)
    _finish_planner(db, objects, run, NOT_READY)
    tasks_before = {p for p in db.docs if p.startswith("tasks/")}

    _tick(client)

    stored = _run_of(db, 42)
    assert stored["state"] == "NOT_READY"
    assert stored["not_ready"]["needs"] == NOT_READY["needs"]
    assert stored["last_writeback_at"] is not None
    # Invariant 1: nothing after the planner -- no workflow, task or lease.
    assert {p for p in db.docs if p.startswith("tasks/")} == tasks_before
    assert not [p for p in db.docs if p.startswith("workflows/")]
    # ONE comment on the issue: the status comment, with the reason and needs.
    comments = writes.on_issue(42)
    assert len(comments) == 1
    body = comments[0]["body"]
    assert "not ready" in body and "blocked by other work" in body
    assert "rewritten by #612" in body and "depends on #612" in body
    # A tick later it is not visited again.
    assert _tick(client).json()["report"]["visited"] == 0
    # The API serves the verdict.
    served = client.get(f"/v1/runs/{stored['id']}", headers=auth_header("alice")).json()["run"]
    assert served["state"] == "NOT_READY" and served["terminal"] is True
    assert served["not_ready"]["kind"] == "blocked"


def test_a_not_ready_issue_is_not_selected_again_until_it_changes(client, db, objects, github):
    first = _auto_run(client, 42)
    _finish_planner(db, objects, first, NOT_READY)
    _tick(client)
    wrote = _run_of(db, 42)["last_writeback_at"]
    # Our own status comment is the issue's last update.
    github.issues = [{**_issue(42), "updated_at": (wrote + timedelta(seconds=2)).isoformat()}]

    for _ in range(3):
        started, report = _collect(client)
        assert started == []
        assert _reasons(report)[f"{REPOSITORY}#42"] == f"not_ready_unchanged: {first['id']}"

    # Someone comments: the issue is a candidate again.
    github.issues = [{**_issue(42), "updated_at": (wrote + timedelta(minutes=5)).isoformat()}]
    started, _ = _collect(client)
    assert started == [f"{REPOSITORY}#42"]


def test_the_status_comment_for_not_ready_neutralises_the_agents_text():
    run = _stored_run(RunState.NOT_READY)
    run.not_ready = {"kind": "other", "reason": "ping @owner, fixes #3", "needs": ["@someone decides"]}
    text = render_status_comment(run)
    assert "@owner" not in text and "@someone" not in text
    assert "fixes #3" not in text.lower()


# --------------------------------------------------------------------------
# 4. the territory guard
# --------------------------------------------------------------------------

def test_an_overlapping_plan_waits_in_planned_and_goes_when_the_other_run_ends(client, db, objects):
    first = _auto_run(client, 42)
    second = _auto_run(client, 43)
    _finish_planner(db, objects, first, _plan("src/sort.py", "src/a.py"))
    _finish_planner(db, objects, second, _plan("src/sort.py", "src/b.py"))

    _tick(client)

    assert db.docs[f"issue_runs/{first['id']}"]["state"] == "RUNNING"
    held = db.docs[f"issue_runs/{second['id']}"]
    assert held["state"] == "PLANNED"
    assert held["hold"] == f"territory_overlap: {first['id']}"
    assert held["workflow_id"] is None
    # Re-checked on every tick, and still held while the first is live.
    _tick(client)
    assert db.docs[f"issue_runs/{second['id']}"]["state"] == "PLANNED"
    served = client.get(f"/v1/runs/{second['id']}", headers=auth_header("alice")).json()["run"]
    assert served["hold"] == f"territory_overlap: {first['id']}"

    IssueRuns(db).transition("eng", first["id"], RunState.CANCELLED, by="test")
    _tick(client)

    released = db.docs[f"issue_runs/{second['id']}"]
    assert released["state"] == "RUNNING"
    assert released["hold"] is None


def test_plans_on_disjoint_files_both_go(client, db, objects):
    first = _auto_run(client, 42)
    second = _auto_run(client, 43)
    _finish_planner(db, objects, first, _plan("src/a.py"))
    _finish_planner(db, objects, second, _plan("src/b.py"))

    _tick(client)

    assert db.docs[f"issue_runs/{first['id']}"]["state"] == "RUNNING"
    assert db.docs[f"issue_runs/{second['id']}"]["state"] == "RUNNING"


def test_a_directory_overlaps_the_files_inside_it_and_a_prefix_does_not():
    assert issueruns.territory_overlap({"src/widgets"}, {"src/widgets/sort.py"}) == ["src/widgets"]
    assert issueruns.territory_overlap({"src/a.py"}, {"src/ab.py"}) == []
    assert issueruns.plan_files(_plan("./src/a.py", "src/b/")) == {"src/a.py", "src/b"}
    assert issueruns.plan_files(None) == set()


def test_two_waiting_runs_never_wait_for_each_other(db):
    """Of two overlapping PLANNED auto runs, only the OLDER holds the newer:
    the oldest of any overlapping set is always free to go."""
    runs = IssueRuns(db)
    older = _stored_run(RunState.PLANNED, number=42, run_id="run_older",
                        updated=NOW - timedelta(minutes=10))
    newer = _stored_run(RunState.PLANNED, number=43, run_id="run_newer",
                        updated=NOW - timedelta(minutes=5))
    for run in (older, newer):
        run.plan = issueruns.parse_plan(_plan("src/sort.py"))
        run.plan_digest = issueruns.plan_digest(run.plan)
        runs.create(run)
    ctx = SimpleNamespace(db=db, now=lambda: NOW)

    assert runs_routes.territory_conflict(ctx, "eng", older) is None
    held = runs_routes.territory_conflict(ctx, "eng", newer)
    assert held is not None and held[0].id == "run_older" and held[1] == ["src/sort.py"]


def test_a_plan_waiting_for_a_person_holds_nobody(client, db, objects):
    person = client.post("/v1/runs", headers=auth_header("alice"),
                         json={"issue": f"{REPOSITORY}#42"}).json()["run"]
    auto = _auto_run(client, 43)
    _finish_planner(db, objects, person, _plan("src/sort.py"))
    _finish_planner(db, objects, auto, _plan("src/sort.py"))

    _tick(client)

    assert db.docs[f"issue_runs/{person['id']}"]["state"] == "PLANNED"
    assert db.docs[f"issue_runs/{auto['id']}"]["state"] == "RUNNING"



# --------------------------------------------------------------------------
# 5. the old job's route, settings and switch are gone (2026-10-12)
# --------------------------------------------------------------------------
#
# Schedules S9's second change (docs/schedules.md §8.1 step 4): the tenant's
# `issue-sweep` schedule replaced the per-tenant Cloud Scheduler job, so the
# job's route, its settings routes, SWEEP_ENABLED and the tenant document's
# `issue_sweep` settings were deleted with it. A route restored by a revert
# would start auto-merging runs beside the schedule's -- two triggers of one
# sweep -- so their absence is held here.

GONE = [
    ("POST", "/v1/admin/issues/sweep"),
    ("GET", "/v1/admin/tenants/{tenant_id}/issue-sweep"),
    ("PUT", "/v1/admin/tenants/{tenant_id}/issue-sweep"),
]


@pytest.mark.parametrize("method,path", GONE)
def test_the_old_sweep_routes_are_not_served(method, path):
    served = {
        (m, getattr(route, "path", None))
        for route in admin_routes.router.routes
        for m in (getattr(route, "methods", None) or ())
    }
    assert (method, path) not in served
    # The control: a neighbouring admin route of the same router is.
    assert ("POST", "/v1/admin/runs/advance") in served


def test_the_old_sweep_route_answers_not_found_even_to_an_admin(client, db):
    response = client.post("/v1/admin/issues/sweep?tenant_id=eng", headers=auth_header("root"))
    assert response.status_code in (404, 405), response.text
    assert _runs(db) == {}


def test_the_scheduler_identity_can_no_longer_start_a_sweep(client, db, github):
    response = client.post("/v1/admin/issues/sweep?tenant_id=eng", headers=SWEEPER_HEADERS)
    assert response.status_code in (403, 404, 405), response.text
    assert _runs(db) == {} and github.calls == []
    assert ("POST", "/v1/admin/issues/sweep") not in auth.ROLLUP_SWEEPER_ROUTES


@pytest.mark.parametrize("path", ["/v1/admin/tenants/eng/issue-sweep"])
def test_the_old_settings_routes_answer_not_found(client, path):
    assert client.get(path, headers=auth_header("root")).status_code in (404, 405)
    assert client.put(path, headers=auth_header("root"),
                      json={"enabled": True}).status_code in (404, 405)


def test_the_platform_switch_is_gone(monkeypatch):
    assert "sweep_enabled" not in ApiSettings.__dataclass_fields__
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setenv("SWEEP_ENABLED", "true")
    assert not hasattr(ApiSettings.from_env(), "sweep_enabled")


@pytest.mark.parametrize("name", [
    "sweep_tenant", "SweepSubmitterNotMember", "get_config", "set_config", "TENANT_FIELD",
])
def test_the_route_only_half_of_issuesweep_is_gone(name):
    assert not hasattr(issuesweep, name), name
    # What the schedule types call stays.
    for kept in ("collect_candidates", "skip_reason", "SweepConfig", "SweepReport",
                 "claimed_issues", "TRUSTED_AUTHORS", "LIVE_SCAN"):
        assert hasattr(issuesweep, kept), kept
