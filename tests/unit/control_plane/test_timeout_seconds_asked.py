"""`Task.timeout_seconds_asked`: swarm-api records the ask it lowered (contract request 131).

The owner's answer to #2981's question, "API records the ask" (accepted
2026-11-01, part of #2688). A spec asking 10,800 s of a profile capped at
7,200 s runs under 7,200 s, as before; now the task document and the API say
what it asked, and the worker's existing reader (`lifecycle.Worker.
_timeout_asked_env`) hands the record to the runner, which reports the clamp
in `limits.clamped`.

A RECORD ONLY (invariant 10): the limit stays `timeout_seconds`, nothing reads
the record as one, and no caller can set it -- not as a top-level field, not
in `metadata`, not in `input`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import lifecycle
from swarm_api.codec import task_from_dict, task_to_api
from swarm_api.validation import validate_timeout
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header, seed_tenant
from .test_child_tasks_api import Attempt, _children, child_client, ready_parent, submit  # noqa: F401
from .test_child_tasks_api import worker_tokens  # noqa: F401

FIELD = "timeout_seconds_asked"
#: The `mock` profile's cap: what a longer ask is lowered to.
CAP = RUNNER_PROFILES["mock"].timeout_seconds


def _tasks(db) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items()
            if path.startswith("tasks/") and path.count("/") == 1]


def _post_task(client, **body: Any):
    return client.post("/v1/tasks", headers=auth_header("alice"),
                       json={"runner_profile": "mock", "input": {"prompt": "go"}, **body})


def _worker_reports(doc: dict[str, Any]) -> dict[str, str]:
    """What the worker's existing reader hands the runner for this document,
    run under the timeout the document stores."""
    worker = SimpleNamespace(_task=doc, cfg=SimpleNamespace(timeout_seconds=doc["timeout_seconds"]))
    return lifecycle.Worker._timeout_asked_env(worker)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_ask_is_recorded_only_when_it_is_above_every_granted_timeout():
    from swarm_api.validation import timeout_asked

    assert timeout_asked(10_800, 7_200) == 10_800
    assert timeout_asked(7_200, 7_200) is None
    assert timeout_asked(60, 7_200) is None
    assert timeout_asked(None, 7_200) is None
    # A `browser_if_ui` step's `code` alternate: lowered on either is recorded,
    # and the worker compares with the one it runs under.
    assert timeout_asked(5_000, 7_200, 3_600) == 5_000
    assert timeout_asked(3_600, 7_200, 3_600) is None


def test_the_limit_is_still_validate_timeout_alone():
    """Invariant 10: recording the ask lengthens nothing."""
    profile = RUNNER_PROFILES["mock"]
    assert validate_timeout(profile, CAP * 3) == CAP


# ---------------------------------------------------------------------------
# A task, a batch entry, a workflow step
# ---------------------------------------------------------------------------


def test_a_submission_asking_above_the_cap_stores_and_serves_the_ask(client, db):
    seed_tenant(db, "eng")
    response = _post_task(client, timeout_seconds=CAP * 3)
    assert response.status_code == 201, response.text
    (doc,) = _tasks(db)
    # MUTATION: not writing it in `_build_task` turns this red.
    assert doc["timeout_seconds"] == CAP and doc[FIELD] == CAP * 3
    served = client.get(f"/v1/tasks/{doc['id']}", headers=auth_header("alice")).json()["task"]
    # MUTATION: not reading it in `task_from_dict`, or not serving it in
    # `task_to_api`, turns this red.
    assert served["timeout_seconds"] == CAP and served[FIELD] == CAP * 3
    # The worker's existing reader reports the clamp to the runner.
    assert _worker_reports(doc) == {lifecycle.TIMEOUT_ASKED_ENV: str(CAP * 3)}


@pytest.mark.parametrize("asked", [None, CAP, CAP // 2])
def test_a_submission_asking_at_or_below_the_cap_stores_none(client, db, asked):
    seed_tenant(db, "eng")
    body = {} if asked is None else {"timeout_seconds": asked}
    response = _post_task(client, **body)
    assert response.status_code == 201, response.text
    (doc,) = _tasks(db)
    assert doc.get(FIELD) is None
    served = client.get(f"/v1/tasks/{doc['id']}", headers=auth_header("alice")).json()["task"]
    assert served[FIELD] is None
    assert _worker_reports(doc) == {}


def test_a_batch_entry_records_its_own_ask(client, db):
    seed_tenant(db, "eng")
    response = client.post("/v1/tasks/batch", headers=auth_header("alice"), json={"tasks": [
        {"runner_profile": "mock", "input": {"prompt": "long"}, "timeout_seconds": CAP + 1},
        {"runner_profile": "mock", "input": {"prompt": "short"}, "timeout_seconds": CAP - 1},
    ]})
    assert response.status_code == 201, response.text
    by_prompt = {doc["input"]["prompt"]: doc for doc in _tasks(db)}
    assert by_prompt["long"][FIELD] == CAP + 1
    assert by_prompt["short"][FIELD] is None
    served = {t["input"]["prompt"]: t for t in response.json()["tasks"]}
    assert served["long"][FIELD] == CAP + 1 and served["short"][FIELD] is None


def test_a_workflow_step_records_its_own_ask(client, db):
    seed_tenant(db, "eng")
    response = client.post("/v1/workflows", headers=auth_header("alice"), json={
        "on_step_failure": "continue",
        "steps": [
            {"step_id": "long", "runner_profile": "mock", "input": {"prompt": "a"},
             "timeout_seconds": CAP * 2},
            {"step_id": "short", "runner_profile": "mock", "input": {"prompt": "b"},
             "depends_on": ["long"]},
        ],
    })
    assert response.status_code == 201, response.text
    by_step = {doc["step_id"]: doc for doc in _tasks(db)}
    assert by_step["long"]["timeout_seconds"] == CAP and by_step["long"][FIELD] == CAP * 2
    assert by_step["short"][FIELD] is None


# ---------------------------------------------------------------------------
# A child, lowered again to its parent's timeout
# ---------------------------------------------------------------------------


def test_a_child_lowered_to_its_parents_timeout_records_its_own_ask(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    # The parent's timeout (300) is below the child's profile cap (600), so
    # the child is lowered by its parent, not by its profile.
    ready_parent(child_client, db, attempt, timeout_seconds=300)
    response = submit(child_client, attempt,
                      child={"runner_profile": "mock", "input": {"prompt": "help"},
                             "timeout_seconds": 500})
    assert response.status_code == 201, response.text
    (child,) = _children(db).values()
    # MUTATION: dropping the `replace`'s record leaves None, since the
    # `TaskCreate` `_build_task` saw already asked only the parent's 300.
    assert child["timeout_seconds"] == 300 and child[FIELD] == 500
    assert response.json()["task"][FIELD] == 500


def test_a_child_asking_within_its_parents_timeout_records_none(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt, timeout_seconds=300)
    response = submit(child_client, attempt,
                      child={"runner_profile": "mock", "input": {"prompt": "help"},
                             "timeout_seconds": 200})
    assert response.status_code == 201, response.text
    (child,) = _children(db).values()
    assert child["timeout_seconds"] == 200 and child[FIELD] is None


# ---------------------------------------------------------------------------
# No caller sets it
# ---------------------------------------------------------------------------


def test_a_caller_cannot_set_it_as_a_top_level_field(client, db):
    seed_tenant(db, "eng")
    response = _post_task(client, **{FIELD: 99_999})
    assert response.status_code == 422, response.text
    assert not _tasks(db)


def test_a_caller_cannot_set_it_in_metadata(client, db):
    seed_tenant(db, "eng")
    response = _post_task(client, metadata={FIELD: 99_999})
    # MUTATION: dropping it from `REFUSED_UNWRITTEN_METADATA_KEYS` turns this red.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reserved_metadata_keys"] == [FIELD]
    assert not _tasks(db)


def test_a_caller_cannot_set_it_in_input(client, db):
    seed_tenant(db, "eng")
    response = _post_task(client, input={"prompt": "go", FIELD: 99_999})
    assert response.status_code == 422, response.text
    assert not _tasks(db)


def test_a_child_cannot_set_it(child_client, db):
    seed_tenant(db, "eng")
    attempt = Attempt()
    ready_parent(child_client, db, attempt)
    response = submit(child_client, attempt,
                      child={"runner_profile": "mock", "input": {"prompt": "help"},
                             FIELD: 99_999})
    assert response.status_code == 422, response.text
    assert not _children(db)


# ---------------------------------------------------------------------------
# The codec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored, read", [
    (10_800, 10_800), (None, None), (0, None), (-5, None), ("10800", None), (True, None),
])
def test_the_codec_reads_an_int_above_zero_and_anything_else_as_none(stored, read):
    from datetime import datetime, timezone

    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    doc = {"id": "task_1", "tenant_id": "eng", "created_at": now, "updated_at": now,
           "state": "READY", "runner_profile": "mock", "resource_class": "standard",
           "timeout_seconds": CAP}
    if stored is not None:
        doc[FIELD] = stored
    task = task_from_dict(doc)
    assert task.timeout_seconds_asked == read
    assert task_to_api(task)[FIELD] == read
    # A whole document written back from a decoded task keeps the record.
    assert task.to_firestore()[FIELD] == read
