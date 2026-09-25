"""`input_from` that the worker would refuse is refused at submission instead.

THE MEASUREMENT BEHIND THIS FILE (#64). On 2026-09-25 workflow
`wf_1e547922a981411991e2` (tenant eng, 30 steps, `on_step_failure=continue`)
was ACCEPTED. Its steps merge-1..merge-4 each declared

    input_from = {"scan-A": "notes.md", "scan-B": "notes.md"}

and rollup-b did the same with three parents. The eight scan steps and the
plan step ran and SUCCEEDED -- about twenty minutes of claude-code work -- and
then all four merge steps FAILED at 03:48-03:49Z on the worker's
`_assert_distinct_destinations` ("... all stage 'notes.md' into this workspace;
one would silently overwrite the other ..."), and seventeen downstream steps
were CANCELLED. The worker's own docstring calls this "a submission-time
mistake that a person can fix". The API never checked it, so the mistake cost
the whole upstream run before anybody heard about it.

WHAT IS ASSERTED, AND WHY IT IS A PROPERTY RATHER THAN A SHAPE. A refusal
proves nothing on its own: a handler that wrote the tasks and THEN raised would
return the same 422 and leave the upstream steps enqueued to burn exactly the
compute this exists to save. So every refusal below is checked for three things
together: the status and code, the step/parents/filename in the message (the
only part a person can act on), and that NOTHING was created -- no task
document, no workflow document, no scheduler wake.

THE STATUS IS 422 `invalid_dag`, THE SAME AS EVERY SIBLING `validate_dag`
REFUSAL: a cycle, a dangling dependency, an `input_from` source that is not a
`depends_on`. One code, one status, so a caller that branches on either reads
one family. The owner decided this on #64 on 2026-09-25. The 400 in the
original brief was a mistake in the brief: the New Workflow screen
(`apps/swarm-ui/src/SubmitWorkflow.tsx`) maps 422 to "invalid", and a 400
would have been headed there as a failed API. Section 6 holds every
`invalid_dag` refusal to that one status.

A WORKFLOW-LEVEL `metadata.input_from` IS THE SAME DECLARATION BY ANOTHER
DOOR. `submit_workflow` copies the workflow's `metadata` onto every step's
task and replaces `input_from` only on a step that declares its own. Every
root step declares none (it has no `depends_on` for one to name), so a
workflow-level `metadata.input_from` reaches at least one task verbatim, keyed
by upstream TASK id, and the worker refuses it there exactly as it refused
merge-1. Section 5 holds that door to the same rule.

THE LAST SECTION BINDS THE RESTATEMENT TO THE WORKER. swarm-api cannot import
`agent_worker` (images/swarm-api/Dockerfile ships apps/common and apps/swarm-api
and nothing else), so the API states the rule again. A restated rule nothing
compares is a copy waiting to drift, so the worker's own functions are run
here against the same inputs: the collision the API refuses is one the worker
really refuses, and every filename the API accepts is one the worker can stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as worker_inputs
from agent_worker.errors import InputUnavailable

from swarm_api.errors import ValidationFailed
from swarm_api.validation import StepSpec, validate_dag

from .conftest import auth_header


def _post(client, steps: list[dict[str, Any]], *, metadata: dict[str, Any] | None = None):
    body: dict[str, Any] = {"steps": steps, "on_step_failure": "continue"}
    if metadata is not None:
        body["metadata"] = metadata
    return client.post("/v1/workflows", headers=auth_header("alice"), json=body)


def _created(db) -> list[str]:
    """Every document a workflow submission writes: its tasks and itself."""
    return sorted(key for key in db.docs if key.startswith(("tasks/", "workflows/")))


def _root(step_id: str) -> dict[str, Any]:
    return {"step_id": step_id, "runner_profile": "mock", "input": {"prompt": f"run {step_id}"}}


def _join(step_id: str, input_from: dict[str, str]) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "runner_profile": "mock",
        "input": {"prompt": f"join {step_id}"},
        "depends_on": list(input_from),
        "input_from": dict(input_from),
    }


def _assert_refused_before_anything_was_created(response, db, api_context) -> dict[str, Any]:
    # 422, the status of every invalid_dag refusal: see the header.
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_dag", body
    assert _created(db) == [], (
        "the submission was refused, but it had already written documents: "
        f"{_created(db)}. The upstream steps are then enqueued and run anyway."
    )
    assert api_context.waker.calls == [], (
        f"the scheduler was woken for a refused submission: {api_context.waker.calls}"
    )
    return body


# --------------------------------------------------------------------------
# 1. Two or more parents staging one filename into one step
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("join", "parents"),
    [
        # merge-1..merge-4 in the measured workflow
        ("merge-1", ("scan-A", "scan-B")),
        # rollup-b in the same workflow: three parents
        ("rollup-b", ("scan-C", "scan-D", "scan-E")),
    ],
)
def test_parents_staging_one_filename_are_refused_before_anything_is_created(
    client, db, api_context, join, parents
):
    steps = [_root(p) for p in parents] + [_join(join, {p: "notes.md" for p in parents})]

    body = _assert_refused_before_anything_was_created(_post(client, steps), db, api_context)

    message = body["message"]
    assert join in message, message
    for parent in parents:
        assert parent in message, f"{parent} is not named in: {message}"
    assert "notes.md" in message, message
    # How to fix it has to be in the refusal, not left for the reader to infer.
    assert "distinct" in message.lower(), message
    assert body["detail"]["step_id"] == join
    assert body["detail"]["filename"] == "notes.md"
    assert sorted(body["detail"]["colliding_upstream_steps"]) == sorted(parents)


def test_names_the_worker_would_strip_to_one_filename_are_refused(client, db, api_context):
    """The worker strips each filename (`declared_inputs`) before it compares
    them, so the API has to compare what the worker compares. `" notes.md"` and
    `"notes.md"` land on the same file."""
    steps = [
        _root("scan-A"),
        _root("scan-B"),
        _join("merge-1", {"scan-A": "notes.md", "scan-B": " notes.md "}),
    ]
    body = _assert_refused_before_anything_was_created(_post(client, steps), db, api_context)
    assert "merge-1" in body["message"]


def test_one_bad_step_refuses_the_whole_workflow(client, db, api_context):
    """A 30-step workflow with one bad join creates none of its 30 tasks: a
    partial workflow would run the upstream steps for a join that cannot."""
    steps = [_root(f"scan-{n}") for n in range(8)]
    steps.append(_join("fine", {"scan-0": "a.md", "scan-1": "b.md"}))
    steps.append(_join("merge-3", {"scan-2": "notes.md", "scan-3": "notes.md"}))
    body = _assert_refused_before_anything_was_created(_post(client, steps), db, api_context)
    assert body["detail"]["step_id"] == "merge-3"


# --------------------------------------------------------------------------
# 2. What must still be accepted
# --------------------------------------------------------------------------

def test_distinct_filenames_per_parent_are_accepted_and_reach_the_task(client, db):
    steps = [
        _root("scan-A"),
        _root("scan-B"),
        _join("merge-1", {"scan-A": "scan-A-notes.md", "scan-B": "scan-B-notes.md"}),
    ]
    response = _post(client, steps)
    assert response.status_code == 201, response.text

    by_step = {s["step_id"]: s for s in response.json()["workflow"]["steps"]}
    merge = db.docs[f"tasks/{by_step['merge-1']['task_id']}"]
    assert merge["metadata"]["input_from"] == {
        by_step["scan-A"]["task_id"]: "scan-A-notes.md",
        by_step["scan-B"]["task_id"]: "scan-B-notes.md",
    }


def test_one_filename_in_two_directories_is_two_files(client):
    steps = [
        _root("scan-A"),
        _root("scan-B"),
        _join("merge-1", {"scan-A": "a/notes.md", "scan-B": "b/notes.md"}),
    ]
    assert _post(client, steps).status_code == 201


def test_the_same_filename_in_two_different_steps_is_not_a_collision(client):
    """Each step has its OWN workspace. Only two parents of ONE step collide, so
    merge-1 and merge-2 may each stage a `notes.md`."""
    steps = [
        _root("scan-A"),
        _root("scan-B"),
        _join("merge-1", {"scan-A": "notes.md"}),
        _join("merge-2", {"scan-B": "notes.md"}),
    ]
    assert _post(client, steps).status_code == 201


# --------------------------------------------------------------------------
# 3. A filename that is not a relative path inside the workspace
# --------------------------------------------------------------------------

UNSAFE_FILENAMES = [
    "/etc/passwd",
    "/notes.md",
    "../notes.md",
    "a/../../escape.txt",
    "reports/../notes.md",
    "..",
    ".",
    "./notes.md",
    "reports/./notes.md",
    "reports//notes.md",
    "reports/",
    "",
    "   ",
    "\\evil",
    "a\x00b",
]


@pytest.mark.parametrize("filename", UNSAFE_FILENAMES)
def test_an_absolute_or_traversal_filename_is_refused_before_anything_is_created(
    client, db, api_context, filename
):
    steps = [_root("analyse"), _join("fix", {"analyse": filename})]

    body = _assert_refused_before_anything_was_created(_post(client, steps), db, api_context)

    assert "'fix'" in body["message"], body["message"]
    assert "'analyse'" in body["message"], body["message"]
    assert body["detail"]["step_id"] == "fix"
    assert body["detail"]["input_from"] == "analyse"


# --------------------------------------------------------------------------
# 4. The API's rule against the worker's own
# --------------------------------------------------------------------------

def _api_accepts(filename: str) -> bool:
    steps = [
        StepSpec(step_id="a", depends_on=()),
        StepSpec(step_id="b", depends_on=("a",), input_from={"a": filename}),
    ]
    try:
        validate_dag(steps, max_steps=50)
    except ValidationFailed:
        return False
    return True


def test_the_collision_the_api_refuses_is_one_the_worker_really_refuses():
    """Without this the API check could be refusing a workflow that would have
    run. The worker's declaration parser is the one that failed merge-1..4."""
    with pytest.raises(InputUnavailable) as raised:
        worker_inputs.declared_inputs(
            {"input_from": {"task_a": "notes.md", "task_b": "notes.md"}}
        )
    assert "notes.md" in str(raised.value)
    # And the fix the API's message recommends is one the worker accepts.
    assert len(
        worker_inputs.declared_inputs(
            {"input_from": {"task_a": "scan-A-notes.md", "task_b": "scan-B-notes.md"}}
        )
    ) == 2


