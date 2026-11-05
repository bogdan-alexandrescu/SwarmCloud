"""`metadata.issue_run` is reserved: written only by the issue-run code
(`issueruns`, `issueci`), refused from every caller (BGD, 2026-11-04).

The key links a task or workflow to the issue run that started it: the
console's "started from" names it, `resolve_fix_rounds` gives an issue run's
merge step no rounds of its own, and the failure digest and the schedule
sweep follow it back to the run. A caller who set it would have its work
read as an issue run's. So it is refused like every other platform-written
key, in the same 422, and the issue-run code gets past the check with a
scoped exemption of the exact value, as a schedule's firing does.

The issue-run flows that carry the key -- the planner, the compiled
workflow, a CI fix round and the merge -- are held admitted by
test_issue_runs.py, test_issue_run_ci.py and test_issue_run_auto_merge.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import issueci, issueruns, task_input
from swarm_api.errors import ValidationFailed
from swarm_api.validation import (
    ISSUE_RUN_METADATA_KEY,
    RESERVED_METADATA_KEYS,
    issue_run_mark_allowed,
    reject_reserved_metadata,
    schedule_mark_allowed,
)

from .conftest import auth_header

#: The code every reserved key is refused with (test_input_from_is_reserved.py).
RESERVED_CODE = "invalid_dispatch"

#: A mark as a compiled workflow carries it, for a run that exists nowhere.
MARK = {"run_id": "run_forged", "issue": "example-org/widgets#42"}


def _created(db) -> dict[str, list[str]]:
    return {"tasks": db.paths("tasks/"), "workflows": db.paths("workflows/")}


def _assert_refused(response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == RESERVED_CODE
    assert body["detail"]["reserved_metadata_keys"] == [ISSUE_RUN_METADATA_KEY]
    # A plain message: what the key is, and what to do instead.
    assert body["message"] == (
        "metadata.issue_run is reserved: it links work to the issue run that started it, "
        "and only the platform writes it. Start an issue run with POST /v1/runs instead."
    )


def test_the_key_is_reserved_and_served_as_stored():
    # MUTATION: drop ISSUE_RUN_METADATA_KEY from RESERVED_METADATA_KEYS and this is red.
    assert ISSUE_RUN_METADATA_KEY in RESERVED_METADATA_KEYS
    assert ISSUE_RUN_METADATA_KEY == "issue_run"
    assert ISSUE_RUN_METADATA_KEY in task_input.PLATFORM_METADATA_KEYS


@pytest.mark.parametrize("value", [MARK, "run_forged", {}, None], ids=repr)
def test_reject_reserved_metadata_refuses_the_key_whatever_its_value(value):
    with pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({"unit": "x", ISSUE_RUN_METADATA_KEY: value})
    assert exc.value.code == RESERVED_CODE
    assert exc.value.detail == {"reserved_metadata_keys": [ISSUE_RUN_METADATA_KEY]}


def test_a_task_carrying_the_key_is_refused_and_creates_nothing(client, db):
    before = _created(db)
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "x"},
              "metadata": {ISSUE_RUN_METADATA_KEY: "run_forged"}},
    )
    _assert_refused(response)
    assert _created(db) == before


def _workflow(step_metadata: dict[str, Any] | None = None,
              metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"step_id": "work", "runner_profile": "mock", "input": {"prompt": "x"}}
    if step_metadata is not None:
        step["metadata"] = step_metadata
    body: dict[str, Any] = {"steps": [step]}
    if metadata is not None:
        body["metadata"] = metadata
    return body


@pytest.mark.parametrize("where", ["step", "workflow"])
def test_a_workflow_carrying_the_key_is_refused_and_creates_nothing(client, db, where):
    before = _created(db)
    mark = {ISSUE_RUN_METADATA_KEY: dict(MARK)}
    body = _workflow(step_metadata=mark) if where == "step" else _workflow(metadata=mark)
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    _assert_refused(response)
    assert _created(db) == before


def test_a_workflow_without_the_key_is_admitted(client):
    # CONTROL for the refusals above: the same body, unmarked, is created.
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow())
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# The issue-run code's exemption
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mark", [MARK, "run_a"], ids=repr)
def test_the_exemption_admits_the_exact_value_inside_its_block(mark):
    with issue_run_mark_allowed(mark):
        reject_reserved_metadata({ISSUE_RUN_METADATA_KEY: mark})
    with pytest.raises(ValidationFailed):
        reject_reserved_metadata({ISSUE_RUN_METADATA_KEY: mark})


def test_the_exemption_refuses_any_other_value():
    with issue_run_mark_allowed("run_a"), pytest.raises(ValidationFailed) as exc:
        reject_reserved_metadata({ISSUE_RUN_METADATA_KEY: "run_b"})
    assert exc.value.detail == {"reserved_metadata_keys": [ISSUE_RUN_METADATA_KEY]}


def test_neither_mark_exempts_the_others_key():
    with schedule_mark_allowed({"id": "s"}), pytest.raises(ValidationFailed):
        reject_reserved_metadata({ISSUE_RUN_METADATA_KEY: {"id": "s"}})
    with issue_run_mark_allowed({"id": "s"}), pytest.raises(ValidationFailed):
        reject_reserved_metadata({"schedule": {"id": "s"}})


def test_only_the_issue_run_code_enters_the_exemption():
    """The scope: `issue_run_mark_allowed` is entered by `issueruns.submitting`
    alone, which routes/runs.py and issueci.py wrap their own submissions in,
    and by nothing else in swarm-api."""
    import pathlib

    import swarm_api

    root = pathlib.Path(swarm_api.__file__).parent
    entering = sorted(
        str(path.relative_to(root)) for path in root.rglob("*.py")
        if "issue_run_mark_allowed(" in path.read_text(encoding="utf-8")
        and path.name != "validation.py"
    )
    assert entering == ["issueruns.py"]
    assert issueci.submitting is issueruns.submitting
    for rel in ("routes/runs.py", "issueci.py"):
        assert "submitting(" in (root / rel).read_text(encoding="utf-8"), rel
