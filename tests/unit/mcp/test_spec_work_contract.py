"""A workflow spec's step can carry a work contract (`work_contract`).

The owner's choice of 10 November 2026: swarm-api takes a step's
`metadata.work_contract` on POST /v1/workflows (`workcontract.prepare`), but
`build_steps` refused every step key outside `_STEP_KEYS`, so a spec could not
submit a step that contract review runs on. These pin that `work_contract` is
sent as the step's `metadata.work_contract`, that a malformed one is refused
before anything is submitted -- naming the step and the field -- and that
`metadata` itself is still refused (invariant 10).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from swarm_mcp import workflows
from swarm_mcp.client import SwarmError


def _contract(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": 1,
        "contract_id": "wc-sort-widgets",
        "tenant_id": "eng",
        "revision": 1,
        "strictness": "advisory",
        "requirements": [{"id": "REQ-1", "text": "Widgets sort by name.",
                          "verification": ["unit_tests"]}],
        "scope": {"write": ["src/widgets/**"]},
        "limits": {"runner_profile": "claude-code"},
        "artifacts": [{"kind": "pull_request"}],
        "ownership": {"owner": "owner@example.com"},
    }
    doc.update(over)
    return doc


def _steps(contract: Any) -> list[dict[str, Any]]:
    return [
        {"step_id": "build", "prompt": "sort the widgets", "runner_profile": "claude-code",
         "work_contract": contract},
        {"step_id": "review", "prompt": "review it", "depends_on": ["build"]},
    ]


def _built(contract: Any) -> dict[str, dict[str, Any]]:
    return {s["step_id"]: s for s in workflows.build_steps(_steps(contract))}


def test_a_valid_contract_reaches_the_payload_as_metadata_work_contract():
    contract = _contract()
    build = _built(contract)["build"]
    assert build["metadata"] == {"work_contract": contract}
    assert "work_contract" not in build
    assert build["runner_profile"] == "claude-code"


def test_the_contract_is_sent_through_submit_as_the_steps_metadata():
    sent: list[dict[str, Any]] = []

    class Client:
        def request(self, method, path, payload=None):
            sent.append(copy.deepcopy(payload))
            return {"workflow": {"workflow_id": "wf_1", "steps": []}}

    contract = _contract()
    spec = workflows.read_spec({"steps": _steps(contract), "strategy": "direct-pr",
                                "repository_url": "https://github.com/example/widgets"})
    workflows.submit(Client(), **spec)
    assert len(sent) == 1
    by_id = {s["step_id"]: s for s in sent[0]["steps"]}
    assert by_id["build"]["metadata"]["work_contract"] == contract
    assert "work_contract" not in (by_id["review"].get("metadata") or {})


def test_the_contract_sits_beside_a_review_gate_in_one_metadata():
    steps = _steps(_contract())
    steps[0]["review_gate"] = False
    build = {s["step_id"]: s for s in workflows.build_steps(steps)}["build"]
    assert build["metadata"] == {"review_gate": False, "work_contract": _contract()}


def test_a_step_without_a_contract_sends_none():
    assert "metadata" not in _built(_contract())["review"]


def test_the_sent_contract_is_a_copy_of_the_specs():
    contract = _contract()
    build = _built(contract)["build"]
    build["metadata"]["work_contract"]["scope"]["write"].append("elsewhere/**")
    assert contract["scope"]["write"] == ["src/widgets/**"]


# Each is (contract, the field the refusal names).
BAD_CONTRACTS = [
    (None, "work_contract"),
    ("wc-sort-widgets", "work_contract"),
    ([], "work_contract"),
    (_contract(schema=2), "work_contract.schema"),
    (_contract(schema=True), "work_contract.schema"),
    ({k: v for k, v in _contract().items() if k != "schema"}, "work_contract.schema"),
    (_contract(image="busybox"), "work_contract"),
    (_contract(scope={"write": ["src/**"], "exec": ["sh"]}), "work_contract.scope"),
    (_contract(scope=["src/**"]), "work_contract.scope"),
    (_contract(scope={"write": "src/**"}), "work_contract.scope.write"),
    (_contract(protected={"files": ["x"]}), "work_contract.protected"),
    (_contract(capabilities={"runtime": ["screenshots"], "command": "x"}),
     "work_contract.capabilities"),
    (_contract(capabilities={"plugins": ["gstack"]}), "work_contract.capabilities.plugins"),
    (_contract(limits={"runner_profile": "claude-code", "image": "x"}), "work_contract.limits"),
    (_contract(limits="claude-code"), "work_contract.limits"),
    (_contract(ownership={"owner": "owner@example.com", "group": "x"}),
     "work_contract.ownership"),
    (_contract(requirements={"id": "REQ-1"}), "work_contract.requirements"),
    (_contract(requirements=["REQ-1"]), "work_contract.requirements[0]"),
    (_contract(requirements=[{"id": "REQ-1", "text": "x", "verification": [], "how": "sh"}]),
     "work_contract.requirements[0]"),
    (_contract(artifacts={"kind": "file"}), "work_contract.artifacts"),
    (_contract(repositories="https://github.com/example/widgets"),
     "work_contract.repositories"),
]


@pytest.mark.parametrize("contract,field", BAD_CONTRACTS)
def test_a_malformed_contract_is_refused_before_submission_naming_step_and_field(contract, field):
    # `build_steps` builds the body `submit` sends; refusing here sends nothing.
    with pytest.raises(SwarmError) as refused:
        workflows.build_steps(_steps(contract))
    message = str(refused.value)
    assert "step 'build'" in message
    assert f"`{field}`" in message
    assert "Nothing was sent" in message


def test_a_malformed_contract_is_refused_by_read_spec_before_submit():
    sent: list[Any] = []

    class Client:
        def request(self, method, path, payload=None):
            sent.append(payload)
            return {"workflow": {}}

    with pytest.raises(SwarmError, match="work_contract"):
        spec = workflows.read_spec({"steps": _steps(_contract(schema=2))})
        workflows.submit(Client(), **spec)
    assert sent == []


def test_metadata_itself_is_still_refused_even_holding_a_contract():
    step = {"step_id": "build", "prompt": "sort the widgets",
            "metadata": {"work_contract": _contract()}}
    with pytest.raises(SwarmError) as refused:
        workflows.build_steps([step])
    message = str(refused.value)
    assert "carries ['metadata'], which this tool does not send" in message
    assert "'work_contract'" in message.split("Accepted:", 1)[1]


GOOD_CONTRACTS = [
    _contract(),
    _contract(acceptance="auto"),
    _contract(strictness="strict", scope={}, artifacts=[],
              requirements=[{"id": "REQ-1", "text": "Widgets sort by name.",
                             "verification": []}]),
    _contract(repositories=[{"repository_url": "https://github.com/example/widgets",
                             "ref": "main"}],
              protected={"paths": ["docs/**"], "interfaces": ["the widget API"]},
              capabilities={"runtime": ["screenshots"]}),
    _contract(revision=2, previous_digest="0" * 64),
]


@pytest.mark.parametrize("contract", GOOD_CONTRACTS)
def test_every_contract_swarm_api_decodes_the_plugin_sends(contract):
    from swarm_api import workcontract

    workcontract.decode(contract)
    assert _built(contract)["build"]["metadata"]["work_contract"] == contract


@pytest.mark.parametrize("contract,field", BAD_CONTRACTS)
def test_every_contract_the_plugin_refuses_swarm_api_refuses(contract, field):
    from swarm_api import workcontract

    with pytest.raises(workcontract.ContractInvalid):
        workcontract.decode(contract)


def test_the_closed_key_sets_are_the_frozen_projections():
    """The shape is read off `swarm_common.workcontract`, never restated: the
    only key it adds is swarm-api's `acceptance`, which its subclass carries."""
    from swarm_api import workcontract

    assert workflows.WORK_CONTRACT_KEYS == workcontract._TOP_KEYS
