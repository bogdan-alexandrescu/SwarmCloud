"""A plain continuation of a lane is refused while the lane's merge step is live (#2685).

Measured 2026-10-28 on PR #2617: the plain `continues_task` lane
task_7ff30c32 (wf_85f651ab, 'CIFIX-2617') -- no `metadata.ci_fix`, no
writer mark -- was admitted while that lane's merge step task_943ec5f2 was
live. The merge step then submitted its ci-fix:1 round task_99b8e1e0 beside
it; the plain lane pushed 5bc405a first, the round's push was rejected
non-fast-forward (f-046), and the merge step refused `head_moved` at a head
none of its rounds had pushed (f-045). `cifix.stamp` read the merge step
(`_refuse_if_lane_owns`) only for a `metadata.ci_fix` request.

So `stamp` refuses a pushing plain continuation 409 `ci_fix_lane_owns_pr`
(through `refusals.refuse`, the same switch) while the continued lane's
merge step is in any non-terminal state, naming the merge step and its
workflow, with `plain_continuation` in the detail; nothing is written. A
merge-only request, a request carrying a writer's own mark
(`PR_WRITER_KINDS`, `metadata.issue_run`) and the merge step's own CI,
conflict and pick rounds keep their own gates.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import cifix, mergewake, refusals
from swarm_api.continuation import Continuation
from swarm_api.issueci import ci_fix_continuation
from swarm_api.schedtypes import pr_shepherd
from swarm_api.schemas import WorkflowCreate
from swarm_api.store import Store
from swarm_api.validation import CONFLICT_ROUND_METADATA_KEY

from swarm_common.states import TERMINAL_STATES, TaskState

from .conftest import auth_header, seed_task
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    PR,
    REPO_URL,
    _integrator,
    _members_hold_grants,
    _original,
    _red_run_payload,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

MERGE_TASK = "task_" + "e" * 20
SWITCH = refusals.env_name("ci_fix_lane_owns_pr")

LIVE_STATES = sorted(s.value for s in TaskState if s not in TERMINAL_STATES)
FINISHED_STATES = sorted(s.value for s in TERMINAL_STATES)


@pytest.fixture(autouse=True)
def _shipped_position(monkeypatch):
    """The switches as they ship: nothing in the environment."""
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv(refusals.env_name("pr_fix_in_flight"), raising=False)


def _lane(client, db, merge_state: str | None, *, merge_tenant: str = "eng") -> str:
    """A lane: an integrate workflow's integrator, whose `swarm/<id>` pull
    request PR is recorded as the worker records it, and -- unless
    `merge_state` is None -- the workflow's merge step in that state."""
    integrator = _integrator(client)
    db.docs[f"tasks/{integrator}"]["result_summary"] = {"git": {"pull_request": {"number": PR}}}
    if merge_state is not None:
        seed_task(
            db, task_id=MERGE_TASK, tenant_id=merge_tenant, state=merge_state,
            runner_profile="merge", workflow_id=db.docs[f"tasks/{integrator}"]["workflow_id"],
        )
    return integrator


def _continuation(root: str, **metadata: Any) -> dict[str, Any]:
    """A plain continuation of `root`, as wf_85f651ab ('CIFIX-2617') was."""
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


def _claims(db) -> list[str]:
    return [key for key in db.docs if key.startswith(f"{cifix.CLAIMS}/")]


def _stamp(db, clock, root: str, spec) -> Any:
    """`cifix.stamp` as `service` calls it for a continuation of the lane's integrator."""
    doc = db.docs[f"tasks/{root}"]
    continuation = Continuation(root_task_id=root, repository_url=REPO_URL, task_id=root,
                                workflow_id=doc["workflow_id"])
    return cifix.stamp(Store(db, now=clock), "eng", spec, continuation)


def test_the_live_states_are_the_ones_the_brief_names():
    """Rule zero: the parametrisation below visits every live state."""
    assert {"QUEUED", "PARKED", "READY", "LEASED", "DISPATCHED", "STARTING", "RUNNING"} <= set(LIVE_STATES)
    assert not set(LIVE_STATES) & set(FINISHED_STATES)


@pytest.mark.parametrize("merge_state", LIVE_STATES)
def test_a_plain_continuation_onto_a_lane_with_a_parked_merge_step_is_refused(
    fix_client, db, merge_state
):
    """PARKED is the merge step on its CI wait, where task_943ec5f2 was; every
    other non-terminal state is the one whose round starts next."""
    integrator = _lane(fix_client, db, merge_state)
    workflow = db.docs[f"tasks/{integrator}"]["workflow_id"]
    before = _tasks(db)

    response = _submit(fix_client, _continuation(integrator))

    # MUTATION: read the merge step only for metadata.ci_fix -- 201, and the
    # merge step's round races the plain lane for the branch, as on PR #2617.
    assert response.status_code == 409, response.text
    body = response.json()
    # MUTATION: answer pr_fix_in_flight -- that code says a fix is live; the
    # merge step is the pull request's writer here.
    assert body["code"] == "ci_fix_lane_owns_pr"
    # MUTATION: leave the merge task or its workflow out of the detail.
    assert body["detail"]["task_id"] == MERGE_TASK
    assert body["detail"]["workflow_id"] == workflow
    assert body["detail"]["root_task_id"] == integrator
    assert body["detail"]["pull_request"] == PR
    assert body["detail"]["plain_continuation"] is True
    # MUTATION: stamp the ci_fix metadata field into the detail -- this request has none.
    assert "metadata" not in body["detail"]
    assert MERGE_TASK in body["message"]
    # MUTATION: refuse after the submission writes -- a refused request leaves a task behind.
    assert _tasks(db) == before
    assert _claims(db) == []


