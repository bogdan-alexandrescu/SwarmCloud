"""A step that builds on another receives that step's owner answers, and never re-asks one.

#2836, reported 2026-10-30: dod-perf (task_8999d3acc63b40c69907) asked the
owner a question and the owner answered it ("virtualise the roadmap's rows");
dod-perf-fix (task_a9ba4380c9134d009f63), which `builds_on` dod-perf, was not
given the answer -- it is recorded on the ASKING task, `metadata.owner_answers`
-- so its agent asked the same question again and the owner was asked twice.

Now a `builds_on` step finds the answered questions of the step it builds on
in `owner-answers.json` beside its staged inputs, its prompt names the
decisions already made, and a question its agent asks again anyway is dropped
from its `questions.json` before anything parks on it or uploads it.

Run through the production worker over in-memory Firestore and a local object
store, as `test_owner_questions_reach_the_review.py` is.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

from agent_worker import owner_questions as owner_questions_mod
from agent_worker.errors import ExitCode
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt

WORKFLOW = "wf_builds_on_answers"
UPSTREAM = "task_dodperf"
FIX = "task_dodperffix"
FILE = "owner-answers.json"

ANSWERED = "Virtualise the roadmap's rows, or paginate them?"
OPEN = "Keep the old sort order?"
QUESTIONS = [
    {
        "question": ANSWERED,
        "options": [{"label": "virtualise", "description": "render only the visible rows"},
                    {"label": "paginate", "description": "fifty rows a page"}],
        "recommended": "virtualise",
        "context": "the roadmap renders 4,000 rows",
        "blocking": True,
    },
    {
        "question": OPEN,
        "options": [{"label": "yes", "description": "as today"},
                    {"label": "no", "description": "by date"}],
        "recommended": "yes",
        "context": "nobody asked",
    },
]


def _seed_upstream(db: Any, store: LocalObjectStore) -> None:
    """dod-perf, SUCCEEDED, that asked both questions; the owner answered the first."""
    key = f"tenants/{TENANT}/tasks/{UPSTREAM}/attempts/att_u/artifacts/questions.json"
    data = json.dumps(QUESTIONS).encode("utf-8")
    store.upload_bytes(key, data)
    db.seed(f"tasks/{UPSTREAM}", {
        "id": UPSTREAM, "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "workflow_id": WORKFLOW, "step_id": "dod-perf",
        "result_summary": {"questions": 2, "artifacts": [
            {"name": "questions.json", "uri": store.uri(key), "bytes": len(data)}]},
        "metadata": {"owner_answers": [
            {"question_index": 0, "question": ANSWERED,
             "choice": "virtualise", "text": None, "by": "owner@example.com"},
        ]},
    })


def _run_fix(
    db: Any, worker_factory: Any, *, builds_on: str | None, asked: list[dict[str, Any]] | None
) -> int:
    task_input: dict[str, Any] = {"prompt": "fix the roadmap", "steps": 1, "sleep_seconds": 0.01}
    if asked is not None:
        task_input.update(artifact_name="questions.json", artifact_text=json.dumps(asked))
    seed_attempt(db, task_id=FIX, attempt_id="att_f", lease_id="lease_f", task_input=task_input)
    doc = db.doc(f"tasks/{FIX}")
    doc["workflow_id"] = WORKFLOW
    doc["step_id"] = "dod-perf-fix"
    doc["depends_on"] = [UPSTREAM]
    if builds_on is not None:
        doc["metadata"] = {"dispatch": {"builds_on": builds_on}}
    worker, _config, _exporter = worker_factory(
        task_id=FIX, attempt_id="att_f", lease_id="lease_f")
    return worker.run()


def _work_file(store: LocalObjectStore, name: str) -> bytes | None:
    """`name` in the fix step's last checkpoint of `work/`, None when it is not there."""
    keys = sorted(k for k in store.list_keys(f"tenants/{TENANT}/tasks/{FIX}/attempts/att_f/")
                  if k.endswith("/archive.tar.gz"))
    assert keys, "the fix step wrote no checkpoint"
    with tarfile.open(fileobj=io.BytesIO(store.download_bytes(keys[-1])), mode="r:gz") as archive:
        if name not in archive.getnames():
            return None
        member = archive.extractfile(name)
        assert member is not None
        return member.read()


