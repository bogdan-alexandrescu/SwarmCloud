"""`owner-questions.json`: the workflow's owner questions, staged into its review.

WHY (#2070, owner decision 2026-10-23, option (c)). `input_from` stages ONE
file per parent -- the frozen contract's `dict[str, str]` -- and a lane's
review stages the implementer's `swarm-work.patch`. So a question an earlier
step asked in its `questions.json` reached neither the review nor the merge:
on 2026-10-23 three lanes (QAFIX6-WORKPAGES, IMAGE-TOOL-RESCAN,
CAPTCHA-BYPASS) reached their merge step with questions open, and each review
wrote "the implementer left no questions.json". Adding a second file per
parent would be a contract change; this needs none.

WHICH STEP. A REVIEW: a step a dependant stages `verdict.json` from, which is
exactly what `metadata.expected_outputs` records (`expected_outputs`). Any
other step is given nothing and reads nothing.

WHICH QUESTIONS. Every SUCCEEDED step of the same workflow that this one
depends on, directly or through other steps of the workflow (`depends_on`,
walked through this tenant's tasks of this workflow only, at most
`MAX_ANCESTORS` documents), so a re-review that depends only on the fix still
sees the implement step's. Each step's questions are the ones its worker
validated and counted (`result_summary.questions`), read back from the
upload in its successful attempt's manifest and validated again
(`questions.validate`); a count the file does not hold is refused. A step
that counted none but parked on a blocking question (`metadata.question_wait`)
offers that wait's questions. The answers are `metadata.owner_answers`, by
question index, which swarm-api's answer route writes (`swarm_task_answer`).

THE READS ARE THE ONES STAGING ALREADY MAKES: `inputs.fetch_upstream_task`
(Firestore, or `GET state/upstream`, which serves a workflow ancestor along
`depends_on` for this reason) and the object store under this tenant's
prefix (`inputs.artifact_key`). No new credential reaches the container.

A COUNTED FILE THAT CANNOT BE READ FAILS THE ATTEMPT, as a declared input
does (`inputs`): a review told "no questions" when there were some is the
failure this module exists to end.

WHAT THE REVIEW DOES WITH THEM is said once, in `guidance`, appended to its
prompt: copy each UNANSWERED question that should hold the merge into
`verdict.needs_owner`, list the other unanswered ones in
`verdict.open_questions`. `merge.run_merge` holds only for the first, and
re-reads the answers before it does.

A workflow, or a review, with no questions behaves exactly as before: no
file, no prompt text.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from swarm_common.config import Settings
from swarm_common.states import TaskState

from . import expected_outputs as expected_mod
from . import inputs as inputs_mod
from . import questions as questions_mod
from .errors import InputUnavailable
from .objectstore import ObjectStore

#: The file, in the work directory beside the staged inputs.
NAME = "owner-questions.json"
#: What a review's dependants stage from it.
REVIEW_OUTPUT = "verdict.json"
#: The verdict keys the guidance names; `verdict.read_verdict` reads both.
NEEDS_OWNER_KEY = "needs_owner"
OPEN_QUESTIONS_KEY = "open_questions"
#: The most task documents the ancestor walk reads: every ancestor is a
#: distinct step of one workflow, so the most steps a workflow may have, read
#: from the FROZEN `Settings` default as `lifecycle._MAX_WORKFLOW_STEPS` is.
#: swarm-api's `state/upstream` walk has the same bound
#: (`attemptstate.WORKFLOW_ANCESTORS_MAX`).
MAX_ANCESTORS: int = next(
    f.default for f in dataclasses.fields(Settings) if f.name == "max_workflow_steps"
)

Fetch = Callable[[str], dict[str, Any]]


def is_review(metadata: Any) -> bool:
    """Whether a dependant stages `verdict.json` from this step."""
    return REVIEW_OUTPUT in expected_mod.declared_outputs(metadata).names


def _ids(value: Any) -> list[str]:
    return sorted({v.strip() for v in value or () if isinstance(v, str) and v.strip()})


def workflow_ancestors(task: Mapping[str, Any], fetch: Fetch) -> list[tuple[str, dict[str, Any]]]:
    """`(task id, document)` of every step of this workflow this one depends on.

    Breadth first from `depends_on`, sorted at each level, so the order is
    the same on every run. A step of another workflow is neither kept nor
    walked through. `fetch` is the tenant-gated upstream read, so another
    tenant's task is its refusal: raised for a parent this step declares,
    and skipped further back.
    """
    workflow_id = task.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    level = _ids(task.get("depends_on"))
    direct = set(level)
    while level and len(seen) < MAX_ANCESTORS:
        following: list[str] = []
        for task_id in level:
            if task_id in seen or len(seen) >= MAX_ANCESTORS:
                continue
            seen.add(task_id)
            try:
                doc = fetch(task_id)
            except InputUnavailable:
                # A parent this step declares is read as staging reads it, and
                # its refusal is the attempt's. One further back that the read
                # refuses is not of this tenant's workflow -- `state/upstream`
                # serves exactly this workflow's ancestors -- so it is no step
                # whose questions this review is owed.
                if task_id in direct:
                    raise
                continue
            if doc.get("workflow_id") != workflow_id:
                continue
            found.append((task_id, doc))
            following += _ids(doc.get("depends_on"))
        level = sorted(set(following) - seen)
    return found


def _counted(doc: Mapping[str, Any]) -> int:
    summary = doc.get("result_summary")
    count = summary.get(questions_mod.SUMMARY_KEY) if isinstance(summary, Mapping) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _validated(data: bytes, where: str) -> list[dict[str, Any]]:
    try:
        return questions_mod.validate(data)
    except questions_mod.Rejected as exc:
        raise InputUnavailable(f"{where} does not validate as its worker validated it: {exc}") from None


def questions_of(
    task_id: str, doc: Mapping[str, Any], *, store: ObjectStore, tenant_id: str
) -> list[dict[str, Any]]:
    """One step's validated questions, each with its stored answer or None."""
    if doc.get("state") != TaskState.SUCCEEDED.value:
        return []
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), Mapping) else {}
    count = _counted(doc)
    where = f"the {questions_mod.NAME} of upstream task {task_id}"
    if count:
        reference = inputs_mod.artifact_reference(
            dict(doc), tenant_id=tenant_id, upstream_task_id=task_id, filename=questions_mod.NAME)
        if reference.size_bytes > questions_mod.MAX_BYTES:
            raise InputUnavailable(f"{where} is larger than {questions_mod.MAX_BYTES} bytes")
        asked = _validated(store.download_bytes(reference.key), where)
        if len(asked) != count:
            raise InputUnavailable(
                f"{where} holds {len(asked)} question(s), not the {count} its worker counted")
    else:
        wait = metadata.get(questions_mod.QUESTION_WAIT_KEY)
        raw = wait.get("questions") if isinstance(wait, Mapping) else None
        if not isinstance(raw, list) or not raw:
            return []
        asked = _validated(json.dumps(raw).encode("utf-8"), f"the question wait of task {task_id}")
    answers: dict[int, Mapping[str, Any]] = {}
    for entry in metadata.get(questions_mod.OWNER_ANSWERS_KEY) or ():
        index = entry.get("question_index") if isinstance(entry, Mapping) else None
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            answers[index] = entry
    step_id = doc.get("step_id") if isinstance(doc.get("step_id"), str) else None
    rows: list[dict[str, Any]] = []
    for index, question in enumerate(asked):
        answer = answers.get(index)
        rows.append({
            "task_id": task_id,
            "step_id": step_id,
            "question_index": index,
            "question": question.get("question"),
            "options": question.get("options"),
            "recommended": question.get("recommended"),
            "context": question.get("context"),
            "blocking": question.get("blocking") is True,
            "answer": None if answer is None else {
                "choice": answer.get("choice"), "text": answer.get("text")},
        })
    return rows