def test_every_filename_the_api_accepts_the_worker_can_stage(tmp_path: Path):
    """API-accepts implies worker-accepts, run through the worker's own parse
    (`declared_inputs`, which strips) and `destination_for`. The reserved names
    are deliberately out of scope: the API does not restate that list, so a
    reserved name is still refused by the worker, at run time.

    The API may be STRICTER than the worker -- `./notes.md` or `reports/` pass
    `destination_for` but can never match an artifact, because the uploader
    names each artifact by its path relative to the artifacts directory -- and
    that direction costs nothing. The direction that cost a run is the one
    asserted: accepted here, refused there.
    """
    safe = ["notes.md", "reports/notes.md", "a/b/c.txt", ".hidden", "notes..md", " notes.md "]
    corpus = safe + UNSAFE_FILENAMES
    accepted: list[str] = []
    refused: list[str] = []
    drift: list[tuple[str, str]] = []
    for filename in corpus:
        if not _api_accepts(filename):
            refused.append(filename)
            continue
        accepted.append(filename)
        try:
            declared = worker_inputs.declared_inputs({"input_from": {"task_a": filename}})
            worker_inputs.destination_for(
                tmp_path, declared[0].filename, reserved=frozenset()
            )
        except InputUnavailable as exc:
            drift.append((filename, str(exc)))

    assert drift == [], (
        "the API accepts filenames the worker refuses at run time, after the "
        f"upstream step has already run: {drift}"
    )
    # A loop that visited nothing proves nothing: every name was classified,
    # and both outcomes occurred -- exactly the safe names were accepted.
    assert len(accepted) + len(refused) == len(corpus)
    assert sorted(accepted) == sorted(safe), (accepted, refused)


