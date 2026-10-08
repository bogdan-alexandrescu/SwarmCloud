"""The issue sweeper: SwarmCloud starts issue runs for the open issues that are ready.

    POST /v1/admin/issues/sweep?tenant_id=<t>   the per-tenant Cloud Scheduler job

Owner decisions 2026-10-08: readiness is decided by the PLANNER (NOT_READY),
not by a label; the sweep runs inside the platform on a schedule; at most 8
live issue runs per tenant; plans auto-approve and pull requests auto-merge.

What these tests hold:

  1. The candidate filter: every skip rule, each beside a control that the
     same issue without the reason IS a candidate.
  2. The cap of 8, and the oldest-updated-first order.
  3. NOT_READY: the planner's verdict ends the run holding nothing, is posted
     on the issue, and the issue is not planned again until it changes.
  4. The territory guard holds an auto approval whose files overlap a live
     run's plan, and releases it when that run ends.
  5. Only the scheduler's identity may call the route.
  6. Both switches default off.

No credentials, no network, no emulator: GitHub is a fake transport under the
real `forge.GitHubIssues`, Firestore is the fake.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forgewrite, issueruns, issuesweep
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
from swarm_api.repositories import repo_id_for
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
           title: str = "") -> dict[str, Any]:
    updated = NOW - timedelta(minutes=minutes_ago)
    return {
        "number": number,
        "title": title or f"issue {number}",
        "labels": [{"name": label} for label in labels],
        "updated_at": updated.isoformat().replace("+00:00", "Z"),
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


def _context(db, tokens, group_map, objects, github, writes, *, sweep_enabled=True):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,), sweep_enabled=sweep_enabled),
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


def _enable(db, tenant_id="eng", **settings: Any) -> None:
    # `submit_as` is explicit (owner decision 2026-10-08): a sweep without it
    # starts nothing, so every test that expects runs names a current member.
    settings.setdefault("submit_as", "alice@saga.xyz")
    issuesweep.set_config(db, tenant_id, SweepConfig(enabled=True, **settings))


@pytest.fixture
def client(db, tokens, group_map, objects, github, writes):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _register(db)
    _enable(db)
    return TestClient(
        create_app(_context(db, tokens, group_map, objects, github, writes)),
        raise_server_exceptions=False,
    )


def _sweep(client, tenant_id="eng", headers=SWEEPER_HEADERS):
    return client.post(f"/v1/admin/issues/sweep?tenant_id={tenant_id}", headers=headers)


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


def _started(body: dict) -> list[str]:
    return [entry["issue"] for entry in body["started"]]


def _reasons(body: dict) -> dict[str, str]:
    return {entry["issue"]: entry["reason"] for entry in body["skipped"]}


# --------------------------------------------------------------------------
# 1. the candidate filter, rule by rule, each with its control
# --------------------------------------------------------------------------

def _candidate(issue: SweepIssue, *, config=SweepConfig(enabled=True), live=None,
               claimed=None, last=None) -> str | None:
    return skip_reason(
        issue, repository=REPOSITORY, config=config, live=live or {},
        claimed=claimed or {}, last_run=lambda number: last,
    )


def _sweep_issue(number=42, labels=(), minutes_ago=60.0) -> SweepIssue:
    return SweepIssue(number=number, title="t", labels=tuple(labels),
                      updated_at=NOW - timedelta(minutes=minutes_ago))


def test_the_control_issue_is_a_candidate():
    assert _candidate(_sweep_issue()) is None


@pytest.mark.parametrize("label", ["epic", "blocked", "security", "Security", "EPIC"])
def test_an_epic_blocked_or_security_label_is_skipped(label):
    assert _candidate(_sweep_issue(labels=(label,))) == f"label: {label.lower()}"
    assert _candidate(_sweep_issue(labels=("bug",))) is None


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
    unchanged = SweepIssue(number=42, title="t", labels=(), updated_at=wrote + timedelta(seconds=5))
    assert _candidate(unchanged, last=verdict) == "not_ready_unchanged: run_prev"
    edited = SweepIssue(number=42, title="t", labels=(), updated_at=wrote + timedelta(minutes=5))
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
    issue = SweepIssue(number=42, title="t", labels=(), updated_at=None)
    assert _candidate(issue, last=ended) == "not_ready_unchanged: run_prev"
    assert _candidate(issue, last=None) is None


def test_pull_requests_in_the_issue_list_are_never_candidates(client, db, github):
    github.issues = [{"number": 99, "title": "a PR", "pull_request": {}}, _issue(42)]
    body = _sweep(client).json()
    assert _started(body) == [f"{REPOSITORY}#42"]
    assert all(d["issue"]["number"] != 99 for d in _runs(db).values())


def test_each_skip_reaches_the_route_report_with_its_reason(client, db, github):
    github.issues = [
        _issue(1, labels=("epic",)), _issue(2, labels=("blocked",)), _issue(3, labels=("security",)),
        _issue(4), _issue(5, labels=("deferred",)), _issue(6), _issue(42),
    ]
    github.pulls = [{"number": 9, "title": "Sort", "body": "Part of #6"}]
    _enable(db, exclude_issues=[4], exclude_labels=["deferred"])

    body = _sweep(client).json()

    assert _started(body) == [f"{REPOSITORY}#42"]
    assert _reasons(body) == {
        f"{REPOSITORY}#1": "label: epic",
        f"{REPOSITORY}#2": "label: blocked",
        f"{REPOSITORY}#3": "label: security",
        f"{REPOSITORY}#4": "excluded: issue",
        f"{REPOSITORY}#5": "excluded: label deferred",
        f"{REPOSITORY}#6": "open_pull_request: #9",
    }
    # And the next sweep finds #42's live run.
    again = _sweep(client).json()
    assert _started(again) == []
    assert _reasons(again)[f"{REPOSITORY}#42"].startswith("live_run: run_")


# --------------------------------------------------------------------------
# what a swept run is
# --------------------------------------------------------------------------

def test_a_swept_run_is_auto_auto_merge_two_rounds_and_says_the_sweep_made_it(client, db, objects):
    body = _sweep(client).json()

    assert body["enabled"] is True and body["cap"] == 8
    run = _run_of(db, 42)
    assert body["started"] == [{"issue": f"{REPOSITORY}#42", "run_id": run["id"]}]
    assert run["plan_approval"] == "auto"
    assert run["auto_merge"] is True
    assert run["fix_rounds"] == 2
    assert run["created_by"] == "issue-sweep"
    assert run["on_behalf_of"] == "alice@saga.xyz"
    assert run["state"] == "PLANNING"
    # The planner is submitted as the registration's creator, never the sweeper.
    planner = db.docs[f"tasks/{run['planner_task_id']}"]
    assert planner["tenant_id"] == "eng" and planner["submitted_by"] == "alice@saga.xyz"
    # It is shown the open work, without its own issue.
    assert run["open_work"]["repository"] == REPOSITORY
    assert all(item["number"] != 42 for item in run["open_work"]["issues"])

    # And its auto approval submits as alice too (`run_owner_auth`).
    _finish_planner(db, objects, run, _plan("src/sort.py"))
    _tick(client)
    stored = _run_of(db, 42)
    assert stored["state"] == "RUNNING"
    tasks = [d for p, d in db.docs.items()
             if p.startswith("tasks/") and d.get("workflow_id") == stored["workflow_id"]]
    assert tasks and {t["submitted_by"] for t in tasks} == {"alice@saga.xyz"}
    assert SWEEPER not in json.dumps(tasks, default=str)


def test_a_swept_run_fails_rather_than_submit_once_its_owner_leaves(client, db, objects, monkeypatch):
    _sweep(client)
    run = _run_of(db, 42)
    _finish_planner(db, objects, run, _plan("src/sort.py"))
    monkeypatch.setattr(client.app.state.ctx.authenticator, "is_tenant_member",
                        lambda email, tenant: email != "alice@saga.xyz")

    _tick(client)

    stored = _run_of(db, 42)
    assert stored["state"] == "FAILED"
    assert "alice@saga.xyz" in stored["error"] and "no longer a member" in stored["error"]
    assert not [p for p in db.docs if p.startswith("workflows/")]


def test_submit_as_is_the_submitter_whoever_registered_the_repository(client, db, objects):
    # bob registered it; the tenant's submit_as is alice. Alice carries the run.
    _register(db, created_by="bob@saga.xyz")
    body = _sweep(client).json()
    assert body["tenant_skipped"] is None
    run = _run_of(db, 42)
    assert body["started"] == [{"issue": f"{REPOSITORY}#42", "run_id": run["id"]}]
    assert run["on_behalf_of"] == "alice@saga.xyz"
    planner = db.docs[f"tasks/{run['planner_task_id']}"]
    assert planner["submitted_by"] == "alice@saga.xyz"


def test_the_registrant_is_no_longer_the_submitter(client, db):
    # alice registered it, submit_as is bob (not an eng member): nothing starts
    # as alice, and nothing falls back to her.
    issuesweep.set_config(db, "eng", SweepConfig(enabled=True, submit_as="bob@saga.xyz"))
    body = _sweep(client).json()
    assert body["started"] == [] and _runs(db) == {}
    assert "alice@saga.xyz" not in json.dumps(body)


def test_an_unset_submit_as_skips_the_tenant_with_the_reason(client, db, github, caplog):
    issuesweep.set_config(db, "eng", SweepConfig(enabled=True))
    with caplog.at_level("WARNING"):
        body = _sweep(client).json()
    assert body["started"] == [] and _runs(db) == {}
    assert body["tenant_skipped"] == "submit_as_unset"
    assert "submit_as" in caplog.text
    assert github.calls == []  # nothing is read for a tenant that cannot submit


def test_a_submit_as_who_is_not_a_member_skips_the_tenant_with_the_reason(client, db, github, caplog):
    issuesweep.set_config(db, "eng", SweepConfig(enabled=True, submit_as="carol@saga.xyz"))
    with caplog.at_level("WARNING"):
        body = _sweep(client).json()
    assert body["started"] == [] and _runs(db) == {}
    assert body["tenant_skipped"] == "submit_as_not_member: carol@saga.xyz"
    assert "carol@saga.xyz" in caplog.text
    assert github.calls == []


def test_membership_is_rechecked_on_every_submission(client, db, github, monkeypatch):
    github.issues = [_issue(1, minutes_ago=90), _issue(2, minutes_ago=80)]
    # A member until the first run exists, then gone: only a check made at
    # each submission (not once per sweep) can refuse the second.
    monkeypatch.setattr(client.app.state.ctx.authenticator, "is_tenant_member",
                        lambda email, tenant: not _runs(db))
    body = _sweep(client).json()
    assert _started(body) == [f"{REPOSITORY}#1"]
    assert _reasons(body)[f"{REPOSITORY}#2"] == "start_failed: submit_as_not_member"


def test_the_settings_route_carries_submit_as(client, db):
    put = client.put("/v1/admin/tenants/research/issue-sweep", headers=auth_header("root"),
                     json={"enabled": True, "submit_as": " Bob@Saga.xyz "})
    assert put.status_code == 200, put.text
    assert put.json()["issue_sweep"]["submit_as"] == "bob@saga.xyz"
    for bad in ({"submit_as": "not-an-address"}, {"submit_as": 5}):
        assert client.put("/v1/admin/tenants/research/issue-sweep", headers=auth_header("root"),
                          json=bad).status_code == 422, bad


def test_the_sweep_reads_and_starts_only_the_named_tenant(client, db, github):
    _register(db, tenant_id="research", created_by="bob@saga.xyz")
    _enable(db, "research", submit_as="bob@saga.xyz")
    body = _sweep(client, "eng").json()
    assert body["repositories"] == 1
    assert {d["tenant_id"] for d in _runs(db).values()} == {"eng"}


# --------------------------------------------------------------------------
# 2. the cap of 8, and oldest-updated first
# --------------------------------------------------------------------------

def test_the_sweep_stops_at_eight_live_runs(client, db, github):
    github.issues = [_issue(n, minutes_ago=100 - n) for n in range(1, 11)]

    body = _sweep(client).json()

    assert len(body["started"]) == 8
    assert body["skipped_by_reason"] == {"cap": 2}
    assert body["live_runs"] == 8
    assert len(_runs(db)) == 8
    # Full: the next sweep starts nothing, and starts nothing twice.
    again = _sweep(client).json()
    assert again["started"] == []
    assert again["skipped_by_reason"] == {"cap": 2, "live_run": 8}


def test_runs_a_person_started_count_against_the_cap(client, db, github):
    github.issues = [_issue(n) for n in range(1, 6)]
    _enable(db, max_live_runs=3)
    created = client.post("/v1/runs", headers=auth_header("alice"),
                          json={"issue": f"{REPOSITORY}#77"})
    assert created.status_code == 201, created.text

    body = _sweep(client).json()

    assert body["live_runs"] == 3 and len(body["started"]) == 2


def test_an_ended_run_frees_its_place(client, db, github):
    github.issues = [_issue(n, minutes_ago=100 - n) for n in range(1, 4)]
    _enable(db, max_live_runs=1)
    first = _sweep(client).json()
    assert _started(first) == [f"{REPOSITORY}#1"]
    IssueRuns(db).transition("eng", first["started"][0]["run_id"], RunState.CANCELLED, by="t")

    second = _sweep(client).json()

    assert _started(second) == [f"{REPOSITORY}#2"]


def test_candidates_start_oldest_updated_first_across_repositories(client, db, github):
    github.issues = [
        _issue(5, minutes_ago=10), _issue(6, minutes_ago=5000), _issue(7, minutes_ago=300),
        {"number": 8, "title": "no time"},
    ]
    _enable(db, max_live_runs=2)

    body = _sweep(client).json()

    assert _started(body) == [f"{REPOSITORY}#6", f"{REPOSITORY}#7"]
    assert _reasons(body) == {f"{REPOSITORY}#5": "cap: 2 live runs",
                              f"{REPOSITORY}#8": "cap: 2 live runs"}
    # And the issue list was asked for in that order, so a cut keeps the oldest.
    url = next(u for u, _ in github.calls if "/issues?" in u)
    assert "sort=updated" in url and "direction=asc" in url


def test_a_repository_with_more_open_pull_requests_than_one_read_is_not_swept(client, db, github):
    github.pulls = [{"number": n, "title": f"pr {n}", "body": ""} for n in range(1, 302)]
    body = _sweep(client).json()
    assert body["started"] == []
    assert body["failures"] == [{"repository": REPOSITORY, "error": "too_many_pull_requests"}]


def test_a_repository_the_token_cannot_read_is_reported_and_the_rest_swept(client, db, github):
    _register(db, owner="saga-xyz", repo="gadgets")
    github.status = {"/gadgets/issues": 403}
    body = _sweep(client).json()
    assert _started(body) == [f"{REPOSITORY}#42"]
    assert body["failures"] == [{"repository": "saga-xyz/gadgets", "error": "no_access"}]


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
    _sweep(client)
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


def test_a_not_ready_issue_is_not_planned_again_until_it_changes(client, db, objects, github):
    _sweep(client)
    first = _run_of(db, 42)
    _finish_planner(db, objects, first, NOT_READY)
    _tick(client)
    wrote = _run_of(db, 42)["last_writeback_at"]
    # Our own status comment is the issue's last update.
    github.issues = [{**_issue(42), "updated_at": (wrote + timedelta(seconds=2)).isoformat()}]

    for _ in range(3):
        body = _sweep(client).json()
        assert body["started"] == []
        assert _reasons(body)[f"{REPOSITORY}#42"] == f"not_ready_unchanged: {first['id']}"
    assert len(_runs(db)) == 1

    # Someone comments: the issue is planned again, by a new run.
    github.issues = [{**_issue(42), "updated_at": (wrote + timedelta(minutes=5)).isoformat()}]
    body = _sweep(client).json()
    assert len(body["started"]) == 1 and body["started"][0]["run_id"] != first["id"]


def test_the_status_comment_for_not_ready_neutralises_the_agents_text():
    run = _stored_run(RunState.NOT_READY)
    run.not_ready = {"kind": "other", "reason": "ping @owner, fixes #3", "needs": ["@someone decides"]}
    text = render_status_comment(run)
    assert "@owner" not in text and "@someone" not in text
    assert "fixes #3" not in text.lower()


# --------------------------------------------------------------------------
# 4. the territory guard
# --------------------------------------------------------------------------

def _auto_run(client, number: int) -> dict:
    created = client.post("/v1/runs", headers=auth_header("alice"),
                          json={"issue": f"{REPOSITORY}#{number}", "plan_approval": "auto"})
    assert created.status_code == 201, created.text
    return created.json()["run"]


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
# 5. only the scheduler's identity
# --------------------------------------------------------------------------

@pytest.mark.parametrize("user", ["alice", "root", "carol"])
def test_the_route_is_refused_to_everyone_but_the_scheduler(client, db, user):
    response = _sweep(client, headers=auth_header(user))
    assert response.status_code == 403, response.text
    assert _runs(db) == {}
    assert _sweep(client).status_code == 200


def test_the_settings_routes_are_admin_only(client):
    assert client.get("/v1/admin/tenants/eng/issue-sweep", headers=auth_header("alice")).status_code == 403
    assert client.put("/v1/admin/tenants/eng/issue-sweep", headers=auth_header("alice"),
                      json={"enabled": True}).status_code == 403
    assert client.get("/v1/admin/tenants/eng/issue-sweep", headers=SWEEPER_HEADERS).status_code == 403


def test_an_admin_sets_and_reads_the_tenants_sweep(client, db):
    put = client.put("/v1/admin/tenants/research/issue-sweep", headers=auth_header("root"),
                     json={"enabled": True, "max_live_runs": 4, "exclude_issues": [476, 12, 12],
                           "exclude_labels": ["Deferred"]})
    assert put.status_code == 200, put.text
    read = client.get("/v1/admin/tenants/research/issue-sweep", headers=auth_header("root")).json()
    assert read["issue_sweep"] == {"enabled": True, "max_live_runs": 4,
                                   "exclude_issues": [12, 476], "exclude_labels": ["deferred"],
                                   "submit_as": None}
    assert read["platform_enabled"] is True
    for bad in ({"enabled": "true"}, {"max_live_runs": 0}, {"max_live_runs": 51},
                {"exclude_issues": ["12"]}, {"surprise": 1}):
        assert client.put("/v1/admin/tenants/research/issue-sweep", headers=auth_header("root"),
                          json=bad).status_code == 422, bad
    assert client.put("/v1/admin/tenants/nobody/issue-sweep", headers=auth_header("root"),
                      json={"enabled": True}).status_code == 404


# --------------------------------------------------------------------------
# 6. both switches default off
# --------------------------------------------------------------------------

def test_the_platform_switch_defaults_off(monkeypatch):
    assert ApiSettings.__dataclass_fields__["sweep_enabled"].default is False
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.delenv("SWEEP_ENABLED", raising=False)
    assert ApiSettings.from_env().sweep_enabled is False
    monkeypatch.setenv("SWEEP_ENABLED", "true")
    assert ApiSettings.from_env().sweep_enabled is True


def test_the_tenant_switch_defaults_off(db):
    seed_tenant(db, "eng")
    assert SweepConfig().enabled is False
    assert issuesweep.get_config(db, "eng") == SweepConfig()
    assert issuesweep.get_config(db, "eng").max_live_runs == 8
    # A stored value that no longer validates reads as off, never on.
    db.docs["tenants/eng"]["issue_sweep"] = {"enabled": "yes"}
    assert issuesweep.get_config(db, "eng").enabled is False


def test_with_the_tenant_switch_off_nothing_is_read_or_started(client, db, github):
    issuesweep.set_config(db, "eng", SweepConfig())
    body = _sweep(client).json()
    assert body["enabled"] is False and body["disabled_by"] == "tenant"
    assert body["started"] == [] and _runs(db) == {}
    assert github.calls == []


def test_with_the_platform_switch_off_nothing_is_read_or_started(
    db, tokens, group_map, objects, github, writes
):
    seed_tenant(db, "eng")
    _register(db)
    _enable(db)
    off = TestClient(create_app(_context(db, tokens, group_map, objects, github, writes,
                                         sweep_enabled=False)), raise_server_exceptions=False)
    response = _sweep(off)
    assert response.status_code == 200, response.text
    assert response.json()["disabled_by"] == "SWEEP_ENABLED"
    assert _runs(db) == {} and github.calls == []
