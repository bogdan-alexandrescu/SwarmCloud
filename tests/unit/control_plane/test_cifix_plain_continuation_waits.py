"""A plain continuation of a pull request's branch waits while a fix on it is live (#2535).

Measured 2026-10-26 on PR #2473: wf_3bde5fa9's plain `implement`
continuation of task_577523f9 was submitted at 19:23:00Z, three seconds
after the platform's CI fixer (wf_cd10182a, task_b0abb81d, 19:22:56Z)
started on the same red head 377c3010. Both fixed the same
drawer.reread.test.tsx; the fixer pushed 6260cff5 first, and the
continuation's publish was refused `head_moved`. `cifix.stamp` checked for
a live fixer only on a `metadata.ci_fix` request, and returned early for
every other continuation.

So `stamp` refuses a pushing continuation that carries no pull-request
writer mark of its own 409 `pr_fix_in_flight` while a live CI fix or
conflict fix (`live_pr_writer`, every `PR_WRITER_KINDS`) holds the pull
request its branch task recorded (`result_summary.git.pull_request.number`),
naming the live task, its workflow, its kind and the pull request. Nothing
is written. The same holds for a `publish_to_pr` workflow -- the path wf_3bde5fa9
actually took -- read by the pull request's number in the spec's repository.
A merge-only continuation and the writers' own requests (each has its own
gate) are left as they were.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from swarm_api import cifix, refusals
from swarm_api.continuation import Continuation
from swarm_api.schemas import WorkflowCreate
from swarm_api.schedtypes import pr_shepherd
from swarm_api.store import Store
from swarm_api.validation import CONFLICT_ROUND_METADATA_KEY

from .conftest import auth_header, seed_task
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    HEAD,
    PR,
    REPO_URL,
    RUN_ID,
    _integrator,
    _members_hold_grants,
    _red_run_payload,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

FIXER_HEADERS = {"Authorization": "Bearer token-fixer"}
LIVE_TASK = "task_" + "d4" * 10
LIVE_WORKFLOW = "wf_" + "d4" * 4
SWITCH = refusals.env_name("pr_fix_in_flight")


@pytest.fixture(autouse=True)
def _shipped_position(monkeypatch):
    """The switch as it ships: nothing in the environment."""
    monkeypatch.delenv(SWITCH, raising=False)


def _root(client, db, *, number: int | None = PR) -> str:
    """The integrator whose `swarm/<id>` branch the pull request is on, with
    the pull request it opened recorded as the worker records it."""
    root = _integrator(client)
    if number is not None:
        db.docs[f"tasks/{root}"]["result_summary"] = {"git": {"pull_request": {"number": number}}}
    return root


def _live_fix(db, continues: str, *, kind: str = cifix.CI_FIX_METADATA_FIELD,
              state: str = "RUNNING", tenant_id: str = "eng", pull_request: int = PR) -> None:
    """A fix continuing `continues` and marked `kind` for `pull_request`, in `state`."""
    seed_task(db, task_id=LIVE_TASK, tenant_id=tenant_id, state=state,
              runner_profile="claude-code", workflow_id=LIVE_WORKFLOW)
    db.docs[f"tasks/{LIVE_TASK}"]["metadata"] = {
        "dispatch": {"strategy": "direct-pr", "continues": continues},
        kind: {"pull_request": pull_request, "head_sha": "1" * 40},
    }


def _unwritten_claim(db, root: str, clock) -> None:
    """A ci_fix claim on PR whose task is not written yet."""
    db.docs[f"{cifix.CLAIMS}/{'e5' * 20}"] = {
        "tenant_id": "eng", "repository": "example-org/widgets", "pull_request": PR,
        "head_sha": HEAD, "run_id": RUN_ID, "root_task_id": root,
        "claim_id": "f6" * 16, "claimed_at": clock.now,
    }


def _continuation(root: str, **metadata: Any) -> dict[str, Any]:
    """A plain `implement` continuation of `root`, as wf_3bde5fa9's was."""
    payload: dict[str, Any] = {
        "steps": [{"step_id": "implement", "runner_profile": "mock",
                   "input": {"prompt": "Fix the failing test."}}],
        "strategy": "direct-pr",
        "continues_task": root,
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def _submit(client, payload: dict[str, Any]):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=payload)


def _tasks(db) -> set[str]:
    return {key for key in db.docs if key.startswith("tasks/")}


def test_a_plain_continuation_beside_a_live_ci_fix_is_refused_naming_it(fix_client, db):
    root = _root(fix_client, db)
    fixed = fix_client.post("/v1/workflows", headers=FIXER_HEADERS, json=_red_run_payload(root))
    assert fixed.status_code == 201, fixed.text
    (step,) = fixed.json()["workflow"]["steps"]
    before = _tasks(db)

    response = _submit(fix_client, _continuation(root))

    # MUTATION: drop the live-writer read from stamp's non-ci_fix branch --
    # 201, and two agents fix one red head, as on PR #2473.
    assert response.status_code == 409, response.text
    body = response.json()
    # MUTATION: answer with ci_fix_in_flight -- the issue run's round reads
    # only this code as "wait".
    assert body["code"] == "pr_fix_in_flight"
    # MUTATION: leave the live task, its workflow, its kind or the pull request out of the detail.
    assert body["detail"]["task_id"] == step["task_id"]
    assert body["detail"]["workflow_id"] == fixed.json()["workflow"]["workflow_id"]
    assert body["detail"]["kind"] == "ci_fix"
    assert body["detail"]["pull_request"] == PR
    assert step["task_id"] in body["message"]
    # MUTATION: refuse after the submission writes -- a refused request leaves a task behind.
    assert _tasks(db) == before


