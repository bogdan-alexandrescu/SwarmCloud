"""A re-review whose agent will not run is readied on claude-code, not the browser.

BGD, 2026-11-05: tasks that start on the browser runtime and barely use it.
In an implement -> review -> fix -> re-review -> merge lane, on MERGE no fix
runs and the re-review runs NO agent: the worker re-publishes the staged
verdict byte for byte (`agent_worker.lifecycle.Worker._republish_staged_verdict`).
Yet a re-review of a `browser_if_ui` review inherited the review's resolved
profile, so a UI lane's MERGE started a browser pod for a copy: 56 re-reviews
on claude-code-browser in the day to 10:00Z on 5 November, 53 of them with no
browser use.

The gate that decides it is the worker's: the re-review's
`metadata.dispatch.verdict_gate` is the integrator's own (`rereview_step_for`),
and the integrator -- which the re-review depends on and builds on -- has
already recorded what it read from the same review's verdict file
(`result_summary.verdict_gate`, the worker's or swarm-api's control-plane
publish). A verdict outside the gate's `verdict_in` shuts the re-review's gate
too. What is held here:

    the republish re-review resolves to claude-code, swapped with its signed
        spec, recorded on the task and in the READY event, and admitted on
        claude-code's pools only (invariants 1 to 3)
    a control-plane-published integrator's MERGE counts the same
    a re-review after a fix keeps the browser when the lane is UI
    a NOT_YET the integrator's no-op precheck skipped keeps today's inherit
    an integrator with no record, another review's record, an unread verdict
        or a malformed gate keeps today's inherit
    a pinned review's re-review (Q-2275) is unchanged
    the restated verdict names are the worker's
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import verdict as verdict_mod
from scheduler import runtime as runtime_mod

from .conftest import seed_pool, seed_task, seed_tenant

BROWSER = "claude-code-browser"
CODE = "claude-code"
POLICY = {"policy": "browser_if_ui", "browser": BROWSER, "code": CODE}
UI_FILE = "apps/swarm-ui/src/pages/Agents.tsx"
GATE = {"task_id": "task_review", "verdict_in": ["NOT_YET"]}


def _alternate() -> dict[str, Any]:
    return {
        "runner_profile": CODE, "resource_class": "standard", "timeout_seconds": 3600,
        "provider": "anthropic", "model": None, "model_class": None,
        "spec_signature": "c2lnbmVkLWNvZGU=", "spec_key_version": "kv/1", "spec_format": 1,
    }


def _record(verdict: str, *, agent_ran: bool, task_id: str = "task_review",
            **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "file": "review.json", "verdict": verdict,
            "verdict_in": ["NOT_YET"], "agent_ran": agent_ran, "findings": [], **extra}


def _lane(db, *, review_profile: str, integrator_summary: dict[str, Any] | None,
          inherit: bool = True, gate: Any = GATE) -> dict[str, Any]:
    """The review SUCCEEDED on `review_profile`, the integrator SUCCEEDED with
    `integrator_summary`, and the re-review PARKED on both, stored on the browser."""
    seed_tenant(db, "eng", max_active=10, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=10)
    review = seed_task(db, task_id="task_review", tenant_id="eng", state="SUCCEEDED",
                       runner_profile=review_profile)
    review["result_summary"] = {"git": {"files": [], "files_truncated": False}}
    integrator = seed_task(db, task_id="task_integrator", tenant_id="eng", state="SUCCEEDED",
                           runner_profile=CODE)
    integrator["result_summary"] = {
        "git": {"files": [{"path": UI_FILE, "status": "M"}], "files_truncated": False},
        **(integrator_summary or {}),
    }
    rereview = seed_task(db, task_id="task_rereview", tenant_id="eng", state="PARKED",
                         park_reason="DEPENDENCY_INCOMPLETE", runner_profile=BROWSER,
                         resource_class="browser", provider="anthropic",
                         depends_on=("task_integrator", "task_review"))
    rereview["spec_signature"] = "c2lnbmVkLWJyb3dzZXI="
    rereview["spec_key_version"] = "kv/1"
    rereview["spec_format"] = 1
    block: dict[str, Any] = {**POLICY, "alternate": _alternate()}
    if inherit:
        block["inherit"] = {"step": "review", "task_id": "task_review"}
    dispatch: dict[str, Any] = {"builds_on": "task_integrator"}
    if gate is not None:
        dispatch["verdict_gate"] = gate
    rereview["metadata"] = {"runtime": block, "dispatch": dispatch}
    return rereview


def _ready_event(db, task_id: str) -> dict[str, Any]:
    prefix = f"tasks/{task_id}/events/"
    (event,) = [doc for key, doc in db.docs.items()
                if key.startswith(prefix) and doc["type"] == "ready"]
    return event


def test_the_republish_re_review_resolves_to_claude_code(db, make_scheduler, dispatcher):
    _lane(db, review_profile=BROWSER,
          integrator_summary={"verdict_gate": _record("MERGE", agent_ran=False),
                              "skipped_agent": "review verdict MERGE"})

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    # MUTATION: inherit the review's browser and this is claude-code-browser.
    assert doc["runner_profile"] == CODE
    assert doc["resource_class"] == "standard"
    assert doc["spec_signature"] == "c2lnbmVkLWNvZGU="
    resolved = doc["metadata"]["runtime"]["resolved"]
    assert resolved["reason"] == "republish_no_agent"
    assert resolved["swapped"] is True
    assert resolved["gate"] == {"task_id": "task_review", "verdict": "MERGE",
                                "verdict_in": ["NOT_YET"], "read_from": "task_integrator"}
    event = _ready_event(db, "task_rereview")
    assert event["detail"]["runtime"]["profile"] == CODE
    assert event["detail"]["runtime"]["reason"] == "republish_no_agent"
    # Invariants 2 and 3: resolved in the promotion's write, so the one
    # admission reserves claude-code's pools and never the browser's.
    leases = [d for k, d in db.docs.items() if k.startswith("leases/")]
    assert len(leases) == 1 and len(dispatcher.dispatched) == 1, doc.get("park_reason")
    assert "runner:claude-code" in leases[0]["pools"]
    assert "runner:claude-code-browser" not in leases[0]["pools"]
    assert "resource:browser" not in leases[0]["pools"]
    assert [record["profile"] for record in dispatcher.dispatched] == [CODE]


def test_a_control_plane_published_merge_counts_the_same(db, make_scheduler):
    _lane(db, review_profile=BROWSER,
          integrator_summary={"published_by": "control_plane",
                              "verdict_gate": _record("merge", agent_ran=False)})

    make_scheduler().release_dependants("task_integrator")

    assert db.docs["tasks/task_rereview"]["runner_profile"] == CODE


def test_a_re_review_after_a_fix_keeps_the_browser_when_the_lane_is_ui(db, make_scheduler):
    _lane(db, review_profile=BROWSER,
          integrator_summary={"verdict_gate": _record("NOT_YET", agent_ran=True)})

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    assert doc["runner_profile"] == BROWSER
    assert doc["metadata"]["runtime"]["resolved"]["reason"] == "inherited"


def test_a_not_yet_the_integrators_precheck_skipped_keeps_the_inherit(db, make_scheduler):
    """The re-review reads NOT_YET too, so its own gate is open: whether its
    no-op precheck then skips the agent needs its clone, and only the worker has it."""
    _lane(db, review_profile=BROWSER, integrator_summary={
        "verdict_gate": _record("NOT_YET", agent_ran=False,
                                noop_precheck={"reason": "the head holds a MERGEd commit"})})

    make_scheduler().release_dependants("task_integrator")

    assert db.docs["tasks/task_rereview"]["runner_profile"] == BROWSER


@pytest.mark.parametrize("summary, gate", [
    ({}, GATE),
    ({"verdict_gate": "MERGE"}, GATE),
    ({"verdict_gate": _record("MERGE", agent_ran=False, task_id="task_other")}, GATE),
    ({"verdict_gate": _record("LGTM", agent_ran=False)}, GATE),
    ({"verdict_gate": {"task_id": "task_review", "agent_ran": False}}, GATE),
    ({"verdict_gate": _record("MERGE", agent_ran=False)}, None),
    ({"verdict_gate": _record("MERGE", agent_ran=False)},
     {"task_id": "task_review", "verdict_in": ["NOT_YET", "SHIP"]}),
    ({"verdict_gate": _record("MERGE", agent_ran=False)},
     {"task_id": "task_review", "verdict_in": []}),
    ({"verdict_gate": _record("MERGE", agent_ran=False)},
     {"task_id": "task_elsewhere", "verdict_in": ["NOT_YET"]}),
], ids=["no-record", "not-an-object", "another-review", "unread-verdict", "no-verdict",
        "no-gate", "gate-names-an-unknown-verdict", "gate-names-none", "gate-not-the-review"])
def test_anything_short_of_a_shut_gate_keeps_todays_inherit(db, make_scheduler, summary, gate):
    _lane(db, review_profile=BROWSER, integrator_summary=summary, gate=gate)

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    assert doc["runner_profile"] == BROWSER
    assert doc["metadata"]["runtime"]["resolved"]["reason"] == "inherited"


def test_an_integrator_outside_depends_on_is_not_read():
    seen: list[str] = []

    def read(task_id: str) -> dict[str, Any]:
        seen.append(task_id)
        if task_id == "task_review":
            return {"runner_profile": BROWSER}
        return {"state": "SUCCEEDED",
                "result_summary": {"verdict_gate": _record("MERGE", agent_ran=False)}}

    resolution = runtime_mod.resolve(
        ["task_review"], {**POLICY, "inherit": {"task_id": "task_review"}}, read,
        dispatch={"builds_on": "task_integrator", "verdict_gate": GATE})
    assert resolution.profile == BROWSER and resolution.reason == "inherited"
    assert "task_integrator" not in seen


def test_an_integrator_that_did_not_succeed_is_not_believed():
    def read(task_id: str) -> dict[str, Any]:
        if task_id == "task_review":
            return {"runner_profile": BROWSER}
        return {"state": "FAILED",
                "result_summary": {"verdict_gate": _record("MERGE", agent_ran=False)}}

    resolution = runtime_mod.resolve(
        ["task_integrator", "task_review"], {**POLICY, "inherit": {"task_id": "task_review"}},
        read, dispatch={"builds_on": "task_integrator", "verdict_gate": GATE})
    assert resolution.profile == BROWSER


def test_a_pinned_reviews_re_review_is_unchanged(db, make_scheduler):
    """Q-2275: a review pinned to claude-code gives its re-review a block with
    no `inherit`, decided from the lane's own files, MERGE or not."""
    _lane(db, review_profile=CODE, inherit=False,
          integrator_summary={"verdict_gate": _record("MERGE", agent_ran=False)})

    make_scheduler().release_dependants("task_integrator")

    doc = db.docs["tasks/task_rereview"]
    assert doc["runner_profile"] == BROWSER
    assert doc["metadata"]["runtime"]["resolved"]["reason"] == "ui_paths"


def test_the_restated_verdicts_are_the_workers():
    assert runtime_mod.REVIEW_VERDICTS == verdict_mod.REVIEW_VERDICTS
