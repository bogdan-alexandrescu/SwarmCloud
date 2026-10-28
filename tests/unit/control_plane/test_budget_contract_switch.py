"""The per-agent budget on the applied contract (BUDGET-CONTRACT, part of #2459).

BGD accepted contract requests 118-121 on 2026-10-26 (~22:15Z), and this lane
applied them. On swarm-api's side that means:

  * 120: submission writes the RESOLVED budget into the typed `Task.budget`
    (signed at spec format 6) and what each step ASKED for into
    `WorkflowStep.budget`; `metadata.dispatch.budget` is written no more, and
    a task stored with it is read through it for one release;
  * 119: the workspace default is a signed settings block: `BudgetStore`
    MACs every write and, for a tenant on the cutover, acts only on a block
    that verifies;
  * 121: the old warning, a `running` event with `detail.cause:
    "budget_warning"`, is read as `EventType.BUDGET_WARNING`;
  * 118: the ledger classes `EndCause.BUDGET_EXCEEDED`.

Offline: FakeFirestore, a local spec signer. No credentials, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import budget, codec, judgment, outcomes
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.judgment import SettingsMac
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker
from swarm_common import specsign
from swarm_common.models import EndCause
from swarm_common.specsign import SETTINGS_MAC_FIELDS
from swarm_common.states import EventType

from .conftest import PROJECT, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore
from .spec_signer import LocalSpecSigner

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def client(db, tokens, group_map, signer) -> TestClient:
    seed_tenant(db, "eng")
    context = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def _workflows(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("workflows/") and p.count("/") == 1]


def _submit(client, **extra: Any):
    body = {"runner_profile": "mock", "input": {"prompt": "hello"}, **extra}
    return client.post("/v1/tasks", headers=auth_header("alice"), json=body)


# --------------------------------------------------------------------------
# Request 120: the typed field, signed at format 6
# --------------------------------------------------------------------------


def test_a_budgeted_task_pins_the_typed_field_and_signs_it_at_format_6(client, db, signer):
    response = _submit(client, budget={"tokens": 40_000})
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    # MUTATION: keep pinning in `metadata.dispatch.budget` and this is None.
    assert task["budget"] == {"tokens": 40_000}
    dispatch = task["metadata"]["dispatch"]
    assert budget.DISPATCH_KEY not in dispatch, "the dispatch key is written no more"
    assert dispatch[budget.PROVENANCE_KEY] == {
        "step": {"tokens": 40_000}, "workspace": {}, "ceiling": {}}
    assert task["spec_format"] == 6 == specsign.SPEC_FORMAT
    assert signer.verifies(task, task["id"])
    # Inside the signed bytes: raising or removing it fails verification.
    assert not signer.verifies({**task, "budget": {"tokens": 4_000_000}}, task["id"])
    assert not signer.verifies({**task, "budget": None}, task["id"])
    # Served at the top level and, as before, in the dispatch view.
    served = response.json()["task"]
    assert served["budget"] == {"tokens": 40_000}
    assert served["dispatch"]["budget"] == {
        "tokens": 40_000, "from": {"step": {"tokens": 40_000}, "workspace": {}, "ceiling": {}}}


def test_a_task_with_no_budget_keeps_the_format_and_bytes_it_had(client, db, signer):
    assert _submit(client).status_code == 201
    (task,) = _tasks(db)
    assert task["budget"] is None
    # MUTATION: sign every task at SPEC_FORMAT and this is 6.
    assert task["spec_format"] == 1
    assert signer.verifies(task, task["id"])


def test_a_workflow_records_what_each_step_asked_for(client, db):
    assert client.put("/v1/workspaces/eng/budget", headers=auth_header("root"),
                      json={"budget": {"unit": "usd", "amount": 3}}).status_code == 200
    steps = [
        {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}, "budget": {"usd": 1}},
        {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "b"}, "depends_on": ["a"]},
    ]
    response = client.post("/v1/workflows", headers=auth_header("alice"), json={"steps": steps})
    assert response.status_code == 201, response.text
    (workflow,) = _workflows(db)
    asked = {s["step_id"]: s.get("budget") for s in workflow["steps"]}
    # MUTATION: leave `WorkflowStep.budget` unset and "a" reads None.
    assert asked == {"a": {"usd": 1.0}, "b": None}
    resolved = {t["step_id"]: t["budget"] for t in _tasks(db)}
    assert resolved == {"a": {"usd": 1.0}, "b": {"usd": 3.0}}
    served = {s["step_id"]: s["budget"] for s in response.json()["workflow"]["steps"]}
    assert served == {"a": {"usd": 1.0}, "b": None}


def test_a_task_stored_before_the_field_is_served_from_its_dispatch_block():
    """The one-release fallback: a task pinned in `metadata.dispatch.budget`."""
    legacy = {"dispatch": {"strategy": "collect", "budget": {
        "usd": 1.5, "from": {"step": {"usd": 1.5}, "workspace": {}, "ceiling": {}}}}}
    # MUTATION: read only `Task.budget` and this is None.
    assert codec.served_budget(None, legacy) == {"usd": 1.5}
    assert codec.served_budget({"tokens": 900}, legacy) == {"tokens": 900}, "the typed field wins"
    assert codec.served_budget(None, {"dispatch": {"strategy": "collect"}}) is None
    assert codec.served_budget(None, {}) is None
    assert codec.served_budget({"usd": 0, "tokens": True}, {}) is None


def test_the_dispatch_view_of_a_legacy_task_keeps_its_shape(db):
    doc = {
        "id": "task_old", "tenant_id": "eng", "created_at": NOW, "updated_at": NOW,
        "state": "RUNNING", "runner_profile": "mock", "resource_class": "standard",
        "input": {}, "submitted_by": "alice@example.com",
        "metadata": {"dispatch": {"strategy": "collect", "budget": {
            "tokens": 15_000_000, "from": {"workspace": {"tokens": 15_000_000}}}}},
    }
    task = codec.task_from_dict(doc)
    assert task.budget is None
    assert codec.dispatch_of(task)["budget"] == {
        "tokens": 15_000_000, "from": {"workspace": {"tokens": 15_000_000}}}


def test_the_codec_reads_the_typed_fields_back():
    doc = {
        "id": "task_new", "tenant_id": "eng", "created_at": NOW, "updated_at": NOW,
        "state": "READY", "runner_profile": "mock", "resource_class": "standard",
        "input": {}, "submitted_by": "alice@example.com", "budget": {"usd": 2.0},
    }
    # MUTATION: drop it from `task_from_dict` and a decode-and-rewrite loses
    # the signed field, so the worker refuses the spec.
    assert codec.task_from_dict(doc).budget == {"usd": 2.0}
    assert codec.task_from_dict({**doc, "budget": "lots"}).budget is None
    workflow = codec.workflow_from_dict({
        "workflow_id": "wf_1", "tenant_id": "eng", "created_at": NOW, "updated_at": NOW,
        "state": "RUNNING", "submitted_by": "alice@example.com",
        "steps": [{"step_id": "a", "runner_profile": "mock", "input": {},
                   "budget": {"tokens": 10}}],
    })
    assert workflow.steps[0].budget == {"tokens": 10}


# --------------------------------------------------------------------------
# Request 121: the old warning is read as the new type
# --------------------------------------------------------------------------


def test_the_old_running_warning_is_read_as_budget_warning():
    detail = {"cause": "budget_warning", "percent": 80, "told": True}
    # MUTATION: drop the legacy branch and this is RUNNING.
    assert codec.stored_event_type({"type": "running", "detail": detail}) is EventType.BUDGET_WARNING
    assert codec.stored_event_type({"type": "budget_warning", "detail": detail}) \
        is EventType.BUDGET_WARNING
    # The control: every other running event stays one.
    assert codec.stored_event_type({"type": "running", "detail": {"cause": "account_assigned"}}) \
        is EventType.RUNNING
    assert codec.stored_event_type({"type": "running", "detail": {}}) is EventType.RUNNING


# --------------------------------------------------------------------------
# Request 119: the workspace default is signed
# --------------------------------------------------------------------------


def _key() -> str:
    return "settings-" + secrets.token_hex(24)


def test_the_budget_block_is_listed_and_its_writer_owns_both_fields():
    assert SETTINGS_MAC_FIELDS["budget"] == budget.COVERED == ("unit", "amount")
    # MUTATION: unlist the block and `SIGNED` goes False with it.
    assert budget.SIGNED is True
    assert judgment.WRITER_FIELDS["budget"] == budget.COVERED
    assert judgment.unowned_covered_keys("budget") == ()


def test_the_writer_signs_the_block_and_the_reader_verifies_it():
    key = _key()
    db = FakeFirestore()
    mac = SettingsMac(keys=(key,), tenants=("eng",))
    store = budget.BudgetStore(db, now=lambda: NOW, mac=mac)
    store.put("eng", budget.Limits(tokens=15_000_000), by="root@example.com")
    stored = db.docs["workspace_settings/eng"]["budget"]
    # MUTATION: write the block without its MAC and this has no `mac`.
    assert judgment.block_verifies("eng", "budget", stored, (key,))
    assert store.default("eng") == budget.Limits(tokens=15_000_000)


def test_a_forged_default_is_no_default_for_a_tenant_on_the_cutover():
    key = _key()
    db = FakeFirestore()
    db.docs["workspace_settings/eng"] = {
        "tenant_id": "eng", "budget": {"unit": "usd", "amount": 9_999}}
    on = budget.BudgetStore(db, now=lambda: NOW, mac=SettingsMac(keys=(key,), tenants=("eng",)))
    # MUTATION: read the block unverified and the forged $9,999 is the default.
    assert on.default("eng") == budget.NONE
    # A tenant not yet cut over reads it as stored, as every block does.
    off = budget.BudgetStore(db, now=lambda: NOW, mac=SettingsMac(keys=(key,), tenants=()))
    assert off.default("eng") == budget.Limits(usd=9_999.0)


def test_a_block_signed_for_one_tenant_does_not_verify_on_another():
    key = _key()
    db = FakeFirestore()
    store = budget.BudgetStore(db, now=lambda: NOW,
                               mac=SettingsMac(keys=(key,), tenants=("eng", "research")))
    store.put("eng", budget.Limits(usd=2.0), by="root@example.com")
    db.docs["workspace_settings/research"] = {
        "tenant_id": "research", "budget": dict(db.docs["workspace_settings/eng"]["budget"])}
    assert store.default("research") == budget.NONE


# --------------------------------------------------------------------------
# Request 118: the ledger classes the new cause
# --------------------------------------------------------------------------


def test_a_budget_stop_is_classed_by_its_cause_as_its_own_class():
    text = ("budget_exceeded: the agent used $1.02 of a budget of $1.00 and was stopped "
            "at a turn boundary; its work was checkpointed and published as it stood")
    by_cause = outcomes.classify_failure("FAILED", text, 143, end_cause=EndCause.BUDGET_EXCEEDED)
    by_text = outcomes.classify_failure("FAILED", text, 143)
    # MUTATION: leave the member out of `_FAILURE_OF_CAUSE` and this is "other".
    # BGD, 2026-10-27: its own class, not the runner error its text reads as.
    assert by_cause == "budget_exceeded"
    # No text rule was added (CLASSIFIER_VERSION stays): the cause is typed
    # wherever a budget stop is written.
    assert by_text == "runner_error"
