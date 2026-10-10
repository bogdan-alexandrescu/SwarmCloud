"""Stranded pull requests are found, reported and redriven (part of #295).

Measured 2026-10-10: 40 pull requests SwarmCloud opened on 2026-10-09 were
left open and nobody was told -- 30 with no merge step at all, 7 whose merge
step failed `behind_too_often`. `swarm_api.strandedprs` classifies every
SwarmCloud-opened pull request open over two hours; this file holds:

  1. each classification, with the control that shows it could have come out
     the other way, and the order they are tested in (held first);
  2. CI that never ran, told apart from CI still running;
  3. which pull requests are SwarmCloud's and THIS tenant's (invariant 9);
  4. the sweep end to end through the real app and a fake GitHub under the
     shipped `GitHubWriter`: rows stored, served by GET, the log line's shape;
  5. one `pr_stranded` entry per pull request per six hours;
  6. redrive: only green `no_merge_step` / `behind`, never held, never red,
     one per repository, never from the scheduler's identity;
  7. tenant isolation: tenant B never sees tenant A's pull requests, even in a
     repository both registered, and its sweep never sends A's token.

Every token is built at runtime (`forge_fakes.AnyTenantTokens`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, repositories, strandedprs
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.strandedprs import (
    BEHIND, CHECKS_RED, CI_NEVER_RAN, CONFLICT, HELD, MERGE_FAILED, NO_MERGE_STEP,
    CiFacts, MergeStep, OpenPull, StrandedRow, ci_facts, classify, opener_task_id,
    redrive_refusal,
)
from swarm_api.waker import NullWaker
from swarm_common.logging_setup import CloudLoggingFormatter

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_task, seed_tenant

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
OWNER, NAME = "saga-xyz", "widgets"
REPO = f"{OWNER}/{NAME}"
OTHER = "gadgets"
NOW = datetime.now(timezone.utc)
APP = 15368


def _sha(n: int) -> str:
    return f"{n:040x}"


def _task_id(n: int) -> str:
    return f"task_{n:020x}"


# --------------------------------------------------------------------------
# a fake api.github.com for GitHubWriter: pull requests, rules, checks
# --------------------------------------------------------------------------

class GitHub:
    """The routes the sweep and a `merge_pr` submission read, over in-memory data."""

    def __init__(self) -> None:
        #: (owner/repo) -> number -> the pull request as GitHub serves it.
        self.pulls: dict[str, dict[int, dict[str, Any]]] = {}
        self.required: list[str] = ["python"]
        self.check_runs: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        #: (method, path, Authorization header) for every request.
        self.calls: list[tuple[str, str, str]] = []

    def add(self, number: int, *, repo: str = REPO, branch: str | None = None,
            hours: float = 5, labels: tuple[str, ...] = (), state: str = "clean",
            mergeable: bool | None = True, body: str = "", head: str | None = None,
            task: int | None = None) -> str:
        head = head or _sha(number)
        ref = branch if branch is not None else f"swarm/{_task_id(task or number)}"
        created = (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        self.pulls.setdefault(repo, {})[number] = {
            "number": number, "title": f"pull {number}", "state": "open", "merged": False,
            "html_url": f"https://github.com/{repo}/pull/{number}", "body": body,
            "head": {"sha": head, "ref": ref, "repo": {"full_name": repo}},
            "base": {"ref": "main", "repo": {"full_name": repo}},
            "labels": [{"name": label} for label in labels], "created_at": created,
            "mergeable": mergeable, "mergeable_state": state,
        }
        return head

    def green(self, sha: str, name: str = "python") -> None:
        self.check_runs.setdefault(sha, []).append(
            {"id": len(self.calls) + 1, "name": name, "status": "completed",
             "conclusion": "success", "app": {"id": APP}, "head_sha": sha})

    def red(self, sha: str, name: str = "python") -> None:
        self.check_runs.setdefault(sha, []).append(
            {"id": len(self.calls) + 1, "name": name, "status": "completed",
             "conclusion": "failure", "app": {"id": APP}, "head_sha": sha})

    def running(self, sha: str, name: str = "python") -> None:
        self.check_runs.setdefault(sha, []).append(
            {"id": len(self.calls) + 1, "name": name, "status": "in_progress",
             "conclusion": None, "app": {"id": APP}, "head_sha": sha})

    def tokens_sent_for(self, repo: str) -> set[str]:
        return {auth for _, path, auth in self.calls if path.startswith(f"/repos/{repo}/")}

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        self.calls.append((method, parsed.path, headers.get("Authorization", "")))
        parts = parsed.path.strip("/").split("/")  # repos/o/r/...
        repo, rest = "/".join(parts[1:3]), parts[3:]
        query = parse_qs(parsed.query)
        if method != "GET":
            return 405, b"{}"
        if rest == ["pulls"]:
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            listed = [
                {k: v for k, v in p.items() if k not in ("mergeable", "mergeable_state")}
                for _, p in sorted(self.pulls.get(repo, {}).items())
            ]
            return 200, json.dumps(listed[(page - 1) * per_page: page * per_page]).encode()
        if rest[:1] == ["pulls"] and len(rest) == 2:
            pull = self.pulls.get(repo, {}).get(int(rest[1]))
            return (200, json.dumps(pull).encode()) if pull else (404, b"{}")
        if rest[:2] == ["rules", "branches"]:
            return 200, json.dumps([{
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [
                    {"context": c, "integration_id": APP} for c in self.required]},
            }] if self.required else []).encode()
        if rest[:1] == ["commits"] and rest[2:] == ["check-runs"]:
            runs = self.check_runs.get(rest[1], [])
            return 200, json.dumps({"total_count": len(runs), "check_runs": runs}).encode()
        if rest[:1] == ["commits"] and rest[2:] == ["status"]:
            return 200, json.dumps({"statuses": self.statuses.get(rest[1], [])}).encode()
        return 404, b"{}"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def github() -> GitHub:
    return GitHub()


@pytest.fixture
def forge_tokens() -> forge_fakes.AnyTenantTokens:
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def client(db, tokens, group_map, objects, github, forge_tokens) -> TestClient:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _register(db, "eng", OWNER, NAME)
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge_writer=forgewrite.GitHubWriter(send=github),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _register(db, tenant_id: str, owner: str, repo: str) -> None:
    repo_id = repositories.repo_id_for(tenant_id, owner, repo)
    db.collection(repositories.COLLECTION).document(repo_id).set({
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github",
        "owner": owner, "repo": repo, "repository": f"{owner}/{repo}", "archived": False,
        "repository_url": repositories.repository_url(owner, repo),
        "default_branch": "main", "default_branch_source": "forge",
        "created_by": "alice@saga.xyz",
    })


def _opener(db, n: int, *, tenant_id: str = "eng", submitted_by: str = "alice@saga.xyz") -> str:
    """The task whose `swarm/<id>` branch opened pull request `n`, in `tenant_id`."""
    doc = seed_task(db, task_id=_task_id(n), tenant_id=tenant_id, state="SUCCEEDED",
                    runner_profile="claude-code")
    doc["submitted_by"] = submitted_by
    doc["repository_url"] = f"https://github.com/{REPO}"
    return doc["id"]


def _merge_task(db, n: int, *, number: int, state: str, refusal: dict | None = None,
                tenant_id: str = "eng", by_task: bool = False, minutes_ago: float = 30) -> str:
    """A merge step naming pull request `number`: by number (merge_pr) or by its opener."""
    doc = seed_task(db, task_id=_task_id(10_000 + n), tenant_id=tenant_id, state=state,
                    runner_profile="merge",
                    created_at=NOW - timedelta(minutes=minutes_ago))
    doc["repository_url"] = f"https://github.com/{REPO}"
    target = {"pull_request": _task_id(number)} if by_task else {"number": number,
                                                                 "head_sha": _sha(number)}
    doc["metadata"] = {"dispatch": {"merge_target": target}}
    if refusal is not None:
        doc["result_summary"] = {"merge": {"refusal": refusal, "repository": REPO,
                                           "pull_request": number}}
    return doc["id"]


def _sweep(client, tenant_id="eng", headers=SWEEPER_HEADERS, body: dict | None = None):
    return client.post(f"/v1/admin/stranded-prs/sweep?tenant_id={tenant_id}",
                       headers=headers, json=body)


def _reasons(body: dict) -> dict[int, str]:
    return {row["number"]: row["reason"] for row in body["stranded"]}


def _merge_workflows(db) -> list[dict]:
    """The merge tasks a redrive submitted: they carry its metadata key."""
    return [d for p, d in db.docs.items() if p.startswith("tasks/")
            and d.get("runner_profile") == "merge"
            and "stranded_pr_redrive" in (d.get("metadata") or {})]


# --------------------------------------------------------------------------
# 1. each classification, pure, in order
# --------------------------------------------------------------------------

def _pull(**overrides: Any) -> OpenPull:
    fields: dict[str, Any] = dict(
        number=7, title="t", head_ref=f"swarm/{_task_id(7)}", head_sha=_sha(7),
        base_ref="main", labels=(), created_at=NOW - timedelta(hours=5),
    )
    fields.update(overrides)
    return OpenPull(**fields)


GREEN_CI = CiFacts(state="green", required=("python",))
RED_CI = CiFacts(state="red", failing=("python",), required=("python",))
NEVER_CI = CiFacts(state="pending", never_ran=True, required=("python",))
FAILED = MergeStep(task_id="task_m", live=False, merged=False,
                   error="behind_too_often: updated 3 times", created_at=NOW)
LIVE = MergeStep(task_id="task_l", live=True, merged=False, error="", created_at=NOW)


def _classify(pull=None, *, state="clean", mergeable=True, ci=GREEN_CI, merges=()):
    return classify(pull or _pull(), mergeable_state=state, mergeable=mergeable, ci=ci,
                    merges=list(merges))


def test_a_green_clean_pull_request_with_no_merge_step_is_no_merge_step():
    verdict = _classify()
    assert verdict.reason == NO_MERGE_STEP and verdict.remedy == "merge_pr"


def test_a_hold_label_is_held_and_beats_every_other_reason():
    verdict = _classify(_pull(labels=("Hold",)), state="dirty", mergeable=False, ci=RED_CI,
                        merges=[LIVE])
    assert verdict.reason == HELD and verdict.remedy == "none"
    # Control: the same pull request without the label is not held.
    assert _classify(_pull(labels=("needs-review",)), ci=RED_CI).reason == CHECKS_RED


def test_a_live_merge_step_is_not_stranded():
    assert _classify(merges=[LIVE]) is None
    assert _classify(merges=[FAILED]).reason == MERGE_FAILED


def test_a_conflict_is_conflict_remedied_by_a_rebase():
    assert _classify(state="dirty").reason == CONFLICT
    assert _classify(state="clean", mergeable=False).reason == CONFLICT
    assert _classify(state="dirty").remedy == "rebase"
    assert _classify(state="unknown", mergeable=None).reason == NO_MERGE_STEP


def test_red_checks_are_checks_red_with_the_failing_names():
    verdict = _classify(ci=RED_CI)
    assert verdict.reason == CHECKS_RED and verdict.remedy == "fix_ci"
    assert "python" in verdict.detail


def test_ci_that_never_ran_is_ci_never_ran():
    verdict = _classify(ci=NEVER_CI)
    assert verdict.reason == CI_NEVER_RAN and verdict.remedy == "fix_ci"
    assert _classify(ci=CiFacts(state="pending", required=("python",))).reason == NO_MERGE_STEP


def test_behind_is_behind_and_names_the_last_merge_failure():
    verdict = _classify(state="behind", merges=[FAILED])
    assert verdict.reason == BEHIND and verdict.remedy == "merge_pr"
    assert "behind_too_often" in verdict.detail


def test_a_merge_step_that_ended_unmerged_is_merge_failed_with_its_error():
    verdict = _classify(merges=[FAILED])
    assert verdict.reason == MERGE_FAILED and "behind_too_often" in verdict.detail
    merged = MergeStep(task_id="task_x", live=False, merged=True, error="", created_at=NOW)
    assert _classify(merges=[merged]).reason == NO_MERGE_STEP


def test_conflict_comes_before_red_and_red_before_never_ran_and_never_ran_before_behind():
    assert _classify(state="dirty", ci=RED_CI).reason == CONFLICT
    assert _classify(state="behind", ci=RED_CI).reason == CHECKS_RED
    assert _classify(state="behind", ci=NEVER_CI).reason == CI_NEVER_RAN
    assert _classify(state="behind", ci=CiFacts(state="unread")).reason == BEHIND


# --------------------------------------------------------------------------
# 2. CI that never ran, against CI still running
# --------------------------------------------------------------------------

RULES = [{"type": "required_status_checks", "parameters": {
    "required_status_checks": [{"context": "python", "integration_id": APP}]}}]


def test_no_required_check_reported_is_never_ran_even_when_another_app_reported():
    labeler = {"name": "labeler", "status": "completed", "conclusion": "success",
               "app": {"id": 99}}
    facts = ci_facts(RULES, [labeler], [])
    assert facts.never_ran and facts.state == "pending"


def test_a_required_check_in_progress_has_run():
    running = {"name": "python", "status": "in_progress", "conclusion": None, "app": {"id": APP}}
    assert not ci_facts(RULES, [running], []).never_ran


def test_with_nothing_required_nothing_reported_is_never_ran():
    assert ci_facts([], [], []).never_ran
    ok = {"name": "x", "status": "completed", "conclusion": "success", "app": {"id": 1}}
    assert not ci_facts([], [ok], []).never_ran


# --------------------------------------------------------------------------
# 3. whose pull request it is
# --------------------------------------------------------------------------

def test_the_branch_names_the_opening_task_and_the_body_link_is_the_fallback():
    assert opener_task_id(_pull()) == _task_id(7)
    body = f"text\n\nConsole:\n- agent: https://c.example/agents/live/{_task_id(9)}\n"
    assert opener_task_id(_pull(head_ref="feature/x", body=body)) == _task_id(9)
    assert opener_task_id(_pull(head_ref="feature/x", body="Fixes #3")) is None
    assert opener_task_id(_pull(head_ref="swarm/not-a-task")) is None


# --------------------------------------------------------------------------
# 4. the sweep end to end
# --------------------------------------------------------------------------

def _seed_every_reason(db, github: GitHub) -> None:
    """One pull request per reason, plus three that are not stranded."""
    for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        _opener(db, n)
    github.green(github.add(1))                              # no_merge_step
    github.green(github.add(2, labels=("hold",)))            # held
    github.green(github.add(3, state="dirty", mergeable=False))  # conflict
    github.red(github.add(4))                                # checks_red
    github.add(5)                                            # ci_never_ran
    github.green(github.add(6, state="behind"))              # behind
    github.green(github.add(7))                              # merge_failed
    _merge_task(db, 7, number=7, state="FAILED",
                refusal={"code": "review_not_merge", "message": "the verdict is FIX"})
    github.green(github.add(8))                              # a live merge step: in flight
    _merge_task(db, 8, number=8, state="RUNNING", by_task=True)
    github.green(github.add(9, hours=1))                     # too young
    github.green(github.add(10, branch="feature/by-hand"))   # not SwarmCloud's
    github.green(github.add(12, task=11))                    # body-less, branch of task 11


def test_the_sweep_classifies_stores_and_get_serves_each_reason(client, db, github):
    _seed_every_reason(db, github)
    response = _sweep(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert _reasons(body) == {
        1: NO_MERGE_STEP, 2: HELD, 3: CONFLICT, 4: CHECKS_RED, 5: CI_NEVER_RAN,
        6: BEHIND, 7: MERGE_FAILED, 12: NO_MERGE_STEP,
    }
    assert body["in_flight"] == 1
    assert body["redrive"] is False and body["redriven"] == []
    assert "review_not_merge" in next(r for r in body["stranded"] if r["number"] == 7)["detail"]

    served = client.get("/v1/stranded-prs", headers=auth_header("alice"))
    assert served.status_code == 200, served.text
    rows = served.json()["rows"]
    assert {row["number"]: row["reason"] for row in rows} == _reasons(body)
    row = next(r for r in rows if r["number"] == 1)
    assert set(row) >= {"repo", "number", "title", "age_hours", "reason", "detail",
                        "head_sha", "remedy"}
    assert row["repo"] == REPO and row["head_sha"] == _sha(1) and row["remedy"] == "merge_pr"
    assert row["age_hours"] >= 4.9
    assert served.json()["tenants"][0]["swept_at"] is not None


def test_get_before_any_sweep_says_never_swept_rather_than_clean(client):
    body = client.get("/v1/stranded-prs", headers=auth_header("alice")).json()
    assert body["rows"] == []
    assert body["tenants"] == [{"tenant_id": "eng", "swept_at": None, "truncated": False,
                                "failures": []}]


def test_the_log_line_is_pr_stranded_with_top_level_fields(client, db, github, caplog):
    _opener(db, 1)
    github.green(github.add(1))
    with caplog.at_level(logging.WARNING, logger="swarm_api.strandedprs"):
        assert _sweep(client).status_code == 200
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "pr_stranded"]
    line = json.loads(CloudLoggingFormatter().format(record))
    # The monitoring module's metric filter and extractors read exactly these.
    assert line["event"] == strandedprs.EVENT == "pr_stranded"
    assert line["tenant_id"] == "eng" and line["reason"] == NO_MERGE_STEP
    assert line["repository"] == REPO and line["number"] == 1
    assert line["severity"] == "WARNING"


def test_only_the_sweeper_and_admins_may_sweep(client):
    assert _sweep(client, headers=auth_header("alice")).status_code == 403
    assert _sweep(client, headers=auth_header("root")).status_code == 200


# --------------------------------------------------------------------------
# 5. one entry per pull request per six hours
# --------------------------------------------------------------------------

def _logged(caplog) -> list[str]:
    return [f"{r.repository}#{r.number}" for r in caplog.records
            if getattr(r, "event", None) == "pr_stranded"]


def test_a_stranded_pull_request_is_logged_once_per_six_hours(client, db, github, caplog):
    _opener(db, 1)
    github.green(github.add(1))
    with caplog.at_level(logging.WARNING, logger="swarm_api.strandedprs"):
        first = _sweep(client).json()
        second = _sweep(client).json()
    assert first["logged"] == [f"{REPO}#1"] and second["logged"] == []
    assert _logged(caplog) == [f"{REPO}#1"]

    # Six hours later it is logged again.
    stored = db.docs["stranded_prs/eng"]
    stored["logged"][f"{REPO}#1"] = NOW - timedelta(hours=6, minutes=1)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="swarm_api.strandedprs"):
        third = _sweep(client).json()
    assert third["logged"] == [f"{REPO}#1"] and _logged(caplog) == [f"{REPO}#1"]


def test_a_pull_request_stranded_again_after_recovering_is_logged_at_once(client, db, github):
    _opener(db, 1)
    head = github.add(1)
    github.green(head)
    assert _sweep(client).json()["logged"] == [f"{REPO}#1"]
    live = _merge_task(db, 1, number=1, state="RUNNING")
    assert _sweep(client).json()["stranded"] == []
    db.docs[f"tasks/{live}"]["state"] = "CANCELLED"
    assert _sweep(client).json()["logged"] == [f"{REPO}#1"]


# --------------------------------------------------------------------------
# 6. redrive
# --------------------------------------------------------------------------

def test_redrive_merges_only_green_no_merge_step_and_behind_never_held_or_red(client, db, github):
    _register(db, "eng", OWNER, OTHER)
    other = f"{OWNER}/{OTHER}"
    for n in (1, 2, 3, 4, 5):
        _opener(db, n)
    github.green(github.add(1, repo=other))                     # green no_merge_step: redriven
    github.green(github.add(2, labels=("hold",)))               # held and green: never
    github.red(github.add(3))                                   # red: never
    github.green(github.add(4, state="behind"))                 # green behind: redriven
    head5 = github.add(5)                                       # pending: not redriven
    github.running(head5)

    body = _sweep(client, headers=auth_header("root"), body={"redrive": True}).json()
    redriven = {entry["pull_request"]: entry["head_sha"] for entry in body["redriven"]}
    assert redriven == {f"{other}#1": _sha(1), f"{REPO}#4": _sha(4)}
    refused = {entry["pull_request"]: entry["reason"] for entry in body["not_redriven"]}
    assert refused[f"{REPO}#2"] == "held"
    assert refused[f"{REPO}#3"] == "checks_red"
    assert refused[f"{REPO}#5"] == "checks_pending"

    merges = _merge_workflows(db)
    targets = sorted(
        (m["metadata"]["dispatch"]["merge_target"]["number"],
         m["metadata"]["dispatch"]["merge_target"]["head_sha"]) for m in merges
    )
    assert targets == [(1, _sha(1)), (4, _sha(4))]
    assert all(m["submitted_by"] == "alice@saga.xyz" and m["tenant_id"] == "eng" for m in merges)

    # The redriven ones now have a live merge step: in flight, not stranded.
    again = _sweep(client).json()
    assert set(_reasons(again)) == {2, 3, 5} and again["in_flight"] == 2


def test_redrive_never_touches_a_held_pull_request_whatever_else_holds(db, github):
    held = StrandedRow(tenant_id="eng", repo=REPO, number=1, title="", url="",
                       opened_at=NOW, reason=HELD, detail="", head_sha=_sha(1),
                       remedy="none", checks="green", opened_by_task=_task_id(1))
    assert redrive_refusal(held) == "held"
    red = StrandedRow(**{**held.__dict__, "reason": NO_MERGE_STEP, "checks": "red"})
    assert redrive_refusal(red) == "checks_red"
    ok = StrandedRow(**{**held.__dict__, "reason": BEHIND, "checks": "green"})
    assert redrive_refusal(ok) == ""
    failed = StrandedRow(**{**held.__dict__, "reason": MERGE_FAILED, "checks": "green"})
    assert redrive_refusal(failed) == "reason_merge_failed"


def test_redrive_submits_one_merge_per_repository_per_sweep(client, db, github):
    for n in (1, 2, 3):
        _opener(db, n)
        github.green(github.add(n, hours=10 - n))
    body = _sweep(client, headers=auth_header("root"), body={"redrive": True}).json()
    assert [entry["pull_request"] for entry in body["redriven"]] == [f"{REPO}#1"]
    refused = {e["pull_request"]: e["reason"] for e in body["not_redriven"]}
    assert refused == {f"{REPO}#2": "one_merge_per_repository_per_sweep",
                       f"{REPO}#3": "one_merge_per_repository_per_sweep"}
    # The next sweep: #1's merge is live in the repository, so nothing more.
    second = _sweep(client, headers=auth_header("root"), body={"redrive": True}).json()
    assert second["redriven"] == []
    assert {e["reason"] for e in second["not_redriven"]} == {"repository_has_a_live_merge"}


def test_the_scheduler_identity_may_not_redrive(client, db, github):
    _opener(db, 1)
    github.green(github.add(1))
    response = _sweep(client, body={"redrive": True})
    assert response.status_code == 403, response.text
    assert _merge_workflows(db) == []
    assert _sweep(client, body={"redrive": False}).status_code == 200


def test_redrive_refuses_a_submitter_who_left_the_tenant(client, db, github):
    _opener(db, 1, submitted_by="carol@saga.xyz")
    github.green(github.add(1))
    body = _sweep(client, headers=auth_header("root"), body={"redrive": True}).json()
    assert body["redriven"] == []
    assert body["not_redriven"] == [{"pull_request": f"{REPO}#1",
                                     "reason": "refused: submitter_not_member"}]
    assert _merge_workflows(db) == []


# --------------------------------------------------------------------------
# 7. tenant isolation (invariant 9)
# --------------------------------------------------------------------------

def test_tenant_b_never_sees_tenant_as_pull_requests(client, db, github, forge_tokens):
    # research registers the SAME repository; the pull request is eng's task's.
    _register(db, "research", OWNER, NAME)
    _opener(db, 1, tenant_id="eng")
    github.green(github.add(1))
    # And research's own pull request, in the same repository, is not eng's.
    _opener(db, 2, tenant_id="research", submitted_by="bob@saga.xyz")
    github.green(github.add(2))

    research = _sweep(client, tenant_id="research").json()
    assert _reasons(research) == {2: NO_MERGE_STEP}
    assert forge_tokens.asked and all("research" in secret for secret in forge_tokens.asked), (
        f"the research sweep read another tenant's token: {forge_tokens.asked}"
    )
    eng = _sweep(client, tenant_id="eng").json()
    assert _reasons(eng) == {1: NO_MERGE_STEP}

    bob = client.get("/v1/stranded-prs", headers=auth_header("bob")).json()
    assert {(r["tenant_id"], r["number"]) for r in bob["rows"]} == {("research", 2)}
    alice = client.get("/v1/stranded-prs", headers=auth_header("alice")).json()
    assert {(r["tenant_id"], r["number"]) for r in alice["rows"]} == {("eng", 1)}
    refused = client.get("/v1/stranded-prs?tenant_id=research", headers=auth_header("alice"))
    assert refused.status_code == 403


def test_each_sweep_sends_only_its_own_tenants_token(client, db, github, forge_tokens):
    _register(db, "research", OWNER, NAME)
    _opener(db, 1)
    github.green(github.add(1))
    _sweep(client, tenant_id="eng")
    eng_calls = set(github.tokens_sent_for(REPO))
    github.calls.clear()
    _sweep(client, tenant_id="research")
    research_calls = set(github.tokens_sent_for(REPO))
    assert len(eng_calls) == 1 and len(research_calls) == 1
    assert eng_calls.isdisjoint(research_calls)


def test_an_admin_reads_every_tenant_they_belong_to_and_any_one_by_name(client, db, github):
    _register(db, "research", OWNER, NAME)
    _opener(db, 1)
    github.green(github.add(1))
    _opener(db, 2, tenant_id="research", submitted_by="bob@saga.xyz")
    github.green(github.add(2))
    _sweep(client, tenant_id="eng")
    _sweep(client, tenant_id="research")
    mine = client.get("/v1/stranded-prs", headers=auth_header("root")).json()
    assert {r["tenant_id"] for r in mine["rows"]} == {"eng"}
    named = client.get("/v1/stranded-prs?tenant_id=research", headers=auth_header("root"))
    assert named.status_code == 200
    assert {r["tenant_id"] for r in named.json()["rows"]} == {"research"}