def test_a_seeded_live_ci_fix_task_refuses_too(fix_client, db):
    root = _root(fix_client, db)
    _live_fix(db, root, state="QUEUED")
    response = _submit(fix_client, _continuation(root))
    # MUTATION: count only RUNNING as live -- a queued fix is the one that pushes next.
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["task_id"] == LIVE_TASK
    assert response.json()["detail"]["workflow_id"] == LIVE_WORKFLOW


def test_a_claim_not_yet_written_refuses_inside_the_grace_and_not_past_it(fix_client, db, clock):
    root = _root(fix_client, db)
    _unwritten_claim(db, root, clock)

    inside = _submit(fix_client, _continuation(root))
    # MUTATION: read tasks only, never claims -- a fixer still submitting is missed.
    assert inside.status_code == 409, inside.text
    assert inside.json()["code"] == "pr_fix_in_flight"
    assert inside.json()["detail"]["kind"] == "ci_fix"
    assert inside.json()["detail"]["task_id"] is None

    clock.now += timedelta(seconds=cifix.CLAIM_GRACE_SECONDS + 1)
    past = _submit(fix_client, _continuation(root))
    # MUTATION: read the wall clock, not the store's -- the grace never ends here.
    assert past.status_code == 201, past.text


@pytest.mark.parametrize("kind", [pr_shepherd.METADATA_KEY, CONFLICT_ROUND_METADATA_KEY])
def test_a_live_conflict_fix_refuses_naming_its_kind(fix_client, db, kind):
    root = _root(fix_client, db)
    _live_fix(db, root, kind=kind, state="PARKED")
    before = _tasks(db)

    response = _submit(fix_client, _continuation(root))

    # MUTATION: pass kinds=(ci_fix,) alone -- a conflict fix's merge of the base is raced.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "pr_fix_in_flight"
    assert response.json()["detail"]["kind"] == kind
    assert response.json()["detail"]["task_id"] == LIVE_TASK
    assert _tasks(db) == before


@pytest.mark.parametrize("where", [
    {"state": "SUCCEEDED"}, {"state": "FAILED"}, {"pull_request": PR + 1}, {"tenant_id": "research"},
], ids=["succeeded", "failed", "another_pull_request", "another_tenant"])
def test_a_terminal_or_other_pr_or_other_tenant_fix_does_not_refuse(fix_client, db, where):
    root = _root(fix_client, db)
    _live_fix(db, root, **where)
    response = _submit(fix_client, _continuation(root))
    # MUTATION: drop the state, pull-request or tenant check (invariant 9).
    assert response.status_code == 201, response.text


def test_a_root_with_no_recorded_pull_request_is_admitted(fix_client, db):
    root = _root(fix_client, db, number=None)
    _live_fix(db, root)
    response = _submit(fix_client, _continuation(root))
    # MUTATION: fall back to the live fix's own number -- a branch with no
    # pull request of record is refused on a guess.
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("number", [0, -3, True, "77"], ids=["zero", "negative", "bool", "string"])
def test_a_malformed_recorded_number_is_read_as_none(fix_client, db, number):
    root = _root(fix_client, db, number=number)
    _live_fix(db, root, pull_request=1 if number is True else PR)
    # MUTATION: accept a bool -- True reads as pull request #1.
    assert _submit(fix_client, _continuation(root)).status_code == 201


def test_another_tenants_root_record_is_never_read(fix_client, db):
    root = _root(fix_client, db)
    _live_fix(db, root)
    continuation = Continuation(root_task_id=root, repository_url=REPO_URL)
    # MUTATION: drop the tenant check on the root's record.
    assert cifix._branch_pull_request(db, "research", continuation) is None
    assert cifix._branch_pull_request(db, "eng", continuation) == PR


def test_a_merge_only_continuation_is_admitted(fix_client, db):
    root = _root(fix_client, db)
    _live_fix(db, root)
    response = _submit(fix_client, {
        "strategy": "direct-pr", "continues_task": root,
        "steps": [{"step_id": "merge", "runner_profile": "merge"}],
    })
    # MUTATION: gate merge-only continuations -- a merge pushes nothing, and
    # its own head check waits for the fixer's push.
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("kind", [
    "merge_fix_round", CONFLICT_ROUND_METADATA_KEY, pr_shepherd.METADATA_KEY,
])
def test_a_writers_own_request_keeps_its_own_gate(fix_client, db, kind):
    root = _root(fix_client, db)
    _live_fix(db, root)
    response = _submit(fix_client, _continuation(root, **{kind: {"pull_request": PR}}))
    # MUTATION: gate every continuation -- the merge step's rounds (gated by
    # mergewake.live_claim) and pr-shepherd's fix (live_pr_writer in
    # _plan_one) are refused here a second time, by a code they do not read.
    assert response.status_code == 201, response.text


