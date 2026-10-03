"""An issue run writes back to its issue: the plan, one status comment, the PR keyword (#454).

What these tests hold, in the order they matter:

  1. THE RUN NEVER WAITS ON THE COMMENT. A 403, a missing credential, GitHub
     down: the run moves exactly as it would have, and the failure is
     recorded on it, redacted, naming the permission the credential lacks.
  2. THE TOKEN GOES IN THE AUTHORIZATION HEADER AND NOWHERE ELSE -- not a
     URL, not a comment, not the run document -- and it is the run's own
     tenant's (invariant 9).
  3. ONE PLAN COMMENT AND ONE STATUS COMMENT per run, edited in place, found
     again by their marker when the stored id is lost.
  4. TEXT SOMEONE ELSE WROTE PINGS NOBODY AND CLOSES NOTHING, and every body
     stays under GitHub's limit.
  5. `Closes #N` only when every planned requirement is confirmed.

GitHub is a fake TRANSPORT under the shipped `forgewrite.GitHubWriter`, so
the status mapping, the host pin and the headers are the real code. Every
token is built at runtime. No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api import forge, forgewrite, issuecomments, issuesync
from swarm_api.auth import StaticTokenVerifier
from swarm_api.codec import run_console_url
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import NotFound
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import IssueRef
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header
from .test_issue_runs import PLAN, _approve, _create, _docs, _edit, _finish_planner, _planned, _run

CONSOLE = "https://console.example.test"
REPO_ROOT = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def writes():
    return forge_fakes.GitHubWrites()


@pytest.fixture
def forge_tokens():
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def api_context(db, tokens, group_map, objects, forge_tokens, writes, clock):
    return build_context(
        settings=api_settings(console_url=CONSOLE),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=forge_fakes.GitHub(issues=[forge_fakes.issue(42)])),
        forge_writer=forgewrite.GitHubWriter(send=writes),
    )


def _doc(db, run_id: str) -> dict:
    return db.docs[f"issue_runs/{run_id}"]


def _bodies(writes, kind: str, run_id: str) -> list[dict]:
    mark = issuecomments.marker(run_id, kind)
    return [c for c in writes.on_issue(42) if c["body"].startswith(mark)]


def _eng_token(forge_tokens) -> str:
    return forge_tokens.issued["swarm-tenant-eng-git"]


# --------------------------------------------------------------------------
# 3. one plan comment, one status comment, edited in place
# --------------------------------------------------------------------------

def test_a_new_run_posts_one_status_comment_and_no_plan_yet(client, db, writes):
    run = _create(client).json()["run"]
    statuses = _bodies(writes, "status", run["id"])
    assert len(statuses) == 1
    assert "planning" in statuses[0]["body"]
    assert not _bodies(writes, "plan", run["id"])
    assert run["status_comment_id"] == statuses[0]["id"]
    assert run["plan_comment_id"] is None


def test_the_plan_comment_is_posted_once_and_edited_by_an_edit(client, db, objects, writes):
    run = _planned(client, db, objects)
    plans = _bodies(writes, "plan", run["id"])
    assert len(plans) == 1
    assert run["plan_comment_id"] == plans[0]["id"]
    assert "Approval: pending" in plans[0]["body"]
    assert run["plan_digest"] in plans[0]["body"]

    edited = _edit(client, run["id"], run["plan_digest"], {**PLAN, "summary": "Sort by date too."})
    assert edited.status_code == 200, edited.text
    plans = _bodies(writes, "plan", run["id"])
    assert len(plans) == 1, "an edit edits the comment, it does not post a second"
    assert plans[0]["id"] == run["plan_comment_id"]
    assert "Sort by date too." in plans[0]["body"]
    assert "**Revision:** 2" in plans[0]["body"]


def test_the_status_comment_is_edited_in_place_through_approval_and_done(client, db, objects, writes):
    run = _planned(client, db, objects)
    status_id = run["status_comment_id"]
    running = _approve(client, run["id"], run["plan_digest"]).json()["run"]
    for doc in _docs(db, "tasks").values():
        if doc.get("workflow_id") == running["workflow_id"]:
            doc["state"] = "SUCCEEDED"
    done = _run(client, run["id"]).json()["run"]
    assert done["state"] == "DONE"
    statuses = _bodies(writes, "status", run["id"])
    assert [c["id"] for c in statuses] == [status_id]
    assert "### SwarmCloud: done" in statuses[0]["body"]
    assert f"{CONSOLE}/runs/{run['id']}" in statuses[0]["body"]
    assert f"{CONSOLE}/workflows/{running['workflow_id']}" in statuses[0]["body"]
    # The plan comment says who approved, by the address's local part only.
    plan = _bodies(writes, "plan", run["id"])[0]["body"]
    assert "**Approved** by alice" in plan
    assert "alice@saga.xyz" not in plan
    assert len(writes.on_issue(42)) == 2


def test_a_read_that_changes_nothing_writes_nothing_and_reads_no_token(client, db, objects, writes, forge_tokens):
    run = _planned(client, db, objects)
    calls, asked = len(writes.calls), len(forge_tokens.asked)
    for _ in range(3):
        assert _run(client, run["id"]).status_code == 200
    assert len(writes.calls) == calls
    assert len(forge_tokens.asked) == asked


def test_a_lost_comment_id_is_recovered_by_its_marker_not_posted_again(client, db, objects, writes):
    run = _planned(client, db, objects)
    ids = (run["plan_comment_id"], run["status_comment_id"])
    doc = _doc(db, run["id"])
    # A write that succeeded and a patch that did not: no ids, no digests.
    for name in ("plan_comment_id", "status_comment_id", "last_plan_posted", "last_status_posted"):
        doc[name] = None
    healed = _run(client, run["id"]).json()["run"]
    assert (healed["plan_comment_id"], healed["status_comment_id"]) == ids
    assert len(writes.on_issue(42)) == 2
    assert ("POST", "/repos/saga-xyz/widgets/issues/42/comments") not in writes.writes()[2:]


def test_a_comment_quoting_the_marker_is_not_adopted(client, db, objects, writes):
    run = _create(client).json()["run"]
    mark = issuecomments.marker(run["id"], "plan")
    # Someone else's comment, opening with our marker: not ours.
    stranger = writes.add_comment(42, mark + "\nhijack", login="mallory")
    # And one that quotes it mid-text.
    writes.add_comment(42, "see " + mark)
    _finish_planner(db, objects, run, PLAN)
    planned = _run(client, run["id"]).json()["run"]
    assert planned["plan_comment_id"] != stranger
    assert writes.comments[stranger]["body"].endswith("hijack")


def test_a_comment_deleted_on_github_is_posted_again(client, db, objects, writes):
    run = _planned(client, db, objects)
    del writes.comments[run["status_comment_id"]]
    _edit(client, run["id"], run["plan_digest"], {**PLAN, "summary": "Again."})
    assert len(_bodies(writes, "status", run["id"])) == 1


# --------------------------------------------------------------------------
# 1. the run never waits on the comment
# --------------------------------------------------------------------------

def test_a_403_is_recorded_on_the_run_and_never_raised(client, db, objects, writes, forge_tokens):
    writes.status = {"POST": 403, "PATCH": 403}
    created = _create(client)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    doc = _doc(db, run["id"])
    assert doc["state"] == "PLANNING"
    assert "writeback_forbidden" in doc["writeback_error"]
    assert "issues: write" in doc["writeback_error"]
    assert _eng_token(forge_tokens) not in doc["writeback_error"]
    assert "writeback_error" not in run, "to_api serves the ids and the PR link only"
    # The planner still finishes, the run still moves.
    _finish_planner(db, objects, run, PLAN)
    assert _run(client, run["id"]).json()["run"]["state"] == "PLANNED"


def test_the_same_failed_write_is_not_retried_on_every_read(client, db, objects, writes, clock):
    writes.status = {"POST": 503}
    run = _create(client).json()["run"]
    tried = len(writes.calls)
    _run(client, run["id"])
    assert len(writes.calls) == tried, "inside RETRY_SECONDS the same write waits"
    writes.status = {}
    clock.at += timedelta(seconds=issuesync.RETRY_SECONDS + 1)
    _run(client, run["id"])
    doc = _doc(db, run["id"])
    assert doc["writeback_error"] is None
    assert doc["status_comment_id"] in writes.comments


def test_a_tenant_without_a_forge_credential_still_gets_a_run(db, tokens, group_map, objects, writes, clock):
    from fastapi.testclient import TestClient
    from swarm_api.main import create_app

    # The open-work read has its own token reader; the write-back's is this one.
    class ReadOnly(forge_fakes.AnyTenantTokens):
        calls = 0

        def token_for(self, tenant):
            ReadOnly.calls += 1
            if ReadOnly.calls > 1:
                raise forge.NoForgeCredential("tenant 'eng' has no forge credential")
            return super().token_for(tenant)

    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, now=clock, forge_tokens=ReadOnly(),
        forge=forge.GitHubIssues(send=forge_fakes.GitHub()),
        forge_writer=forgewrite.GitHubWriter(send=writes),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    created = client.post("/v1/runs", headers=auth_header("alice"), json={"issue": "saga-xyz/widgets#42"})
    assert created.status_code == 201, created.text
    assert "no_forge_credential" in _doc(db, created.json()["run"]["id"])["writeback_error"]
    assert not writes.calls


# --------------------------------------------------------------------------
# 2. the token: the Authorization header, the run's own tenant, nowhere else
# --------------------------------------------------------------------------

def test_the_token_is_only_ever_in_the_authorization_header(client, db, objects, writes, forge_tokens):
    run = _planned(client, db, objects)
    _approve(client, run["id"], run["plan_digest"])
    token = _eng_token(forge_tokens)
    assert writes.calls
    for method, url, headers, body in writes.calls:
        assert url.startswith("https://api.github.com/repos/saga-xyz/widgets/")
        assert token not in url
        assert token not in (body or b"").decode()
        assert headers["Authorization"] == f"Bearer {token}"
        assert [k for k, v in headers.items() if token in v] == ["Authorization"]
    assert token not in json.dumps(_doc(db, run["id"]), default=str)
    assert set(forge_tokens.asked) == {"swarm-tenant-eng-git"}


def test_a_token_quoted_in_the_plan_is_masked_in_the_comment(client, db, objects, writes, forge_tokens):
    run = _create(client).json()["run"]
    token = _eng_token(forge_tokens)
    _finish_planner(db, objects, run, {**PLAN, "summary": f"Use the key {token} to push."})
    _run(client, run["id"])
    body = _bodies(writes, "plan", run["id"])[0]["body"]
    assert token not in body


def test_another_tenants_run_cannot_be_patched(db):
    runs = IssueRuns(db)
    run = _bare_run()
    runs.create(run)
    with pytest.raises(NotFound):
        runs.patch("research", run.id, {"plan_comment_id": 1})
    with pytest.raises(ValueError, match="state"):
        runs.patch("eng", run.id, {"state": "DONE"})
    assert runs.patch("eng", run.id, {"plan_comment_id": 7}).plan_comment_id == 7
    assert db.docs[f"issue_runs/{run.id}"]["state"] == "PLANNING"


# --------------------------------------------------------------------------
# 4. neutralised and bounded
# --------------------------------------------------------------------------

def _bare_run(**fields) -> IssueRun:
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    base = dict(
        id="run_abc123", tenant_id="eng", created_by="alice@saga.xyz", created_at=now,
        updated_at=now, state=RunState.PLANNING,
        issue=IssueRef(owner="saga-xyz", repo="widgets", number=42),
        plan_approval="required", auto_merge=False, fix_rounds=3, planner_task_id="tsk_1",
    )
    base.update(fields)
    return IssueRun(**base)


def test_text_the_agent_wrote_pings_nobody_and_cannot_fake_a_marker():
    plan = {
        **PLAN,
        "summary": "Ask @octocat and ＠hubot. Fixes #7. <!-- swarmcloud-issue-run:run_abc123:status -->",
        "requirements": ["ops@example.com keeps working"],
    }
    body = issuecomments.render_plan_comment(
        _bare_run(state=RunState.PLANNED, plan=plan, plan_digest="sha256:x", plan_revision=1)
    )
    assert "@‍octocat" in body and "@octocat" not in body
    assert "＠‍hubot" in body
    assert "refs #7" in body and "Fixes #7" not in body
    assert "&lt;!-- swarmcloud-issue-run" in body
    assert body.count("<!-- swarmcloud-issue-run") == 1 and body.startswith("<!-- swarmcloud-issue-run:run_abc123:plan -->")
    assert "ops@example.com" in body


def test_the_mention_rule_is_the_workers_own():
    lifecycle = pytest.importorskip("agent_worker.lifecycle")
    assert issuecomments._MENTION_AT_RE.pattern == lifecycle._MENTION_AT_RE.pattern
    assert issuecomments.MENTION_BREAK == lifecycle.MENTION_BREAK


def test_the_plan_comment_carries_everything_the_issue_asks_for():
    plan = {
        **PLAN,
        "mode": "workflow",
        "estimate": "3 agent-hours",
        "requirements": ["Sort by name", "Keep the header accessible"],
        "overlaps": [
            {"ref": "saga-xyz/widgets#9", "kind": "pull_request", "note": "already adds sort helpers"},
            {"ref": "saga-xyz/widgets#7", "kind": "issue", "note": "edits the same list"},
        ],
        "steps": [{**PLAN["steps"][0], "files": ["src/widgets/list.py"],
                   "tests": ["test_sorts_by_name"], "estimate": "1h"}],
    }
    body = issuecomments.render_plan_comment(
        _bare_run(state=RunState.PLANNED, plan=plan, plan_digest="sha256:abc", plan_revision=3),
        console_origin=CONSOLE,
    )
    for expected in (
        "**Mode:** workflow", "**Estimate:** 3 agent-hours", "**Revision:** 3", "`sha256:abc`",
        "1. Sort by name", "2. Keep the header accessible",
        "[saga-xyz/widgets#9](https://github.com/saga-xyz/widgets/pull/9) (pull request): already adds sort helpers",
        "[saga-xyz/widgets#7](https://github.com/saga-xyz/widgets/issues/7) (issue)",
        "`src/widgets/list.py`", "test_sorts_by_name", "— 1h", "Approval: pending",
        f"{CONSOLE}/runs/run_abc123",
    ):
        assert expected in body, expected


def test_every_body_stays_under_githubs_limit():
    huge_step = {"step_id": "s", "title": "t" * 200, "prompt": "p" * 16_000,
                 "files": ["f" * 300] * 60, "tests": ["x" * 500] * 30}
    plan = {
        "summary": "s" * 4_000,
        "requirements": ["r" * 500] * 40,
        "overlaps": [{"ref": "a/b#1", "kind": "issue", "note": "n" * 1_000}] * 30,
        "steps": [{**huge_step, "step_id": f"s{n}"} for n in range(8)],
    }
    run = _bare_run(state=RunState.FAILED, plan=plan, plan_digest="sha256:x", error="e" * 50_000)
    plan_body = issuecomments.render_plan_comment(run, console_origin=CONSOLE)
    status_body = issuecomments.render_status_comment(run, console_origin=CONSOLE)
    for body in (plan_body, status_body):
        assert len(body) <= issuecomments.MAX_BODY_CHARS < 65_536
        assert body.startswith("<!-- swarmcloud-issue-run:run_abc123:")
    pr_body = issuecomments.apply_keyword_block(
        "b" * 100_000, run, issuecomments.keyword_block(run, closes=False, unmet=["x"] * 100)
    )
    assert len(pr_body) <= issuecomments.MAX_BODY_CHARS
    assert pr_body.rstrip().endswith(issuecomments.keyword_end(run.id))


def test_the_status_comment_shows_the_pull_request_the_fix_round_and_the_failure():
    run = _bare_run(
        state=RunState.RUNNING, workflow_id="wf_1", ci_fix_round=2,
        pull_request={"number": 57, "url": "https://github.com/saga-xyz/widgets/pull/57",
                      "head_sha": "a" * 40, "checks": "red"},
    )
    body = issuecomments.render_status_comment(run, console_origin=CONSOLE)
    assert "### SwarmCloud: CI red, fixing" in body
    assert "#57" in body and "**CI fix round:** 2 of 3" in body
    failed = issuecomments.render_status_comment(
        _bare_run(state=RunState.FAILED, error="tests failed: @octocat broke it")
    )
    assert "### SwarmCloud: failed" in failed and "@‍octocat" in failed


@pytest.mark.parametrize(
    "fields,phase",
    [
        ({"state": RunState.PLANNED}, "plan awaiting approval"),
        ({"state": RunState.RUNNING}, "running"),
        ({"state": RunState.RUNNING, "pull_request": {"number": 1}}, "pull request opened"),
        ({"state": RunState.RUNNING, "pull_request": {"number": 1, "checks": "green"}}, "checks green"),
        ({"state": RunState.DONE, "pull_request": {"number": 1, "merged": True}}, "merged"),
        ({"state": RunState.REJECTED}, "plan rejected"),
    ],
)
def test_the_status_phase_follows_the_run(fields, phase):
    assert issuecomments.status_phase(_bare_run(**fields)) == phase


# --------------------------------------------------------------------------
# 5. the pull request body's keyword
# --------------------------------------------------------------------------

def test_closes_only_when_every_requirement_is_confirmed():
    run = _bare_run(plan={**PLAN, "requirements": ["a", "b"]})
    assert "Closes #42" in issuecomments.keyword_block(run, closes=True)
    partial = issuecomments.keyword_block(run, closes=True, unmet=["b, @octocat"])
    assert "Closes" not in partial and "part of #42" in partial and "b, @‍octocat" in partial
    assert "part of #42" in issuecomments.keyword_block(run, closes=False)
    unlisted = issuecomments.keyword_block(_bare_run(plan=PLAN), closes=True)
    assert "Closes" not in unlisted and "no requirements" in unlisted


def test_the_block_replaces_itself_and_no_other_line_closes_the_issue():
    run = _bare_run(plan={**PLAN, "requirements": ["a"]})
    first = issuecomments.apply_keyword_block(
        "Fixes #42\n\nDoes the thing.", run, issuecomments.keyword_block(run, closes=False)
    )
    assert "Fixes #42" not in first and "refs #42" in first and "part of #42" in first
    second = issuecomments.apply_keyword_block(first, run, issuecomments.keyword_block(run, closes=True))
    assert second.count(issuecomments.marker(run.id, "keyword")) == 1
    assert "Closes #42" in second and "part of #42" not in second
    assert re.findall(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#42", second) == ["Closes #42"]


def test_sync_pull_request_records_the_pr_and_writes_the_keyword(client, db, objects, writes, api_context):
    run = _planned(client, db, objects)
    writes.pulls[57] = {
        "number": 57, "html_url": "https://github.com/saga-xyz/widgets/pull/57",
        "head": {"sha": "c" * 40, "ref": "swarm/x"}, "body": "Closes #42 maybe", "state": "open",
        "merged": False,
    }
    stored = IssueRuns(db).get("eng", run["id"])
    after = issuesync.sync_pull_request(api_context, stored, 57, closes=False, unmet=["b"])
    assert after.pull_request["head_sha"] == "c" * 40
    assert after.to_api()["pull_request"] == {"number": 57, "url": "https://github.com/saga-xyz/widgets/pull/57"}
    assert "part of #42" in writes.pulls[57]["body"] and "Closes #42" not in writes.pulls[57]["body"]
    assert "#57" in _bodies(writes, "status", run["id"])[0]["body"]


# --------------------------------------------------------------------------
# the writer on its own
# --------------------------------------------------------------------------

REF = IssueRef(owner="saga-xyz", repo="widgets", number=42)


@pytest.mark.parametrize(
    "status,error,needs",
    [
        (403, forgewrite.ForgeWriteForbidden, "issues: write"),
        (401, forgewrite.ForgeWriteUnauthorized, "expired"),
        (404, forgewrite.ForgeWriteNotFound, "not found"),
        (302, forgewrite.ForgeWriteError, "never followed"),
        (500, forgewrite.ForgeWriteError, "HTTP 500"),
    ],
)
def test_each_refusal_is_its_own_code_and_never_quotes_the_token(status, error, needs):
    token = "ghp_" + "x" * 36

    def send(method, url, headers, body, timeout):
        return status, json.dumps({"message": f"bad credential {token}"}).encode()

    with pytest.raises(error) as refused:
        forgewrite.GitHubWriter(send=send).create_comment(REF, "hi", token)
    assert needs in refused.value.message
    assert token not in refused.value.message
    assert refused.value.__cause__ is None


def test_a_pull_request_write_403_names_the_pull_request_permission():
    token = "ghp_" + "y" * 36
    with pytest.raises(forgewrite.ForgeWriteForbidden, match="pull_requests: write"):
        forgewrite.GitHubWriter(send=lambda *a: (403, b"{}")).edit_pull_body(REF, 5, "b", token)


def test_a_transport_failure_reports_its_type_only():
    token = "ghp_" + "z" * 36

    def send(method, url, headers, body, timeout):
        raise OSError(f"connection to {url} with {headers['Authorization']} failed")

    with pytest.raises(forgewrite.ForgeWriteError) as refused:
        forgewrite.GitHubWriter(send=send).edit_comment(REF, 1, "b", token)
    assert "OSError" in refused.value.message and token not in refused.value.message


def test_the_production_transport_sends_to_api_github_com_only():
    with pytest.raises(forgewrite.ForgeWriteError, match="api.github.com only"):
        forgewrite._urllib_send("POST", "https://example.com/repos/a/b", {}, b"{}", 1.0)


def test_the_console_run_link_is_the_route_paths_ts_serves():
    paths = (REPO_ROOT / "apps/swarm-ui/src/paths.ts").read_text()
    assert "return `/runs/${encodeURIComponent(run)}`" in paths
    assert run_console_url(CONSOLE + "/", "run_a b") == f"{CONSOLE}/runs/run_a%20b"
    assert run_console_url("", "run_1") is None
