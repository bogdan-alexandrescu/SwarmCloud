"""The workflow switches in the signed dispatch block (the owner's experiment, 2026-11-01).

A spec may carry `dispatch: {code_intelligence, quality_gates, jev_judgments}`,
each `on` or `off`, which every step obeys. swarm-api accepts only the two
words, refuses anything else with the accepted values named (the
`_accepted_value` pattern, code `invalid_dispatch`), and writes into
`metadata.dispatch` exactly the keys the spec set: a spec without the map
stores exactly the block it stored before. The block is one of the keys the
spec signature covers, so the worker reads the switches only once it verifies.

The worker halves: tests/unit/worker/test_dispatch_switches_worker.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_common.specsign import SIGNED_METADATA_KEYS

from swarm_api.validation import (
    DISPATCH_METADATA_KEY,
    DISPATCH_SWITCHES,
    DISPATCH_SWITCH_VALUES,
    DispatchOptionError,
    resolve_dispatch_options,
)

from .conftest import auth_header

SWITCHES = ("code_intelligence", "quality_gates", "jev_judgments")


def _workflow(client: Any, **top: Any) -> Any:
    spec = {"steps": [{"step_id": "a", "runner_profile": "mock"},
                      {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]}]}
    spec.update(top)
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _blocks(db: Any, response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 201, response.text
    ids = [s["task_id"] for s in response.json()["workflow"]["steps"]]
    return [db.docs[f"tasks/{task_id}"]["metadata"]["dispatch"] for task_id in ids]


def test_the_switches_are_the_three_the_owner_named():
    assert DISPATCH_SWITCHES == SWITCHES
    assert DISPATCH_SWITCH_VALUES == ("on", "off")
    assert DISPATCH_METADATA_KEY in SIGNED_METADATA_KEYS


@pytest.mark.parametrize("name", SWITCHES)
@pytest.mark.parametrize("value", ["on", "off"])
def test_each_switch_value_is_stored_exactly_on_every_step(client, db, name, value):
    baseline = _blocks(db, _workflow(client))
    blocks = _blocks(db, _workflow(client, dispatch={name: value}))
    for before, after in zip(baseline, blocks):
        assert after == {**before, name: value}


def test_all_three_are_stored_together(client, db):
    switches = {"code_intelligence": "on", "quality_gates": "on", "jev_judgments": "off"}
    for block in _blocks(db, _workflow(client, dispatch=switches)):
        assert {k: block[k] for k in SWITCHES} == switches


def test_no_map_stores_exactly_todays_block(client, db):
    for block in _blocks(db, _workflow(client)):
        assert block == {"strategy": "collect", "carrier": "checkpoints"}
    for block in _blocks(db, _workflow(client, dispatch={})):
        assert block == {"strategy": "collect", "carrier": "checkpoints"}


def test_a_task_carries_them_too(client, db):
    response = client.post("/v1/tasks", headers=auth_header("alice"),
                           json={"runner_profile": "mock",
                                 "dispatch": {"code_intelligence": "off"}})
    assert response.status_code == 201, response.text
    task = db.docs[f"tasks/{response.json()['task']['id']}"]
    assert task["metadata"]["dispatch"] == {"strategy": "collect", "carrier": "checkpoints",
                                            "code_intelligence": "off"}


@pytest.mark.parametrize("bad", ["ON", "true", "yes", "", "enabled", True, 1, None])
@pytest.mark.parametrize("name", SWITCHES)
def test_any_other_word_is_refused_with_the_accepted_values_named(client, db, name, bad):
    response = _workflow(client, dispatch={name: bad})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert "on" in body["message"] and "off" in body["message"]
    assert body["detail"]["accepted_switch_values"] == ["on", "off"]
    assert not [key for key in db.docs if key.startswith("tasks/")]


def test_an_unknown_switch_is_refused_with_the_accepted_switches_named(client, db):
    response = _workflow(client, dispatch={"code_intelligence": "on", "image": "evil"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dispatch"
    assert body["detail"]["accepted_switches"] == list(SWITCHES)
    for name in SWITCHES:
        assert name in body["message"]


def test_a_map_that_is_not_one_is_refused():
    with pytest.raises(DispatchOptionError):
        resolve_dispatch_options(strategy="collect", carrier="checkpoints", scale="workflow",
                                 repository_url=None, switches="on")


def test_the_resolver_keeps_the_switches_through_every_step_derivation():
    options = resolve_dispatch_options(strategy="collect", carrier="checkpoints",
                                       scale="workflow", repository_url=None,
                                       switches={"jev_judgments": "on", "quality_gates": "off"})
    routed = options.with_role("contributor").with_routing(builds_on=None, gate_task_id=None)
    block = routed.to_metadata()
    assert block["quality_gates"] == "off" and block["jev_judgments"] == "on"
    assert "code_intelligence" not in block