def test_the_refusal_ships_on_and_off_lets_the_continuation_through(fix_client, db, monkeypatch):
    # MUTATION: register the switch default=False -- the gate is report-only
    # in production, and PR #2473's race happens again.
    assert refusals.enforced("pr_fix_in_flight", environ={})
    root = _root(fix_client, db)
    _live_fix(db, root)
    monkeypatch.setenv(SWITCH, "off")
    response = _submit(fix_client, _continuation(root))
    # MUTATION: raise the Conflict directly, not through `refusals.refuse`.
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# publish_to_pr: the path wf_3bde5fa9 actually took onto PR #2473
# --------------------------------------------------------------------------
#
# Its step's `result_summary.git.publish_to_pr` was {number 2473, branch
# swarm/task_577523f9...}: the worker writes that block only for a spec that
# named `publish_to_pr`. `service` reads that pull request's branch task from
# GitHub after `stamp`, so `stamp` is called directly here, with no forge.


def _publish(number: Any = PR, repository_url: str = REPO_URL, **metadata: Any) -> WorkflowCreate:
    payload: dict[str, Any] = {
        "steps": [{"step_id": "implement", "runner_profile": "mock",
                   "input": {"prompt": "Fix the failing test."}}],
        "strategy": "direct-pr", "repository_url": repository_url, "publish_to_pr": number,
    }
    if metadata:
        payload["metadata"] = metadata
    return WorkflowCreate.model_validate(payload)


def _stamp_publish(db, clock, spec: WorkflowCreate, tenant_id: str = "eng") -> WorkflowCreate:
    return cifix.stamp(Store(db, now=clock), tenant_id, spec, None)


def _live_fix_in(db, repository_url: str = REPO_URL + ".git", **kwargs: Any) -> None:
    _live_fix(db, "task_" + "a1" * 10, **kwargs)
    db.docs[f"tasks/{LIVE_TASK}"]["repository_url"] = repository_url


@pytest.mark.parametrize("kind", [
    cifix.CI_FIX_METADATA_FIELD, pr_shepherd.METADATA_KEY, CONFLICT_ROUND_METADATA_KEY,
])
def test_a_publish_to_pr_step_beside_a_live_fix_is_refused_naming_it(db, clock, kind):
    _live_fix_in(db, kind=kind)
    with pytest.raises(cifix.PrFixInFlight) as refused:
        # MUTATION: gate only `continues_task` -- PR #2473's own path is admitted.
        _stamp_publish(db, clock, _publish())
    detail = refused.value.detail
    # MUTATION: compare repository URLs raw -- `.git` on one side misses the fix.
    assert detail == {"task_id": LIVE_TASK, "workflow_id": LIVE_WORKFLOW, "kind": kind,
                      "pull_request": PR}


def test_a_publish_to_pr_step_beside_a_claim_inside_its_grace_is_refused(db, clock):
    _unwritten_claim(db, "task_" + "a1" * 10, clock)
    with pytest.raises(cifix.PrFixInFlight) as refused:
        # MUTATION: read tasks only -- a fixer still submitting is missed.
        _stamp_publish(db, clock, _publish(f"example-org/widgets#{PR}"))
    assert refused.value.detail["kind"] == "ci_fix"
    clock.now += timedelta(seconds=cifix.CLAIM_GRACE_SECONDS + 1)
    # MUTATION: no grace bound -- a refused fixer's claim holds the PR forever.
    _stamp_publish(db, clock, _publish())


@pytest.mark.parametrize("where", [
    {"state": "SUCCEEDED"}, {"pull_request": PR + 1}, {"tenant_id": "research"},
    {"repository_url": "https://github.com/example-org/gadgets"},
], ids=["terminal", "another_pull_request", "another_tenant", "another_repository"])
def test_a_publish_to_pr_step_beside_no_live_fix_of_its_pull_request_is_admitted(db, clock, where):
    repository = where.pop("repository_url", REPO_URL)
    _live_fix_in(db, repository_url=repository, **where)
    # MUTATION: drop the state, number, tenant or repository check (invariant 9).
    spec = _stamp_publish(db, clock, _publish())
    assert spec.publish_to_pr == PR


def test_a_publish_to_pr_step_that_is_itself_a_fix_keeps_its_own_gate(db, clock):
    _live_fix_in(db)
    # MUTATION: gate a writer's own request -- pr-shepherd's fix is refused twice.
    _stamp_publish(db, clock, _publish(**{pr_shepherd.METADATA_KEY: {"pull_request": PR}}))


def test_the_publish_to_pr_refusal_switched_off_lets_the_step_through(db, clock, monkeypatch):
    _live_fix_in(db)
    monkeypatch.setenv(SWITCH, "off")
    # MUTATION: raise the Conflict directly, not through `refusals.refuse`.
    assert _stamp_publish(db, clock, _publish()).publish_to_pr == PR
