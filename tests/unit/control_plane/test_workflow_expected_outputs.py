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
what is in the stored task document once `POST /v1/workflows` returns, that it
is part of the write that creates the document, and that a caller cannot write
the key at all. The service is its only writer, the same reservation
`metadata.dispatch` has.

The key is spelled out here rather than imported, on purpose. It is a field in
a persisted Firestore document that a worker image of a different age reads, so
renaming it is a migration, not a refactor, and a test that followed the rename
would hide that. `tests/unit/control_plane/test_expected_outputs_seam.py` holds
the API and worker constants to each other and to what this route stores.
"""

from __future__ import annotations

import copy

import pytest

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


def test_the_names_are_in_the_write_that_creates_the_task(client, db, monkeypatch):
    """Part of the task document's creating write, never a second update.

    A worker can be dispatched the moment the upstream task document exists.
    Written by a later update, the key could arrive after that worker had read
    the task and told its agent nothing. The stored document looks the same
    either way, so this test watches the WRITES, not the result: every write
    the in-memory Firestore applies to the upstream task's document.
    """
    from . import fakes

    writes: list[tuple[str, str, dict]] = []
    real_set = fakes.FakeDocumentRef.set
    real_update = fakes.FakeDocumentRef.update

    def recording_set(self, data, merge=False):
        writes.append(("set_merge" if merge else "set", self.path, copy.deepcopy(data)))
        return real_set(self, data, merge=merge)

    def recording_update(self, data):
        writes.append(("update", self.path, copy.deepcopy(data)))
        return real_update(self, data)

    # A batch, a transaction and a direct write all land in these two methods,
    # so nothing that writes the task document can go unrecorded.
    monkeypatch.setattr(fakes.FakeDocumentRef, "set", recording_set)
    monkeypatch.setattr(fakes.FakeDocumentRef, "update", recording_update)

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

    upstream = [w for w in writes if w[1] == f"tasks/{task_ids['a']}"]
    assert upstream, "the recorder saw no write of the upstream task; it would prove nothing"
    op, _path, data = upstream[0]
    assert op == "set", upstream
    assert data["metadata"].get(STORED_KEY) == ["notes.md"], (
        "the document was created without the names; a worker dispatched on "
        "that version would tell its agent nothing"
    )
    later = [w for w in upstream[1:] if "metadata" in w[2] or w[0] != "update"]
    assert later == [], f"the task document was written again after creation: {later}"


# --------------------------------------------------------------------------
# a caller may not write the key
# --------------------------------------------------------------------------

#: The code the `metadata.dispatch` reservation answers with. The key is
#: reserved the same way (owner decision on #149, point (d), after #151).
RESERVED_CODE = "invalid_dispatch"


def _created(db) -> dict[str, list[str]]:
    return {"tasks": db.paths("tasks/"), "workflows": db.paths("workflows/")}


def _assert_refused(response) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == RESERVED_CODE
    assert body["detail"]["reserved_metadata_keys"] == [STORED_KEY]
    # Who sets it, and what to send instead.
    assert f"metadata.{STORED_KEY}" in body["message"]
    assert "input_from" in body["message"]
    assert "POST /v1/workflows" in body["message"]


@pytest.mark.parametrize("value", [["notes.md"], [], None, "notes.md"], ids=repr)
def test_a_plain_task_carrying_the_key_is_refused_and_creates_nothing(client, db, value):
    """On a plain task the worker would tell the agent "later steps of this
    workflow need these files" about a task no step stages from. Reserved, not
    validated, so the value does not matter."""
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={
            "runner_profile": "mock",
            "input": {"prompt": "hello"},
            "metadata": {"unit": "payments", STORED_KEY: value},
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_refused(response)


def test_a_batch_with_one_task_carrying_the_key_creates_none_of_them(client, db):
    response = client.post(
        "/v1/tasks/batch",
        headers=auth_header("alice"),
        json={
            "tasks": [
                {"runner_profile": "mock", "metadata": {"unit": "clean"}},
                {"runner_profile": "mock", "metadata": {STORED_KEY: ["notes.md"]}},
            ]
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_refused(response)


def test_a_workflows_own_metadata_carrying_the_key_is_refused_counted_and_creates_nothing(
    client, db, api_context
):
    """A workflow's own `metadata` is copied onto every step's task, so a value
    there would reach steps nothing stages from. Refused before a single task
    is built, and counted like every other rejected submission."""

    def rejected() -> float:
        return api_context.metrics.registry.get_sample_value(
            "swarm_api_tasks_rejected_total", {"reason": RESERVED_CODE}
        ) or 0.0

    before = rejected()
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "a", "runner_profile": "mock"},
                {
                    "step_id": "b",
                    "runner_profile": "mock",
                    "depends_on": ["a"],
                    "input_from": {"a": "notes.md"},
                },
            ],
            "metadata": {STORED_KEY: ["something-else.md"], "unit": "payments"},
        },
    )

    assert _created(db) == {"tasks": [], "workflows": []}
    _assert_refused(response)
    assert rejected() == before + 1


def test_the_rest_of_a_workflows_metadata_still_reaches_every_step(client, db):
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
        metadata={"unit": "payments"},
    )

    assert _metadata(db, task_ids["a"])["unit"] == "payments"
    assert _metadata(db, task_ids["b"])["unit"] == "payments"
    assert _metadata(db, task_ids["a"]).get(STORED_KEY) == ["notes.md"]


def test_a_plain_task_is_unaffected(client, db):
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hello"}},
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task"]["id"]

    assert STORED_KEY not in db.docs[f"tasks/{task_id}"]["metadata"]


def test_the_inversion_strips_names_and_leaves_out_an_empty_one():
    """The worker strips the filename it stages, so the stripped name is the
    one the upstream agent must write. An empty one names no file."""
    from swarm_api.expected_outputs import expected_outputs_by_step

    assert expected_outputs_by_step(
        [
            ("a", {}),
            ("b", {"a": " notes.md "}),
            ("c", {"a": "   "}),
            ("d", {"a": "notes.md", "b": "out.json"}),
        ]
    ) == {"a": ["notes.md"], "b": ["out.json"]}