def _uploaded_questions(store: LocalObjectStore) -> list[dict[str, Any]] | None:
    """The `questions.json` the fix step's attempt uploaded, None when it uploaded none."""
    keys = [k for k in store.list_keys(f"tenants/{TENANT}/tasks/{FIX}/attempts/att_f/")
            if k.endswith("/artifacts/questions.json")]
    return json.loads(store.download_bytes(keys[0])) if keys else None


def test_a_builds_on_step_is_given_the_answers_and_the_prompt_names_them(
    db, store, worker_factory, runner_inputs
):
    _seed_upstream(db, store)

    assert _run_fix(db, worker_factory, builds_on=UPSTREAM, asked=None) == ExitCode.OK
    assert db.doc(f"tasks/{FIX}")["state"] == TaskState.SUCCEEDED.value

    raw = _work_file(store, FILE)
    # MUTATION: stage nothing for a builds_on step (the #2836 bug) and this is None.
    assert raw is not None, f"the builds_on step was not given {FILE}"
    rows = json.loads(raw)
    # MUTATION: stage the unanswered question too and this is two rows.
    assert rows == [{
        "task_id": UPSTREAM, "step_id": "dod-perf", "question_index": 0,
        "question": ANSWERED, "answer": {"choice": "virtualise", "text": None},
    }]

    prompt = runner_inputs[-1]["prompt"]
    assert prompt.startswith("fix the roadmap")
    # MUTATION: write the file and say nothing, and the agent never reads it.
    assert FILE in prompt
    assert "do not ask the owner any of these questions again" in prompt
    assert ANSWERED in prompt and "virtualise" in prompt
    assert OPEN not in prompt


def test_a_question_asked_again_is_dropped_as_already_answered(
    db, store, worker_factory, runner_inputs
):
    _seed_upstream(db, store)
    # The agent asks the answered question again (spacing and case changed)
    # beside a new one of its own.
    again = dict(QUESTIONS[0], question="  virtualise the ROADMAP'S rows,  or paginate them? ")
    new = dict(QUESTIONS[1], question="Cache the rendered rows?")

    assert _run_fix(db, worker_factory, builds_on=UPSTREAM, asked=[again, new]) == ExitCode.OK
    doc = db.doc(f"tasks/{FIX}")
    assert doc["state"] == TaskState.SUCCEEDED.value
    # MUTATION: drop no question and the summary counts two.
    assert doc["result_summary"]["questions"] == 1
    uploaded = _uploaded_questions(store)
    assert uploaded is not None
    assert [q["question"] for q in uploaded] == ["Cache the rendered rows?"]


def test_a_step_without_builds_on_is_unchanged(db, store, worker_factory, runner_inputs):
    _seed_upstream(db, store)

    assert _run_fix(db, worker_factory, builds_on=None, asked=[QUESTIONS[0]]) == ExitCode.OK
    doc = db.doc(f"tasks/{FIX}")
    assert doc["state"] == TaskState.SUCCEEDED.value
    # MUTATION: stage for every step of the workflow and these fail.
    assert _work_file(store, FILE) is None
    assert FILE not in runner_inputs[-1]["prompt"]
    # MUTATION: drop by the workflow's answers rather than builds_on's and this is 0.
    assert doc["result_summary"]["questions"] == 1


def test_drop_answered_removes_the_wait_when_no_blocking_question_is_left(tmp_path: Path):
    (tmp_path / "questions.json").write_text(json.dumps([QUESTIONS[0], QUESTIONS[1]]))
    (tmp_path / "await-answers").write_text("")

    dropped = owner_questions_mod.drop_answered(tmp_path, [ANSWERED])

    assert dropped == [ANSWERED]
    assert json.loads((tmp_path / "questions.json").read_text()) == [QUESTIONS[1]]
    # MUTATION: keep the marker and the step parks on a decision already made.
    assert not (tmp_path / "await-answers").exists()


def test_drop_answered_removes_the_file_when_every_question_was_answered(tmp_path: Path):
    (tmp_path / "questions.json").write_text(json.dumps([QUESTIONS[0]]))

    assert owner_questions_mod.drop_answered(tmp_path, [ANSWERED]) == [ANSWERED]
    assert not (tmp_path / "questions.json").exists()


def test_drop_answered_leaves_an_invalid_file_for_the_summary(tmp_path: Path):
    (tmp_path / "questions.json").write_text("not json")

    assert owner_questions_mod.drop_answered(tmp_path, [ANSWERED]) == []
    assert (tmp_path / "questions.json").read_text() == "not json"