# --------------------------------------------------------------------------
# 5. A workflow-level metadata.input_from: the same declaration, another door
# --------------------------------------------------------------------------
#
# `submit_workflow` builds each step's task with `{**spec.metadata, ...}` and
# replaces `input_from` only on a step that declares its own. A root step
# cannot declare one (it has no `depends_on` for it to name), so whatever the
# workflow's own `metadata.input_from` says reaches every root step's task,
# keyed by upstream TASK id, exactly as the worker reads it. The worker refuses
# a colliding or unsafe one there, after the other roots have been enqueued.

MALFORMED_DECLARATIONS = [
    ["task_earlier"],
    [],
    "task_earlier:notes.md",
    3,
    {"": "notes.md"},
    {"   ": "notes.md"},
    {"task_earlier": 3},
    {"task_earlier": None},
    {"task_earlier": ["notes.md"]},
]


def _roots_and_join() -> list[dict[str, Any]]:
    """Two roots, which inherit the workflow's metadata, and a join, which has its own."""
    return [
        _root("scan-A"),
        _root("scan-B"),
        _join("merge-1", {"scan-A": "scan-A-notes.md", "scan-B": "scan-B-notes.md"}),
    ]


def test_a_workflow_level_input_from_that_collides_is_refused_before_anything_is_created(
    client, db, api_context
):
    metadata = {"input_from": {"task_earlier_a": "notes.md", "task_earlier_b": "notes.md"}}

    body = _assert_refused_before_anything_was_created(
        _post(client, _roots_and_join(), metadata=metadata), db, api_context
    )

    message = body["message"]
    assert "metadata.input_from" in message, message
    for upstream in ("task_earlier_a", "task_earlier_b"):
        assert upstream in message, f"{upstream} is not named in: {message}"
    assert "notes.md" in message, message
    assert "distinct" in message.lower(), message
    assert body["detail"]["filename"] == "notes.md"
    assert sorted(body["detail"]["colliding_upstream_tasks"]) == [
        "task_earlier_a",
        "task_earlier_b",
    ]


