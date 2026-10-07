"""The CI fixer's post-back, and its per-pull-request cap held by the API (#263).

`scripts/ci-fix.sh` submits ONE fix step for a red `swarm/<task>` pull
request: a one-step `direct-pr` workflow that `continues_task` the red
branch's task, with `metadata.ci_fix` naming the pull request, the red run,
its head, and `attempt` of `max_attempts`. Until this, nothing ever said on
the pull request how that step ENDED: the script comments when it submits,
and then the pull request went quiet.

What these tests hold:

  1. A red-run payload, in the script's shape, produces exactly ONE fix task,
     stamped `ci_fix.postback = "pending"` by the API (never by the caller).
  2. When that task is terminal, the per-tenant `merge_wake` tick posts
     exactly ONE comment on the pull request -- the task id, its console
     link, its state, a one-paragraph outcome -- with the tenant's own `-git`
     token, which appears in the Authorization header and nowhere else. A
     second tick posts nothing more. A task still running gets nothing.
  3. The comment says when the per-pull-request cap is reached.
  4. The cap holds at the API, not only in the script's comment count: a
     submission past `max_attempts` for one pull request is refused, and so
     is a cap above the ceiling.
  5. The comment goes only to a pull request whose head branch is the
     continued task's own, only with the named tenant's token, and a failed
     write is retried rather than lost.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import cifix, forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from swarm_common.identity import TenantMember

from . import forge_fakes
from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header, seed_task, seed_tenant

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
CONSOLE = "https://console.example.test"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
REPO_URL = "https://github.com/saga-xyz/widgets"
HEAD = "a" * 40
PUSHED = "c" * 40
PR = 77
RUN_ID = 4242
#: The CI fixer's account, listed under eng and so continuation-scoped
#: (contract request 30), as test_continuation_scope_is_narrow.py lists it.
FIXER = f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com"
FIXER_UID = "104857600000000000001"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    return forge_fakes.GitHubWrites()


@pytest.fixture
def tenant_tokens() -> forge_fakes.AnyTenantTokens:
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def fix_client(db, tokens, group_map, objects, github, tenant_tokens, clock) -> TestClient:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    tokens["token-fixer"] = {"email": FIXER, "email_verified": True, "sub": FIXER_UID}
    context = build_context(
        settings=api_settings(
            rollup_sweeper_users=(SWEEPER,), console_url=CONSOLE,
            tenant_service_accounts=(
                TenantMember(email=FIXER, kind="group", principal=ENG_GROUP, uid=FIXER_UID),
            ),
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=tenant_tokens,
        forge_writer=forgewrite.GitHubWriter(send=github),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _original(client: TestClient, user: str = "alice") -> str:
    """The `direct-pr` task whose `swarm/<id>` pull request went red."""
    response = client.post("/v1/tasks", headers=auth_header(user), json={
        "runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO_URL,
    })
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def _red_run_payload(continues: str, *, attempt: int = 1, max_attempts: int = 2,
                     pull_request: int = PR, **ci_fix: Any) -> dict[str, Any]:
    """What `scripts/ci-fix.sh run` sends (its jq program), with the test's profile."""
    return {
        "steps": [{"step_id": "ci-fix", "runner_profile": "mock",
                   "input": {"prompt": "CI failed. Fix exactly this failure."}}],
        "strategy": "direct-pr",
        "continues_task": continues,
        "metadata": {"ci_fix": {
            "pull_request": pull_request, "run_id": RUN_ID, "head_sha": HEAD,
            "attempt": attempt, "max_attempts": max_attempts, **ci_fix,
        }},
    }


def _submit(client: TestClient, payload: dict[str, Any], user: str = "alice"):
    return client.post("/v1/workflows", headers=auth_header(user), json=payload)


def _fix_task(db, response) -> dict[str, Any]:
    (step,) = response.json()["workflow"]["steps"]
    return db.docs[f"tasks/{step['task_id']}"]


