"""Work contracts on swarm-api's submission doors (part of #2688).

The owner chose on 3 November 2026 to wire work contracts into submission
before the work contract change merges. Held here, red first:

  * with SWARM_WORK_CONTRACTS on, a task and a workflow step that carry
    `metadata.work_contract` store its canonical document and carry its
    signed reference at `metadata.dispatch.work_contract`;
  * a strict contract the worker cannot enforce is refused at submission,
    creating nothing; so is a contract of another tenant, one whose limits
    or repository disagree with the step's, and one on a whole workflow;
  * a contract's limits fill the step fields the step leaves unset, and
    the existing validation resolves them;
  * with the switch off a contract is ignored, as `workcontract.attach`
    documents: the key is the caller's metadata, exactly as today;
  * an issue run's approved plan steps each compile their contract.

Offline: FakeFirestore, a local spec signer. No credentials, no emulator.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import workcontract as wc
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.issueruns import compile_plan
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_tenant
from .spec_signer import LocalSpecSigner
from .test_review_gate_default import PLAN, _run

CODE = "claude-code"
REPO = "https://github.com/acme/widgets.git"


@pytest.fixture
def client(db, tokens, group_map) -> TestClient:
    seed_tenant(db, "eng")
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=LocalSpecSigner(),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")


def _contract(**over: Any) -> dict[str, Any]:
    doc = {
        "schema": 1,
        "contract_id": "wc-sort-widgets",
        "tenant_id": "eng",
        "revision": 1,
        "strictness": "advisory",
        "requirements": [{"id": "REQ-1", "text": "Widgets sort by name.",
                          "verification": ["unit_tests"]}],
        "scope": {"write": ["src/widgets/**"]},
        "limits": {"runner_profile": CODE},
        "artifacts": [{"kind": "pull_request"}],
        "ownership": {"owner": "alice@example.com"},
    }
    doc.update(over)
    return doc


def _strict_enforceable(**over: Any) -> dict[str, Any]:
    doc = _contract(strictness="strict", scope={}, artifacts=[])
    doc["requirements"][0]["verification"] = []
    doc.update(over)
    return doc


def _tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def _task(client, contract: dict[str, Any] | None, **extra: Any):
    body: dict[str, Any] = {"runner_profile": CODE, "input": {"prompt": "hello"}, **extra}
    if contract is not None:
        body["metadata"] = {**body.get("metadata", {}), wc.METADATA_KEY: contract}
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


def _workflow(client, steps: list[dict[str, Any]], **extra: Any):
    body = {"on_step_failure": "continue", "strategy": "direct-pr",
            "repository_url": REPO, "steps": steps, **extra}
    return client.post("/v1/workflows", headers=auth_header("alice"), json=body)


def _step(step_id: str, contract: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": step_id, "runner_profile": CODE,
                            "input": {"prompt": f"do {step_id}"}, **extra}
    if contract is not None:
        step["metadata"] = {wc.METADATA_KEY: contract}
    return step


def _assert_signed_reference(task: dict[str, Any], contract: dict[str, Any]) -> None:
    stored = wc.decode(task["metadata"][wc.METADATA_KEY])
    assert stored == wc.decode(contract)
    reference = task["metadata"]["dispatch"][wc.DISPATCH_FIELD]
    assert reference == {"id": "wc-sort-widgets", "revision": 1, "digest": wc.digest(stored),
                         "strictness": stored.strictness, "requirements": ["REQ-1"]}
    assert wc.verify_reference(stored, reference)
    # Inside the signed spec: the task was signed after the reference was written.
    assert task["spec_signature"]


# --- a task -----------------------------------------------------------------


def test_a_task_with_a_contract_carries_its_signed_reference(client, db, switch_on):
    response = _task(client, _contract())
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    _assert_signed_reference(task, _contract())


def test_a_task_without_a_contract_is_unchanged_with_the_switch_on(client, db, switch_on):
    assert _task(client, None).status_code == 201
    (task,) = _tasks(db)
    assert wc.DISPATCH_FIELD not in task["metadata"]["dispatch"]
    assert wc.METADATA_KEY not in task["metadata"]


def test_with_the_switch_off_a_contract_is_ignored(client, db, monkeypatch):
    monkeypatch.delenv(wc.SWITCH_ENV, raising=False)
    # Even one that would be refused: off, nothing reads it.
    sent = _contract(strictness="strict")
    assert _task(client, sent).status_code == 201
    (task,) = _tasks(db)
    assert wc.DISPATCH_FIELD not in task["metadata"]["dispatch"]
    assert task["metadata"][wc.METADATA_KEY] == sent


def test_a_strict_contract_the_worker_cannot_enforce_is_refused(client, db, switch_on):
    response = _task(client, _contract(strictness="strict"))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["work_contract"] == "work_contract_not_enforceable"
    assert detail["unenforced"] == ["artifacts", "verification", "write_scope"]
    assert _tasks(db) == []


def test_a_strict_contract_of_enforced_components_is_accepted(client, db, switch_on):
    contract = _strict_enforceable()
    assert _task(client, contract).status_code == 201
    (task,) = _tasks(db)
    assert task["metadata"]["dispatch"][wc.DISPATCH_FIELD]["strictness"] == "strict"


def test_a_malformed_contract_is_refused(client, db, switch_on):
    response = _task(client, _contract(requirements=[]))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["work_contract"] == "work_contract_invalid"
    assert _tasks(db) == []


def test_a_contract_of_another_tenant_is_refused(client, db, switch_on):
    response = _task(client, _contract(tenant_id="research"))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["work_contract"] == "work_contract_invalid"
    assert _tasks(db) == []


def test_the_contracts_limits_fill_the_fields_the_step_leaves_unset(client, db, switch_on):
    contract = _contract(limits={"runner_profile": CODE, "max_turns": 30,
                                 "timeout_seconds": 1200})
    assert _task(client, contract).status_code == 201, _tasks(db)
    (task,) = _tasks(db)
    assert task["max_turns"] == 30
    assert task["timeout_seconds"] == 1200


@pytest.mark.parametrize("limits, extra", [
    ({"runner_profile": CODE, "max_turns": 30}, {"max_turns": 40}),
    ({"runner_profile": "claude-code-browser"}, {}),
    ({"runner_profile": CODE, "model_class": "sonnet"}, {"model_class": "opus"}),
])
def test_a_contract_that_disagrees_with_the_step_is_refused(client, db, switch_on,
                                                            limits, extra):
    response = _task(client, _contract(limits=limits), **extra)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["work_contract"] == "work_contract_mismatch"
    assert _tasks(db) == []


# --- a workflow step ---------------------------------------------------------


def test_a_workflow_step_with_a_contract_carries_its_signed_reference(client, db, switch_on):
    contract = _contract(repositories=[{"repository_url": "https://github.com/acme/widgets"}])
    response = _workflow(client, [_step("a", contract), _step("b", depends_on=["a"])])
    assert response.status_code == 201, response.text
    by_step = {t["step_id"]: t for t in _tasks(db)}
    _assert_signed_reference(by_step["a"], contract)
    assert wc.DISPATCH_FIELD not in by_step["b"]["metadata"]["dispatch"]


def test_a_workflow_step_contract_on_another_repository_is_refused(client, db, switch_on):
    contract = _contract(repositories=[{"repository_url": "https://github.com/acme/other"}])
    response = _workflow(client, [_step("a", contract)])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["work_contract"] == "work_contract_mismatch"
    assert _tasks(db) == []


def test_a_strict_workflow_step_the_worker_cannot_enforce_is_refused(client, db, switch_on):
    response = _workflow(client, [_step("a", _contract(strictness="strict"))])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["work_contract"] == "work_contract_not_enforceable"
    assert _tasks(db) == []


def test_a_contract_on_a_whole_workflow_is_refused(client, db, switch_on):
    response = _workflow(client, [_step("a"), _step("b")],
                         metadata={wc.METADATA_KEY: _contract()})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == f"metadata.{wc.METADATA_KEY}"
    assert _tasks(db) == []


# --- an issue run ---------------------------------------------------------------


STAGED = {
    "summary": "Sort and filter widgets.",
    "requirements": ["Widgets sort by name", "Widgets filter by tag"],
    "steps": [
        {"step_id": "sort", "title": "Sort", "prompt": "Sort.", "files": ["src/sort.py"],
         "tests": ["uv run pytest tests/test_sort.py"], "depends_on": []},
        {"step_id": "filter", "title": "Filter", "prompt": "Filter.", "depends_on": ["sort"]},
    ],
}


@pytest.mark.parametrize("plan, impl", [(PLAN, ["impl-sort"]),
                                        (STAGED, ["impl-sort", "impl-filter"])])
def test_each_approved_plan_step_compiles_its_contract(switch_on, plan, impl):
    run = _run(copy.deepcopy(plan))
    workflow = compile_plan(run)
    by_id = {s.step_id: s for s in workflow.steps}
    for step_id in impl:
        document = by_id[step_id].metadata[wc.METADATA_KEY]
        contract = wc.decode(document)
        assert contract.contract_id == f"wc-run-x-{step_id[len('impl-'):]}"
        assert contract.tenant_id == "eng" and contract.strictness == "advisory"
        assert contract.requirement_ids() == ("REQ-1", "REQ-2")
        assert contract.limits["runner_profile"] == by_id[step_id].runner_profile
        assert contract.repositories[0]["repository_url"] == run.issue.repository_url
        # It compiles: the submission path will accept it.
        assert wc.compile_contract(contract, step_id=step_id).advisory
    for other in set(by_id) - set(impl):
        assert wc.METADATA_KEY not in by_id[other].metadata


def test_with_the_switch_off_an_issue_run_compiles_no_contract(monkeypatch):
    monkeypatch.delenv(wc.SWITCH_ENV, raising=False)
    workflow = compile_plan(_run(copy.deepcopy(STAGED)))
    assert all(wc.METADATA_KEY not in s.metadata for s in workflow.steps)