def collect(
    task: Mapping[str, Any], *, fetch: Fetch, store: ObjectStore, tenant_id: str
) -> list[dict[str, Any]]:
    """Every earlier step's questions for a review; [] for any other step."""
    if not is_review(task.get("metadata")):
        return []
    rows: list[dict[str, Any]] = []
    for task_id, doc in workflow_ancestors(task, fetch):
        rows += questions_of(task_id, doc, store=store, tenant_id=tenant_id)
    return rows


def guidance(path: Path | str, rows: list[dict[str, Any]]) -> str:
    """The review's verdict instructions for the questions it was given."""
    open_count = sum(1 for row in rows if row["answer"] is None)
    return (
        f"\n\nEarlier steps of this workflow asked the owner {len(rows)} question(s), "
        f"{open_count} still unanswered. They are in {path}, a JSON list of "
        "{task_id, step_id, question_index, question, options, recommended, context, "
        "blocking, answer}, with answer null while the owner has not answered. Read it "
        f"before you write verdict.json. In verdict.json, put in `{NEEDS_OWNER_KEY}` a "
        "list of {task_id, step_id, question_index, question}, one for each UNANSWERED "
        "question that must hold the merge: one whose answer could reverse a recorded "
        "owner decision, one about IAM, secrets, tenant isolation, cost or the frozen "
        "contract, and one the asker marked blocking. Put every other unanswered "
        f"question, in the same shape, in `{OPEN_QUESTIONS_KEY}`, so the pull request "
        f"lists it. The merge step will not merge while a `{NEEDS_OWNER_KEY}` question "
        "is unanswered; it merges past the others. An answered question goes in neither."
    )


def stage(work: Path | str, payload: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    """Write `work/owner-questions.json` and close the prompt with `guidance`; how many.

    Written whole on every attempt, and removed when there is nothing to
    stage, so a stale file restored from a checkpoint is never trusted.
    """
    path = Path(work) / NAME
    if not rows:
        path.unlink(missing_ok=True)
        return 0
    path.write_text(json.dumps(rows, indent=2))
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        payload["prompt"] = prompt + guidance(path.resolve(), rows)
    return len(rows)
