"""An upstream step's task records the files its dependants will stage (#149).

Measured on 2026-09-25, workflow `wf_73946ff4a32a4f99b3a4`: eight claude-code
scan steps SUCCEEDED and uploaded only their own logs and transcript, because
their prompts said "write it to scan-01.md" and the agent wrote it into its
working directory. Only files in `$SWARM_ARTIFACTS_DIR` are uploaded, so all four
merge steps then FAILED with "upstream task ... did not produce an artifact named
'scan-01.md'", and seventeen downstream steps were cancelled.

The owner chose option (b): at submission, the API writes onto each UPSTREAM
step's task the artifact names its dependants' `input_from` expect, so the
worker can tell that agent where they must go. These tests hold the API half:
what is in the stored task document once `POST /v1/workflows` returns.

The key is spelled out here rather than imported, on purpose. It is a field in
a persisted Firestore document that a worker image of a different age reads, so
renaming it is a migration, not a refactor, and a test that followed the rename
would hide that. `tests/unit/worker/test_expected_outputs_seam.py` holds the API
and worker constants to each other and to what this route stores.
"""

from __future__ import annotations

from .conftest import auth_header

STORED_KEY = "expected_outputs"


def _submit(client, steps, **extra):
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={"steps": steps, **extra},
    )
    assert response.status_code == 201, response.text
    return {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}


def _metadata(db, task_id):
    return db.docs[f"tasks/{task_id}"]["metadata"]


def test_the_upstream_step_records_the_filename_its_dependant_stages(client, db):
    task_ids = _submit(
        client,
        [
            {"step_id": "a", "runner_profile": "mock"},
            {
                "step_id": "b",
                "runner_profile": "mock",
                "depends_on": ["a"],
                "input_from": {"a": "notes.md"},
            },
        ],
    )

    assert _metadata(db, task_ids["a"]).get(STORED_KEY) == ["notes.md"]
    # The dependant stages the file; it produces nothing anybody declared.
    assert STORED_KEY not in _metadata(db, task_ids["b"])
    # The declaration the dependant stages from is unchanged by this.
    assert _metadata(db, task_ids["b"])["input_from"] == {task_ids["a"]: "notes.md"}


def test_a_step_feeding_two_dependants_records_both_names_sorted_once(client, db):
    task_ids = _submit(
        client,
        [
            {"step_id": "scan", "runner_profile": "mock"},
            {
                "step_id": "report",
                "runner_profile": "mock",
                "depends_on": ["scan"],
                "input_from": {"scan": "notes.md"},
            },
            {
                "step_id": "index",
                "runner_profile": "mock",
                "depends_on": ["scan"],
                "input_from": {"scan": "data.json"},
            },
            {
                # A third dependant asking for a name another already asked for:
                # the agent is told about it once.
                "step_id": "audit",
                "runner_profile": "mock",
                "depends_on": ["scan"],
                "input_from": {"scan": "notes.md"},
            },
        ],
    )

    assert _metadata(db, task_ids["scan"]).get(STORED_KEY) == ["data.json", "notes.md"]
    for leaf in ("report", "index", "audit"):
        assert STORED_KEY not in _metadata(db, task_ids[leaf]), leaf


def test_a_join_records_each_parents_own_name_on_that_parent_only(client, db):
    """The shape that failed on 2026-09-25: one merge step, two scan parents,
    a distinct filename from each. Each scan is told only its own file."""
    task_ids = _submit(
        client,
        [
            {"step_id": "scan-01", "runner_profile": "mock"},
            {"step_id": "scan-02", "runner_profile": "mock"},
            {
                "step_id": "merge-1",
                "runner_profile": "mock",
                "depends_on": ["scan-01", "scan-02"],
                "input_from": {"scan-01": "scan-01.md", "scan-02": "scan-02.md"},
            },
        ],
    )

    assert _metadata(db, task_ids["scan-01"]).get(STORED_KEY) == ["scan-01.md"]
    assert _metadata(db, task_ids["scan-02"]).get(STORED_KEY) == ["scan-02.md"]
    assert STORED_KEY not in _metadata(db, task_ids["merge-1"])


def test_a_dependency_that_stages_nothing_records_nothing(client, db):
    """`depends_on` alone is ordering, not data: no file is expected of it."""
    task_ids = _submit(
        client,
        [
            {"step_id": "a", "runner_profile": "mock"},
            {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
        ],
    )

    assert STORED_KEY not in _metadata(db, task_ids["a"])
    assert STORED_KEY not in _metadata(db, task_ids["b"])


def test_the_workflow_graph_decides_it_not_the_workflows_own_metadata(client, db):
    """A workflow's own `metadata` is copied onto every step's task. A value
    under this key there would otherwise reach a step no dependant stages from
    and have its agent told to produce a file nobody reads."""
    task_ids = _submit(
        client,
        [
            {"step_id": "a", "runner_profile": "mock"},
            {
                "step_id": "b",
                "runner_profile": "mock",
                "depends_on": ["a"],
                "input_from": {"a": "notes.md"},
            },
        ],
        metadata={STORED_KEY: ["something-else.md"], "unit": "payments"},
    )

    assert _metadata(db, task_ids["a"]).get(STORED_KEY) == ["notes.md"]
    assert STORED_KEY not in _metadata(db, task_ids["b"])
    # The rest of the caller's metadata is untouched.
    assert _metadata(db, task_ids["a"])["unit"] == "payments"
    assert _metadata(db, task_ids["b"])["unit"] == "payments"


def test_a_plain_task_is_unaffected(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hello"}},
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]

    assert STORED_KEY not in db.docs[f"tasks/{task_id}"]["metadata"]
