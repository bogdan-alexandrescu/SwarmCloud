"""A task's input and metadata are served masked by every route, with their counts.

THE OWNER'S DECISIONS (2026-09-26, on #184):

  * "The input is masked everywhere: `GET /v1/tasks/{id}` returns a
    read-time-redacted input with its count, like /input, so the CLI and
    plugin show it masked too. The API never serves a credential-shaped string
    back, even to the submitter." PR #210 masked what the SCREENS drew
    (`/input`) and left the task route serving the input as submitted.
  * Task metadata masked too ("mask it everywhere"): Details drew
    `task.metadata` raw between two masked blocks, and `TaskCreate.metadata` is
    caller-supplied. ONE masker per task, over the input and the caller's
    metadata, so a literal masked in one is masked in the other. The keys the
    platform writes -- `dispatch`, `input_from`, `expected_outputs`, which
    submission refuses from every caller -- are served as stored.

What is pinned: every route that serves a task (get, list, create, cancel, a
workflow's tasks) and a workflow's steps serve the masked input with
`input_redaction_count`; the task serves the masked metadata with
`metadata_redaction_count`; the platform's keys are readable and uncounted;
`/input` and the task route agree, count for count; and a sweep of every GET
route finds no planted secret inside any `input` or `metadata` it serves.

MUTATIONS: serve `task.input` from `task_to_api` again; mask the input and not
the metadata; build one masker for the input and another for the metadata;
mask the platform's keys; count them; serve a workflow step's input raw.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from typing import Any

from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant

#: Shaped like the families `redaction.RULES` knows. Not real credentials.
OPENAI = "sk-proj0123456789abcdefghijklmnopqrstuv"
#: No recognisable prefix: caught only by the key/value rule, a key's name, or
#: as a literal the document named elsewhere.
BARE = "correct-horse-battery-staple-8812"
LITERAL = "zork-grue-lantern-brass-4471"


def _task(db, *, input_doc: dict[str, Any], metadata: dict[str, Any], task_id="task_a", state="RUNNING"):
    seed_tenant(db, "eng")
    seed_task(db, task_id=task_id, tenant_id="eng", state=state, runner_profile="mock")
    db.docs[f"tasks/{task_id}"]["input"] = input_doc
    db.docs[f"tasks/{task_id}"]["metadata"] = metadata


def _get(client, path: str) -> dict[str, Any]:
    response = client.get(path, headers=auth_header("alice"))
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def test_the_task_route_serves_the_input_masked_with_its_count(client, db):
    _task(
        db,
        input_doc={"prompt": f"Deploy with DB_PASSWORD={BARE} and {OPENAI}.", "steps": 2},
        metadata={},
    )
    task = _get(client, "/v1/tasks/task_a")["task"]

    assert BARE not in json.dumps(task) and OPENAI not in json.dumps(task)
    assert isinstance(task["input"], dict), "the masked input is still an object a client reads keys from"
    assert task["input"]["steps"] == 2
    assert MASK in task["input"]["prompt"]
    assert task["input_redaction_count"] == 2
    assert task["metadata_redaction_count"] == 0


def test_the_task_route_serves_the_callers_metadata_masked_and_the_platforms_as_stored(client, db):
    dispatch = {"strategy": "integrate", "carrier": "branches", "role": "integrator", "integrates": ["task_0"]}
    input_from = {"task_up": "notes.md"}
    _task(
        db,
        input_doc={"prompt": "summarise"},
        metadata={
            "dispatch": dispatch,
            "input_from": input_from,
            "expected_outputs": ["report.md"],
            "ci_token": BARE,
            "note": f"pushed with {OPENAI}",
            "unit": "fanout-3",
        },
    )
    task = _get(client, "/v1/tasks/task_a")["task"]
    metadata = task["metadata"]

    assert BARE not in json.dumps(task) and OPENAI not in json.dumps(task)
    assert metadata["ci_token"] == MASK, "a value under a credential's name is masked whole"
    assert MASK in metadata["note"]
    # The platform's keys, which only the platform can write, are readable.
    assert metadata["dispatch"] == dispatch
    assert metadata["input_from"] == input_from
    assert metadata["expected_outputs"] == ["report.md"]
    # A label is never credential-shaped, so the masker leaves it as written.
    assert metadata["unit"] == "fanout-3"
    assert list(metadata) == ["dispatch", "input_from", "expected_outputs", "ci_token", "note", "unit"]
    assert task["metadata_redaction_count"] == 2
    assert task["dispatch"]["strategy"] == "integrate", "the effective dispatch still reads the stored block"


def test_one_masker_per_task_so_a_literal_named_in_the_metadata_is_masked_in_the_prompt(client, db):
    _task(
        db,
        input_doc={"prompt": f"the deploy key is {LITERAL}; use it"},
        metadata={"deploy_secret": LITERAL},
    )
    task = _get(client, "/v1/tasks/task_a")["task"]
    copy = _get(client, "/v1/tasks/task_a/input")

    assert LITERAL not in json.dumps(task)
    assert LITERAL not in json.dumps(copy)
    assert task["input"]["prompt"] == f"the deploy key is {MASK}; use it"
    assert task["input_redaction_count"] == 1
    assert copy["prompt"]["redaction_count"] == 1, "the /input copy's prompt used the same masker"


def test_one_masker_per_task_so_a_value_the_prompt_assigns_is_masked_in_the_metadata(client, db):
    _task(
        db,
        input_doc={"prompt": f"export DB_PASSWORD={LITERAL} then run"},
        metadata={"connection": f"postgres://app:{LITERAL}@db/prod"},
    )
    task = _get(client, "/v1/tasks/task_a")["task"]

    assert LITERAL not in json.dumps(task)
    assert task["metadata"]["connection"] == f"postgres://app:{MASK}@db/prod"
    assert task["metadata_redaction_count"] == 1


def test_the_input_copy_serves_the_metadata_block_the_task_route_serves(client, db):
    _task(
        db,
        input_doc={"prompt": "summarise"},
        metadata={"dispatch": {"strategy": "collect", "carrier": "checkpoints"}, "api_key": BARE},
    )
    task = _get(client, "/v1/tasks/task_a")["task"]
    copy = _get(client, "/v1/tasks/task_a/input")

    assert copy["metadata"] == {
        "value": task["metadata"],
        "redaction_count": task["metadata_redaction_count"],
        "platform_keys": ["dispatch"],
    }
    assert copy["metadata"]["value"]["api_key"] == MASK
    assert copy["metadata"]["redaction_count"] == 1
    # The input's counts are the task route's.
    assert copy["full"]["redaction_count"] == task["input_redaction_count"]


def test_the_list_route_serves_every_task_masked(client, db):
    _task(db, input_doc={"prompt": f"use {OPENAI}"}, metadata={"note": OPENAI}, task_id="task_a")
    _task(db, input_doc={"prompt": f"PASSWORD={BARE}"}, metadata={}, task_id="task_b")
    page = _get(client, "/v1/tasks?limit=50")

    assert OPENAI not in json.dumps(page) and BARE not in json.dumps(page)
    counts = {t["id"]: (t["input_redaction_count"], t["metadata_redaction_count"]) for t in page["tasks"]}
    assert counts == {"task_a": (1, 1), "task_b": (1, 0)}


def test_a_create_response_serves_the_input_it_was_given_masked(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": f"Deploy with DB_PASSWORD={BARE}."},
            "metadata": {"ci_token": BARE},
        },
    )
    assert response.status_code == 201, response.text
    assert BARE not in response.text, "the 201 echoed the credential it was sent"
    task = response.json()["task"]
    assert task["input_redaction_count"] == 1
    assert task["metadata_redaction_count"] == 1

    # The STORED task is unchanged: the runner reads what was submitted.
    stored = db.docs[f"tasks/{task['id']}"]
    assert BARE in stored["input"]["prompt"], "masking at read time must not touch the task document"


def test_a_cancel_response_serves_the_input_masked(client, db):
    _task(db, input_doc={"prompt": f"use {OPENAI}"}, metadata={}, state="QUEUED")
    response = client.post("/v1/tasks/task_a/cancel", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert OPENAI not in response.text


def test_a_workflow_read_serves_its_steps_and_tasks_masked(client, db):
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "research", "runner_profile": "mock",
                 "input": {"prompt": f"read it; DB_PASSWORD={BARE}"}},
                {"step_id": "draft", "runner_profile": "mock", "input": {"prompt": "write it up"},
                 "depends_on": ["research"]},
            ]
        },
    )
    assert created.status_code == 201, created.text
    assert BARE not in created.text, "the workflow's create response echoed a step's credential"
    workflow_id = created.json()["workflow"]["workflow_id"]

    body = _get(client, f"/v1/workflows/{workflow_id}")
    assert BARE not in json.dumps(body)
    steps = {s["step_id"]: s for s in body["workflow"]["steps"]}
    assert steps["research"]["input_redaction_count"] == 1
    assert steps["draft"]["input_redaction_count"] == 0
    assert steps["draft"]["input"] == {"prompt": "write it up"}
    by_step = {t["step_id"]: t for t in body["tasks"]}
    assert by_step["research"]["input_redaction_count"] == 1


def test_a_workflow_step_is_masked_by_its_tasks_masker_so_the_metadatas_literal_is_masked_in_both(
    client, db
):
    """THE PR #229 REVIEW. The step copy was masked by a masker over the step's
    input alone, so a literal only the workflow's metadata named was masked in
    `tasks[0].input` and served in `workflow.steps[0].input` of the SAME
    response -- and in the create response and the list."""
    created = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "metadata": {"deploy_secret": LITERAL},
            "steps": [{"step_id": "s1", "runner_profile": "mock",
                       "input": {"prompt": f"use {LITERAL} to deploy"}}],
        },
    )
    assert created.status_code == 201, created.text
    assert LITERAL not in created.text, "the create response served the step's input in clear"
    workflow_id = created.json()["workflow"]["workflow_id"]

    body = _get(client, f"/v1/workflows/{workflow_id}")
    listing = _get(client, "/v1/workflows?limit=50")
    assert LITERAL not in json.dumps(body)
    assert LITERAL not in json.dumps(listing)

    (step,) = body["workflow"]["steps"]
    (task,) = body["tasks"]
    assert step["input"] == task["input"] == {"prompt": f"use {MASK} to deploy"}
    assert step["input_redaction_count"] == task["input_redaction_count"] == 1
    assert step["input_masked_by"] == "task"
    (listed,) = [w for w in listing["workflows"] if w["workflow_id"] == workflow_id]
    assert listed["steps"][0]["input"] == {"prompt": f"use {MASK} to deploy"}
    assert listed["steps"][0]["input_masked_by"] == "task"


def test_a_workflow_whose_step_tasks_were_not_read_serves_no_step_input(client, db):
    """Masked by a masker that never saw the metadata is the defect; with no
    step task read, the list serves the step's input as null and says why."""
    seed_tenant(db, "eng")
    moment = datetime.now(timezone.utc)
    db.docs["workflows/wf_orphan"] = {
        "workflow_id": "wf_orphan", "tenant_id": "eng", "created_at": moment,
        "updated_at": moment, "state": "QUEUED", "submitted_by": "alice@saga.xyz",
        "steps": [{"step_id": "s1", "runner_profile": "mock",
                   "input": {"prompt": f"use {LITERAL}"}, "depends_on": [],
                   "resource_class": "standard", "input_from": {}, "timeout_seconds": 600,
                   "task_id": "task_gone"}],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False,
    }
    listing = _get(client, "/v1/workflows?limit=50")

    (listed,) = [w for w in listing["workflows"] if w["workflow_id"] == "wf_orphan"]
    (step,) = listed["steps"]
    assert step["input"] is None and step["input_redaction_count"] is None
    assert step["input_masked_by"] == "not_read"


