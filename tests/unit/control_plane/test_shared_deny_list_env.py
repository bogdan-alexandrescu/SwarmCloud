"""swarm-api's never-touch list comes from the environment and fails closed
(docs/design/generic-repository.md section 2.4).

Terraform renders two variables on swarm-api: `SWARM_SHARED_DENY_LIST`, a
JSON array of the shared project's other-team resource names, and
`SWARM_SHARED_DENY_LIST_SET=1`. Without the marker the list is unknown, and an
issue run whose plan touches infrastructure is refused at plan with the fixed
code DENY_LIST_UNSET: never treated as an empty list. With it, the names are
the ones the variable holds.

Against the real app and the in-memory Firestore; no credentials, no network.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from swarm_api import issueruns

from .conftest import auth_header
from .test_issue_run_holds import (  # noqa: F401  (fixtures, by name)
    PLAIN_PLAN,
    _grants,
    _plan,
    _planned,
    _workflows,
    api_context,
    client,
    github,
    group_map,
    labels,
)

_SPEC = importlib.util.spec_from_file_location(
    "placeholders", Path(__file__).resolve().parents[2] / "placeholders.py")
assert _SPEC and _SPEC.loader
placeholders = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(placeholders)
PEER = placeholders.PEER

INFRA_PLAN = _plan("terraform/infra/network.tf", "src/widgets/list.py")


@pytest.fixture
def unrendered(monkeypatch):
    monkeypatch.delenv("SWARM_SHARED_DENY_LIST_SET", raising=False)
    monkeypatch.delenv("SWARM_SHARED_DENY_LIST", raising=False)


def _render(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SWARM_SHARED_DENY_LIST", value)
    monkeypatch.setenv("SWARM_SHARED_DENY_LIST_SET", "1")


# --------------------------------------------------------------------------
# no marker: refused, never empty
# --------------------------------------------------------------------------

def test_without_the_marker_an_infrastructure_run_is_refused_at_plan(client, db, objects, unrendered):
    run = _planned(client, db, objects, INFRA_PLAN)

    # MUTATION: treat an absent list as empty and the run is PLANNED.
    assert run["state"] == "FAILED"
    # MUTATION: interpolate the plan's paths and the error is not the constant.
    assert run["error"] == issueruns.DENY_LIST_UNSET_REFUSAL
    assert "terraform/infra/network.tf" not in run["error"]
    assert "SWARM_SHARED_DENY_LIST_SET" in run["error"]
    assert run["plan"] is None
    assert not _workflows(db)


def test_without_the_marker_a_list_alone_is_not_read(monkeypatch, client, db, objects):
    monkeypatch.delenv("SWARM_SHARED_DENY_LIST_SET", raising=False)
    monkeypatch.setenv("SWARM_SHARED_DENY_LIST", json.dumps([PEER["cluster"]]))
    # MUTATION: read the list without its marker and this is a tuple.
    assert issueruns.shared_deny_list() is None
    assert _planned(client, db, objects, INFRA_PLAN)["state"] == "FAILED"


@pytest.mark.parametrize("value", ["", "not json", '{"entries": []}', '["peer-cluster", ""]', "[1]"])
def test_a_malformed_list_is_refused_like_an_absent_one(monkeypatch, value):
    _render(monkeypatch, value)
    assert issueruns.shared_deny_list() is None
    assert issueruns.deny_list_refusal(INFRA_PLAN) == issueruns.DENY_LIST_UNSET_REFUSAL


def test_without_the_marker_a_plan_touching_no_infrastructure_runs(client, db, objects, unrendered):
    # The refusal is for what can reach the shared project, not for every run.
    assert _planned(client, db, objects, PLAIN_PLAN)["state"] == "PLANNED"


def test_without_the_marker_an_edit_adding_infrastructure_is_a_422(client, db, objects, unrendered):
    run = _planned(client, db, objects, PLAIN_PLAN)
    response = client.post(
        f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("alice"),
        json={"plan_digest": run["plan_digest"], "plan": INFRA_PLAN},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["message"] == issueruns.DENY_LIST_UNSET_REFUSAL
    assert "terraform/infra/network.tf" in body["detail"]["paths"]


# --------------------------------------------------------------------------
# with the marker: the list is the variable's
# --------------------------------------------------------------------------

def test_with_the_marker_the_list_is_read_from_the_variable(monkeypatch, client, db, objects):
    names = [PEER["cluster"], PEER["network"]]
    _render(monkeypatch, json.dumps(names))
    # MUTATION: read any other source and the names differ.
    assert issueruns.shared_deny_list() == tuple(names)
    assert issueruns.deny_list_refusal(INFRA_PLAN) is None
    assert _planned(client, db, objects, INFRA_PLAN)["state"] == "PLANNED"


def test_a_rendered_empty_list_is_a_statement_not_an_absence(monkeypatch, client, db, objects):
    _render(monkeypatch, "[]")
    assert issueruns.shared_deny_list() == ()
    assert _planned(client, db, objects, INFRA_PLAN)["state"] == "PLANNED"


def test_a_rendered_name_holds_an_added_line_naming_it(monkeypatch):
    network = PEER["network"]
    _render(monkeypatch, json.dumps([network]))
    path = "terraform/infra/network.tf"
    patch = f'--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n unchanged\n+  network = "{network}"\n'
    held = issueruns.hard_stop_hold([path], platform=True, hard_stop_paths=[], patches={path: patch},
                                     prompt_text="")
    assert held is not None and held["reasons"] == ["shared"]