@pytest.mark.parametrize("case", [
    *(f"finished:{state}" for state in FINISHED_STATES),
    "no_merge_step", "single_task", "another_tenant",
])
def test_a_finished_or_absent_or_other_tenants_merge_step_admits_it(fix_client, db, case):
    if case.startswith("finished:"):
        root = _lane(fix_client, db, case.split(":", 1)[1])
    elif case == "no_merge_step":
        root = _lane(fix_client, db, None)
    elif case == "another_tenant":
        root = _lane(fix_client, db, "PARKED", merge_tenant="research")
    else:
        root = _original(fix_client)
    response = _submit(fix_client, _continuation(root))
    # MUTATION: refuse whenever the workflow has a merge step, refuse a task in
    # no workflow, or read the workflow's tasks without the tenant filter
    # (invariant 9).
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("kind", [
    "merge_only", cifix.CI_FIX_METADATA_FIELD, "merge_fix_round", CONFLICT_ROUND_METADATA_KEY,
    pr_shepherd.METADATA_KEY, cifix.ISSUE_RUN_METADATA_KEY,
])
def test_merge_only_and_writer_marked_requests_keep_their_own_gate(db, clock, fix_client, kind):
    """A merge-only request pushes nothing. Each writer has its own gate
    (ci_fix: `_refuse_if_lane_owns`; the merge step's rounds: mergewake's
    claim; pr-shepherd: `live_pr_writer`; an issue run: issueci), so the new
    read is not a second one -- for ci_fix the merge step's refusal is the
    existing one, with the ci_fix detail scripts/ci-fix.sh reads."""
    integrator = _lane(fix_client, db, "PARKED")
    if kind == "merge_only":
        payload = {"strategy": "direct-pr", "continues_task": integrator,
                   "steps": [{"step_id": "merge", "runner_profile": "merge"}]}
    elif kind == cifix.CI_FIX_METADATA_FIELD:
        payload = _red_run_payload(integrator)
    else:
        payload = _continuation(integrator, **{kind: {"pull_request": PR}})
    spec = WorkflowCreate.model_validate(payload)
    if kind == cifix.CI_FIX_METADATA_FIELD:
        with pytest.raises(cifix.CiFixLaneOwnsPr) as refused:
            _stamp(db, clock, integrator, spec)
        # MUTATION: run the new check ahead of the ci_fix path -- its detail
        # loses `metadata: ci_fix`.
        assert refused.value.detail["metadata"] == cifix.CI_FIX_METADATA_FIELD
        assert "plain_continuation" not in refused.value.detail
        return
    # MUTATION: gate a merge-only request or a writer's own request here --
    # refused by a code it does not read.
    _stamp(db, clock, integrator, spec)


def test_the_merge_steps_own_ci_conflict_and_pick_rounds_are_not_refused(db, clock, fix_client):
    """The rounds as mergewake builds them: `merge_fix_round` for a CI round and
    (with `kind: "pick"`) a pick round, `merge_conflict_round` for a conflict round."""
    integrator = _lane(fix_client, db, "PARKED")
    record = {"merge_task": MERGE_TASK, "round": 1, "head_sha": "a" * 40, "pull_request": PR}
    url = f"{REPO_URL}/pull/{PR}"
    ci_round = ci_fix_continuation(
        continues_task=integrator, repository_url=REPO_URL, lead="CI is red.", round_no=1,
        rounds=2, excerpt="", step_input={}, metadata={"merge_fix_round": record},
    )
    conflict_round = mergewake.conflict_fix_continuation(
        continues_task=integrator, repository_url=REPO_URL, number=PR, url=url, base="main",
        round_no=1, rounds=2, metadata={CONFLICT_ROUND_METADATA_KEY: record},
    )
    pick_round = mergewake.pick_fix_continuation(
        continues_task=integrator, repository_url=REPO_URL, number=PR, url=url, round_no=1,
        rounds=2, picks=[f"pick {'task_' + 'b' * 20} question 1 build it"],
        metadata={"merge_fix_round": {
            "kind": mergewake.ROUND_KIND[mergewake.PICK_ROUNDS_KEY], **record}},
    )
    # #2858: a pick the branch already builds is reported built with nothing
    # pushed, which the merge counts; the brief says so.
    prompt = pick_round.steps[0].input["prompt"]
    assert "ALREADY builds a pick" in prompt and "pushing nothing then is correct" in prompt
    # The pick round's mark, pinned: a writer kind, so the new read leaves it alone.
    assert "merge_fix_round" in pick_round.metadata
    assert pick_round.metadata["merge_fix_round"]["kind"] == "pick"
    for spec in (ci_round, conflict_round, pick_round):
        # MUTATION: gate every continuation of a lane task -- the merge
        # step's own rounds are refused and its pull request stays red.
        stamped = _stamp(db, clock, integrator, spec)
        assert cifix.FIX_HEAD_FIELD in stamped.metadata


def test_the_switch_off_lets_the_continuation_through(fix_client, db, monkeypatch):
    # MUTATION: register the switch default=False -- the gate is report-only.
    assert refusals.enforced("ci_fix_lane_owns_pr", environ={})
    integrator = _lane(fix_client, db, "PARKED")
    monkeypatch.setenv(SWITCH, "off")
    response = _submit(fix_client, _continuation(integrator))
    # MUTATION: raise CiFixLaneOwnsPr directly, not through `refusals.refuse`.
    assert response.status_code == 201, response.text
