"""A step's plugins in its own `plugins` field, carried in its signed work contract.

The owner picked a per-step `plugins` field on PR #3264 (5 November 2026): a
caller names a step's plugins, mode and skills on the step, never by writing
a work contract. swarm-api checks them (`plugins.check_step`, pointing at
`plugins`) and carries them in the step's work contract, its own or the
smallest one `workcontract.decode` accepts (`plugins.step_contract`), whose
digest the signed dispatch block holds, so the worker loads exactly them.
Offline: FakeFirestore, a local spec signer.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import plugins as worker_plugins
from swarm_api import plugins, workcontract as wc
from swarm_common.workcontract import contract_digest

from .conftest import auth_header
from .test_plugins_setting import (  # noqa: F401 - fixtures are used by name
    _allow,
    _contract,
    _tasks,
    settings_key,
    sub_client,
    switch_on,
)
from .test_qa_targets import api_context, resolver, seeded  # noqa: F401

REVIEW = {"gstack": {"mode": "unattended", "skills": ["review", "qa"]}}


def _task(client, chosen: Any, profile: str = "claude-code", **extra: Any):
    return client.post("/v1/tasks", headers=auth_header("alice"), json={
        "runner_profile": profile, "input": {"prompt": "review"}, "plugins": chosen, **extra})


def test_the_smallest_contract_decodes_strict_and_compiles():
    document = plugins.step_contract(REVIEW, tenant_id="eng", step_id="Review_1",
                                     runner_profile="claude-code", owner="alice@example.com")
    contract = wc.decode(document)
    assert contract.strictness == wc.STRICT
    assert {n: (c["mode"], list(c["skills"])) for n, c in contract.plugins.items()} == {
        "gstack": ("unattended", ["qa", "review"])}
    compiled = wc.compile_contract(contract, step_id="Review_1")
    assert compiled.step["runner_profile"] == "claude-code"
    assert compiled.advisory == ()


@pytest.mark.parametrize("step_id", ["a", "Review_Step", "-x", None, "x" * 64])
def test_every_step_id_folds_into_a_contract_id(step_id):
    assert wc._CONTRACT_ID.match(plugins.contract_id_of(step_id))


def test_a_task_naming_plugins_stores_them_signed_and_the_worker_reads_them(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key)
    response = _task(sub_client, REVIEW)
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    stored = task["metadata"][wc.METADATA_KEY]
    assert stored["capabilities"]["plugins"] == {"gstack": {"mode": "unattended",
                                                            "skills": ["qa", "review"]}}
    assert stored["ownership"]["owner"] == "alice@example.com"
    assert stored["limits"]["runner_profile"] == "claude-code"
    assert task["metadata"]["dispatch"][wc.DISPATCH_FIELD]["digest"] == contract_digest(stored)
    assert task["spec_signature"]
    # MUTATION: drop `plugins=` from either TaskCreate or `with_step_plugins`
    # and the worker reads none.
    assert worker_plugins.selected(task) == {
        "gstack": {"mode": "unattended", "skills": ("qa", "review")}}


def test_a_workflow_step_naming_plugins_stores_them_on_its_task(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key)
    steps = [{"step_id": "review", "runner_profile": "claude-code",
              "input": {"prompt": "review"}, "plugins": REVIEW},
             {"step_id": "plain", "runner_profile": "claude-code",
              "input": {"prompt": "plain"}}]
    response = sub_client.post("/v1/workflows", headers=auth_header("alice"), json={
        "on_step_failure": "continue", "strategy": "direct-pr",
        "repository_url": "https://github.com/acme/widgets.git", "steps": steps})
    assert response.status_code == 201, response.text
    by_step = {t["metadata"]["workflow_step"]: t for t in _tasks(db)}
    assert worker_plugins.selected(by_step["review"]) == {
        "gstack": {"mode": "unattended", "skills": ("qa", "review")}}
    assert wc.METADATA_KEY not in by_step["plain"]["metadata"]
    assert worker_plugins.selected(by_step["plain"]) == {}


@pytest.mark.parametrize("chosen, profile, words", [
    ({"gstack": {"mode": "unattended", "skills": ["review"]}}, "codex", "runs only on the profiles"),
    ({"gstack": {"mode": "planner", "skills": ["spec"]}}, "claude-code", "question bridge"),
    ({"gstack": {"mode": "unattended", "skills": ["ship-it"]}}, "claude-code", "does not offer ship-it"),
    ({"gstack": {"mode": "unattended", "skills": []}}, "claude-code", "at least one skill"),
    ({"nope": {"mode": "unattended", "skills": ["review"]}}, "claude-code", "not in the plugin catalogue"),
    ({"security-audit": {"mode": "unattended", "skills": ["security-audit"]}}, "claude-code",
     "has not allowed security-audit"),
])
def test_what_the_platform_cannot_run_is_refused_at_the_field(
        sub_client, db, switch_on, settings_key, chosen, profile, words):
    _allow(db, settings_key)
    response = _task(sub_client, chosen, profile)
    assert response.status_code == 422, response.text
    body = response.json()
    assert words in body["message"]
    assert body["detail"]["plugins"] == "plugin_refused"
    assert body["detail"]["field"] == plugins.STEP_FIELD
    assert _tasks(db) == []


def test_with_work_contracts_off_a_step_naming_plugins_is_refused(sub_client, db, settings_key):
    _allow(db, settings_key)
    response = _task(sub_client, REVIEW)
    # Stored, it would run without them: the worker loads only signed plugins.
    assert response.status_code == 422, response.text
    assert wc.SWITCH_ENV in response.json()["message"]
    assert response.json()["detail"]["field"] == plugins.STEP_FIELD
    assert _tasks(db) == []


@pytest.mark.parametrize("chosen", [
    {"gstack": {"mode": "unattended", "skills": ["review"], "path": "/opt/x"}},
    {"gstack": {"mode": "unattended", "skills": ["review"], "version": "1.0"}},
])
def test_a_plugin_takes_no_path_or_version(sub_client, db, switch_on, settings_key, chosen):
    _allow(db, settings_key)
    assert _task(sub_client, chosen).status_code == 422
    assert _tasks(db) == []


def test_a_contract_the_caller_sent_gets_the_fields_plugins(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key)
    contract = _contract()
    del contract["capabilities"]
    response = _task(sub_client, REVIEW, metadata={wc.METADATA_KEY: contract})
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    assert task["metadata"][wc.METADATA_KEY]["contract_id"] == "wc-review-widgets"
    assert worker_plugins.selected(task) == {
        "gstack": {"mode": "unattended", "skills": ("qa", "review")}}


def test_a_contract_naming_the_same_plugins_in_another_order_is_accepted(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key)
    contract = _contract(skills=["qa", "review"])
    response = _task(sub_client, REVIEW, metadata={wc.METADATA_KEY: contract})
    assert response.status_code == 201, response.text


def test_a_contract_naming_other_plugins_than_the_field_is_refused(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key)
    contract = _contract(skills=["health"])
    response = _task(sub_client, REVIEW, metadata={wc.METADATA_KEY: contract})
    assert response.status_code == 422, response.text
    assert "different plugins" in response.json()["message"]
    assert _tasks(db) == []