def _ci_fix_tasks(db) -> list[dict[str, Any]]:
    return [doc for key, doc in db.docs.items()
            if key.startswith("tasks/") and "ci_fix" in (doc.get("metadata") or {})]


def _finish(doc: dict[str, Any], state: str = "SUCCEEDED", **fields: Any) -> None:
    doc["state"] = state
    doc["completed_at"] = NOW
    doc.update(fields)


def _tick(client: TestClient, tenant_id: str = "eng"):
    return client.post(f"/v1/admin/merges/wake?tenant_id={tenant_id}", headers=SWEEPER_HEADERS)


def _posted(github: forge_fakes.GitHubWrites, number: int = PR) -> list[str]:
    return [c["body"] for c in github.on_issue(number)]


# --------------------------------------------------------------------------
# 1 and 2. one red run: one fix dispatch, one post-back
# --------------------------------------------------------------------------

def test_a_red_run_payload_produces_one_fix_dispatch_and_one_post_back(fix_client, db, github, clock):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")

    response = _submit(fix_client, _red_run_payload(original))
    assert response.status_code == 201, response.text
    assert len(_ci_fix_tasks(db)) == 1, "one red run is one fix dispatch"
    task = _fix_task(db, response)
    assert task["metadata"]["ci_fix"]["postback"] == cifix.PENDING
    assert task["metadata"]["dispatch"]["continues"] == original

    # Still running: nothing to say yet.
    assert _tick(fix_client).status_code == 200
    assert _posted(github) == []

    _finish(task, result_summary={
        "git": {"pushed_head": PUSHED},
        "runner": {"summary": "Fixed the off-by-one in test_y; the assertion now holds."},
    })
    tick = _tick(fix_client)
    assert tick.status_code == 200, tick.text
    assert tick.json()["ci_fix"]["posted"] == 1

    (body,) = _posted(github)
    assert body.startswith(cifix.outcome_marker(task["id"]))
    assert task["id"] in body
    assert f"{CONSOLE}/agents/live/{task['id']}" in body
    assert "SUCCEEDED" in body
    assert "attempt 1 of 2" in body
    assert PUSHED[:12] in body
    assert "off-by-one" in body
    # The script's attempt marker is the only one its cap counts; the
    # post-back must never be mistaken for an attempt.
    assert "<!-- swarm-ci-fix:attempt -->" not in body

    record = db.docs[f"tasks/{task['id']}"]["metadata"]["ci_fix"]
    assert record["postback"] == cifix.POSTED
    assert isinstance(record["comment_id"], int)

    clock.now = NOW + timedelta(hours=1)
    assert _tick(fix_client).status_code == 200
    assert len(_posted(github)) == 1, "a second tick posted a second outcome"


def test_the_post_back_carries_no_token_and_no_log_dump(fix_client, db, github, tenant_tokens):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")
    task = _fix_task(db, _submit(fix_client, _red_run_payload(original)))
    token = tenant_tokens.token_for(db_tenant(db, "eng"))
    dump = "\n".join(f"log line {i}: FAILED something" for i in range(2000))
    _finish(task, "FAILED", last_error=f"agent exited 1 with {token} in its output",
            result_summary={"runner": {"summary": f"I printed {token}.\n\n{dump}"}})

    assert _tick(fix_client).status_code == 200
    (body,) = _posted(github)
    assert token not in body
    assert "log line 1999" not in body
    assert len(body) < 4000, "the outcome is a paragraph, not the log"
    assert "FAILED" in body

    # The token went to GitHub in the Authorization header, and only there.
    for _, url, headers, payload in github.calls:
        assert token not in url
        assert token not in (payload or b"").decode()
    assert any(token in h.get("Authorization", "") for _, _, h, _ in github.calls)


def db_tenant(db, tenant_id: str):
    from swarm_common.models import Tenant

    data = dict(db.docs[f"tenants/{tenant_id}"])
    return Tenant(**data)


# --------------------------------------------------------------------------
# 3. the comment says when the cap is reached
# --------------------------------------------------------------------------