# ---------------------------------------------------------------------------
# The sweep: no GET route serves a planted secret inside an input or metadata
# ---------------------------------------------------------------------------

ROUTER_MODULES = (
    "platform", "tasks", "attempts", "workflows", "tenants", "admin", "accounts", "health",
    "checkpoints", "outcomes",
)
QUERY_FOR = {"/v1/outcomes": "?tz=UTC&span=7d"}


def _inputs_and_metadata(node: Any, out: list[Any]) -> list[Any]:
    """Every value under a key named `input` or `metadata`, anywhere in a body."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("input", "metadata"):
                out.append(value)
            _inputs_and_metadata(value, out)
    elif isinstance(node, list):
        for item in node:
            _inputs_and_metadata(item, out)
    return out


def test_no_get_route_serves_a_planted_secret_inside_an_input_or_metadata(client, db, objects):
    """Swept, not listed: a route added tomorrow is covered the day it exists,
    as long as its path parameters are among the ones filled in below -- a
    route with any other parameter is skipped, and counted, and the count is
    asserted, so a new one is noticed rather than silently left out (the PR
    #229 review found this sweep skipped every checkpoint route while its
    docstring said "covered").

    Since the PR #229 review the claim is the whole body, not only `input` and
    `metadata`: the secrets are planted in everything the task collected about
    itself too -- `last_error`, `result_summary`, an attempt's `error`, an
    event's `detail`, `repository_url`, the runner's stderr log and the
    `input.json` inside a checkpoint -- and no JSON body any GET route serves
    may hold one. The one route not held to it is the whole checkpoint archive,
    which is not JSON: the owner's decision of 2026-09-24 serves it byte for
    byte, and whether that stands under "masked everywhere" is the owner's
    open question (docs/agent-output.md).
    """
    from .test_checkpoint_content import put_checkpoint, tar_gz

    _task(
        db,
        input_doc={"prompt": f"use {OPENAI} with DB_PASSWORD={BARE}"},
        metadata={"note": OPENAI, "ci_token": BARE},
        task_id="task_mine",
    )
    collected = f"the agent printed {OPENAI} and {BARE}"
    db.docs["tasks/task_mine"].update(
        {
            "last_error": collected,
            "result_summary": {"runner": {"summary": collected}},
            "repository_url": f"https://x-access-token:{BARE}@github.com/o/r",
        }
    )
    db.docs["attempts/att_mine"] = {
        "attempt_id": "att_mine", "task_id": "task_mine", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_mine", "backend": "CLOUD_RUN_JOB",
        "created_at": db.docs["tasks/task_mine"]["created_at"], "exit_code": 1,
        "error": collected,
    }
    db.docs["tasks/task_mine/events/ev_mine"] = {
        "event_id": "ev_mine", "task_id": "task_mine", "tenant_id": "eng", "type": "failed",
        "at": db.docs["tasks/task_mine"]["created_at"], "attempt_id": "att_mine",
        "lease_id": "lease_mine", "generation": 1, "detail": {"error": collected},
    }
    base = "tenants/eng/tasks/task_mine/attempts/att_mine"
    objects.put(
        f"{base}/logs/stderr.log",
        json.dumps({"message": "child started", "argv": ["claude", "-p", collected]}) + "\n"
        + f"plain text: {collected}\n",
    )
    worker_input = {**db.docs["tasks/task_mine"]["input"], "task_id": "task_mine",
                    "attempt_id": "att_mine", "resumed_from_checkpoint": False}
    put_checkpoint(
        objects, task="task_mine", attempt="att_mine",
        archive=tar_gz([("input.json", "file", json.dumps(worker_input, indent=2)),
                        ("notes.md", "file", f"{collected}\n")]),
        file_count=2,
    )
    db.docs["workflows/wf_mine"] = {
        "workflow_id": "wf_mine", "tenant_id": "eng", "created_at": db.docs["tasks/task_mine"]["created_at"],
        "updated_at": db.docs["tasks/task_mine"]["created_at"], "state": "RUNNING",
        "submitted_by": "alice@saga.xyz",
        "steps": [{"step_id": "s1", "runner_profile": "mock",
                   "input": {"prompt": f"use {OPENAI} with DB_PASSWORD={BARE}"}, "depends_on": [],
                   "resource_class": "standard", "input_from": {}, "timeout_seconds": 600,
                   "task_id": "task_mine"}],
        "on_step_failure": "fail_workflow", "priority": 0, "cancel_requested": False,
    }
    db.docs["tasks/task_mine"]["workflow_id"] = "wf_mine"

    swept = 0
    carried = 0
    skipped: list[str] = []
    served_ok: set[str] = set()
    for name in ROUTER_MODULES:
        module = importlib.import_module(f"swarm_api.routes.{name}")
        for route in module.router.routes:
            if "GET" not in (getattr(route, "methods", set()) or set()):
                continue
            path = (
                route.path.replace("{task_id}", "task_mine")
                .replace("{workflow_id}", "wf_mine")
                .replace("{tenant_id}", "eng")
                .replace("{checkpoint_id}", "ckpt-00001")
                .replace("{path:path}", "input.json")
            )
            if "{" in path:
                skipped.append(route.path)
                continue
            path += QUERY_FOR.get(path, "")
            for user in ("alice", "root"):
                response = client.get(path, headers=auth_header(user))
                swept += 1
                if response.headers.get("content-type", "").startswith("application/json"):
                    try:
                        body = response.json()
                    except ValueError:
                        continue
                    if response.status_code == 200:
                        served_ok.add(route.path)
                    found = _inputs_and_metadata(body, [])
                    carried += len(found)
                    text = json.dumps(found, default=str)
                    assert OPENAI not in text and BARE not in text, (
                        f"{path} as {user} served a planted secret inside an input or metadata"
                    )
                    whole = json.dumps(body, default=str)
                    assert OPENAI not in whole and BARE not in whole, (
                        f"{path} as {user} served a planted secret: {whole[:2000]}"
                    )
    assert swept > 20, f"the sweep visited only {swept} route reads"
    assert carried > 0, "no route served an input or metadata at all, so the sweep proved nothing"
    assert skipped == [], f"these GET routes have a path parameter the sweep does not fill: {skipped}"
    # The planted fields were really served, so the whole-body check compared
    # against something: each of these reached a 200 with JSON.
    for template in (
        "/v1/tasks/{task_id}", "/v1/tasks/{task_id}/attempts", "/v1/tasks/{task_id}/events",
        "/v1/tasks/{task_id}/logs", "/v1/tasks/{task_id}/checkpoints/{checkpoint_id}/files/{path:path}",
        "/v1/attempts", "/v1/workflows/{workflow_id}",
    ):
        assert template in served_ok, f"{template} never answered 200, so the sweep proved nothing there"