@pytest.mark.parametrize("filename", UNSAFE_FILENAMES)
def test_a_workflow_level_input_from_with_an_unsafe_filename_is_refused_before_anything_is_created(
    client, db, api_context, filename
):
    metadata = {"input_from": {"task_earlier": filename}}

    body = _assert_refused_before_anything_was_created(
        _post(client, _roots_and_join(), metadata=metadata), db, api_context
    )

    assert "metadata.input_from" in body["message"], body["message"]
    assert "'task_earlier'" in body["message"], body["message"]
    assert body["detail"]["input_from"] == "task_earlier"


@pytest.mark.parametrize("declaration", MALFORMED_DECLARATIONS, ids=repr)
def test_a_malformed_workflow_level_input_from_is_refused_before_anything_is_created(
    client, db, api_context, declaration
):
    body = _assert_refused_before_anything_was_created(
        _post(client, _roots_and_join(), metadata={"input_from": declaration}), db, api_context
    )
    assert "metadata.input_from" in body["message"], body["message"]


@pytest.mark.parametrize("declaration", MALFORMED_DECLARATIONS, ids=repr)
def test_every_malformed_declaration_the_api_refuses_the_worker_refuses_too(declaration):
    """The API must not refuse a declaration the worker would have staged."""
    with pytest.raises(InputUnavailable):
        worker_inputs.declared_inputs({"input_from": declaration})


def test_a_well_formed_workflow_level_input_from_still_reaches_the_root_steps(client, db):
    """Refusing the bad shapes must not remove the good one: a root step still
    carries the workflow's declaration, as it did before #64."""
    metadata = {"input_from": {"task_earlier": "notes.md"}, "unit": "payments"}

    response = _post(client, _roots_and_join(), metadata=metadata)

    assert response.status_code == 201, response.text
    by_step = {s["step_id"]: s for s in response.json()["workflow"]["steps"]}
    for root in ("scan-A", "scan-B"):
        task = db.docs[f"tasks/{by_step[root]['task_id']}"]
        assert task["metadata"]["input_from"] == {"task_earlier": "notes.md"}
        assert task["metadata"]["unit"] == "payments"


@pytest.mark.parametrize("absent", [None, {}])
def test_an_empty_workflow_level_input_from_is_accepted(client, absent):
    """`declared_inputs` reads None and {} as "stages nothing", so the API does too."""
    response = _post(client, _roots_and_join(), metadata={"input_from": absent})
    assert response.status_code == 201, response.text


def test_the_metadata_key_the_api_checks_is_the_one_the_worker_reads():
    """Two spellings of one key in two images, held equal here: a check on a key
    the worker does not read would pass everything."""
    from swarm_api import validation

    assert validation.INPUT_FROM_METADATA_KEY == worker_inputs.METADATA_KEY


# --------------------------------------------------------------------------
# 6. One error code, one status
# --------------------------------------------------------------------------
#
# `invalid_dag` is one code, and it answers one status. The New Workflow
# screen reads the status (`KIND_BY_STATUS` in SubmitWorkflow.tsx), a caller
# reads the code, and both must land on "the request was invalid" for every
# refusal in the family. So the input_from refusals are held against their
# siblings, through the real HTTP surface, rather than against a number
# restated here.

def test_every_invalid_dag_refusal_answers_one_status(client):
    submissions: dict[str, tuple[list[dict[str, Any]], dict[str, Any] | None]] = {
        # the siblings, whose 422 predates #64
        "cycle": (
            [
                {**_root("build"), "depends_on": ["test"]},
                {**_root("test"), "depends_on": ["build"]},
            ],
            None,
        ),
        "dangling dependency": ([{**_root("build"), "depends_on": ["nowhere"]}], None),
        "input_from source that is not a depends_on": (
            [_root("scan-A"), {**_root("merge-1"), "input_from": {"scan-A": "notes.md"}}],
            None,
        ),
        # the refusals #64 added
        "two parents staging one filename": (
            [
                _root("scan-A"),
                _root("scan-B"),
                _join("merge-1", {"scan-A": "notes.md", "scan-B": "notes.md"}),
            ],
            None,
        ),
        "a traversing filename": ([_root("analyse"), _join("fix", {"analyse": "../x.md"})], None),
        "a workflow-level collision": (
            _roots_and_join(),
            {"input_from": {"task_earlier_a": "notes.md", "task_earlier_b": "notes.md"}},
        ),
        "a malformed workflow-level declaration": (
            _roots_and_join(),
            {"input_from": ["task_earlier"]},
        ),
    }

    answers: dict[str, tuple[int, Any]] = {}
    for name, (steps, metadata) in submissions.items():
        response = _post(client, steps, metadata=metadata)
        answers[name] = (response.status_code, response.json().get("code"))

    # Every submission was sent and answered, not only the first.
    assert len(answers) == len(submissions) == 7
    assert answers == {name: (422, "invalid_dag") for name in submissions}, answers