def test_the_post_back_says_when_the_per_pr_cap_is_reached(fix_client, db, github):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")
    first = _fix_task(db, _submit(fix_client, _red_run_payload(original, attempt=1)))
    _finish(first)
    assert _tick(fix_client).status_code == 200
    second = _fix_task(db, _submit(fix_client, _red_run_payload(original, attempt=2)))
    _finish(second, "FAILED", last_error="the agent made no change")
    assert _tick(fix_client).status_code == 200

    one, two = _posted(github)
    assert "attempt 1 of 2" in one
    assert "cap" not in one.lower()
    assert "attempt 2 of 2" in two
    assert "cap of 2 attempts" in two.lower()
    assert "no further attempt" in two.lower()


# --------------------------------------------------------------------------
# 4. the cap holds at the API
# --------------------------------------------------------------------------

def test_the_cap_holds_at_the_api_whatever_the_caller_numbers_the_attempt(fix_client, db):
    original = _original(fix_client)
    assert _submit(fix_client, _red_run_payload(original, attempt=1)).status_code == 201
    assert _submit(fix_client, _red_run_payload(original, attempt=2)).status_code == 201

    # A third, numbered 1 again -- as it would be if the attempt comments were
    # deleted from the pull request -- is still the third for this PR.
    third = _submit(fix_client, _red_run_payload(original, attempt=1))
    assert third.status_code == 422, third.text
    assert third.json()["code"] == "invalid_dispatch"
    assert len(_ci_fix_tasks(db)) == 2

    # Another pull request's count is its own.
    other = _original(fix_client)
    assert _submit(fix_client, _red_run_payload(other, pull_request=78)).status_code == 201


@pytest.mark.parametrize("record", [
    {"attempt": 3, "max_attempts": 2},
    {"attempt": 1, "max_attempts": cifix.MAX_ATTEMPTS_CEILING + 1},
    {"attempt": 0},
    {"pull_request": 0},
    {"head_sha": "not-a-sha"},
    {"postback": "posted"},
    {"comment_id": 1},
])
def test_a_malformed_or_self_stamped_ci_fix_record_is_refused(fix_client, db, record):
    original = _original(fix_client)
    response = _submit(fix_client, _red_run_payload(original, **record))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_dispatch"
    assert _ci_fix_tasks(db) == []


def test_a_ci_fix_record_needs_a_continuation(fix_client, db):
    payload = _red_run_payload("unused")
    del payload["continues_task"]
    payload["repository_url"] = REPO_URL
    response = _submit(fix_client, payload)
    assert response.status_code == 422, response.text
    assert _ci_fix_tasks(db) == []


def test_a_ci_fix_record_on_a_step_is_refused(fix_client, db):
    original = _original(fix_client)
    payload = _red_run_payload(original)
    payload["steps"][0]["metadata"] = {"ci_fix": payload.pop("metadata")["ci_fix"]}
    response = _submit(fix_client, payload)
    assert response.status_code == 422, response.text
    assert _ci_fix_tasks(db) == []


# --------------------------------------------------------------------------
# 5. where it posts, with what, and when a write fails
# --------------------------------------------------------------------------

def _integrator(client: TestClient, user: str = "alice") -> str:
    """An `integrate` workflow's integrator: the task that opens a lane's pull
    request from `swarm/<its own id>`."""
    response = client.post("/v1/workflows", headers=auth_header(user), json={
        "steps": [
            {"step_id": "build", "runner_profile": "mock"},
            {"step_id": "publish", "runner_profile": "mock", "depends_on": ["build"]},
        ],
        "strategy": "integrate",
        "repository_url": REPO_URL,
    })
    assert response.status_code == 201, response.text
    return {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}["publish"]


def test_the_fixers_integrator_fix_is_posted_back_on_the_integrators_pull_request(
    fix_client, db, github
):
    """PR #740's case, end to end (owner decision 2026-10-06): the listed
    fixer continues an integrator's red pull request, the fix is stamped and
    counted, and its outcome lands on the pull request of `swarm/<integrator>`."""
    integrator = _integrator(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{integrator}")

    response = fix_client.post(
        "/v1/workflows", headers={"Authorization": "Bearer token-fixer"},
        json=_red_run_payload(integrator),
    )
    assert response.status_code == 201, response.text
    task = _fix_task(db, response)
    assert task["submitted_by"] == FIXER
    assert task["metadata"]["dispatch"]["continues"] == integrator
    assert task["metadata"]["ci_fix"]["postback"] == cifix.PENDING

    _finish(task, result_summary={"git": {"pushed_head": PUSHED}})
    tick = _tick(fix_client)
    assert tick.status_code == 200, tick.text
    assert tick.json()["ci_fix"]["posted"] == 1
    (body,) = _posted(github)
    assert body.startswith(cifix.outcome_marker(task["id"]))
    assert PUSHED[:12] in body


def test_a_pull_request_on_another_branch_gets_no_comment(fix_client, db, github):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref="swarm/task_ffffffffffffffffffff")
    task = _fix_task(db, _submit(fix_client, _red_run_payload(original)))
    _finish(task)

    tick = _tick(fix_client)
    assert tick.status_code == 200
    assert _posted(github) == []
    assert {"task_id": task["id"], "code": "pull_request_mismatch"} in tick.json()["ci_fix_failures"]
    assert db.docs[f"tasks/{task['id']}"]["metadata"]["ci_fix"]["postback"] == cifix.GAVE_UP


def test_a_stamp_on_a_task_that_continues_nothing_is_never_posted(fix_client, db, github):
    """`postback` is the API's stamp; a seeded one without a continuation is refused."""
    doc = seed_task(db, task_id="task_forged", tenant_id="eng", state="SUCCEEDED")
    doc["repository_url"] = REPO_URL
    doc["metadata"] = {"ci_fix": {"pull_request": PR, "attempt": 1, "max_attempts": 2,
                                  "postback": cifix.PENDING}}
    github.open_pull(PR, HEAD, ref="swarm/task_forged")

    assert _tick(fix_client).status_code == 200
    assert _posted(github) == []
    assert doc["metadata"]["ci_fix"]["postback"] == cifix.GAVE_UP


def test_only_the_named_tenants_fixes_are_posted_with_its_token(fix_client, db, github, tenant_tokens):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")
    task = _fix_task(db, _submit(fix_client, _red_run_payload(original)))
    _finish(task)

    assert _tick(fix_client, "research").status_code == 200
    assert _posted(github) == []
    assert tenant_tokens.asked == [], "an empty tick read a token"

    assert _tick(fix_client, "eng").status_code == 200
    assert len(_posted(github)) == 1
    assert tenant_tokens.asked == ["swarm-tenant-eng-git"]


def test_a_failed_write_is_retried_and_not_lost(fix_client, db, github, clock):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")
    task = _fix_task(db, _submit(fix_client, _red_run_payload(original)))
    _finish(task)

    github.status = {"POST": 502}
    tick = _tick(fix_client)
    assert tick.status_code == 200
    assert tick.json()["ci_fix"]["failed"] == 1
    record = db.docs[f"tasks/{task['id']}"]["metadata"]["ci_fix"]
    assert record["postback"] == cifix.PENDING
    assert record["postback_failures"] == 1

    github.status = {}
    clock.now = NOW + timedelta(seconds=cifix.RETRY_SECONDS)
    assert _tick(fix_client).status_code == 200
    assert len(_posted(github)) == 1


def test_a_comment_posted_before_its_record_was_lost_is_not_posted_twice(fix_client, db, github):
    original = _original(fix_client)
    github.open_pull(PR, HEAD, ref=f"swarm/{original}")
    task = _fix_task(db, _submit(fix_client, _red_run_payload(original)))
    _finish(task)
    github.add_comment(PR, cifix.outcome_marker(task["id"]) + "\nan earlier write")

    assert _tick(fix_client).status_code == 200
    assert len(_posted(github)) == 1
    assert db.docs[f"tasks/{task['id']}"]["metadata"]["ci_fix"]["postback"] == cifix.POSTED
