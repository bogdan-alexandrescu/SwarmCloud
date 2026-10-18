"""An answer's files are staged with its form (HW-WORKER, part of #1436).

An `await-input` step's answer is one artifact (`review.json`) whose manifest
entry lists the files the person attached, named `review.files/<file>`
(docs/design/human-steps.md §4.3, "Staging", owner decision Q12). A dependant
that declares `input_from: {review: review.json}` gets those files too, beside
the form, held to every check the form is held to: the tenant prefix
(`artifact_key`), the destination (`destination_for`), a whole `bytes`, the
one cap on what an attempt may stage, and the checkpoint rule on a resume.

An entry with no `attachments` -- every entry a worker's own upload writes --
must stage exactly as it did before, which the last tests hold.

MUTATIONS: stage only the declared artifact (the two-file test goes red);
take an attachment's key from its URI without `artifact_key` (the forged-URI
cases go red and leave a file behind); default a missing `bytes` to 0 (the
sizeless test goes red); sum only the artifacts' sizes (the cap test goes
red); re-read the manifest on a resume (the checkpoint test raises, because
its database holds no upstream task); sort the staged list by name alone (the
form is no longer the first entry of its upstream, which a `when` gate reads).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker.errors import InputUnavailable
from agent_worker.logs import build_logger
from swarm_common.states import TaskState

from worker_seeds import TENANT

FORM = b'{"verdict": "REQUEST_CHANGES", "comment": "see the marked-up draft"}'
DRAFT = b"# The draft, as the reviewer marked it up\n"
NOTES = b"line one\nline two\n"
ATTEMPT = "input-0123456789ab"


def _key(name: str, *, tenant: str = TENANT, task: str = "task_await", attempt: str = ATTEMPT,
         area: str = "artifacts") -> str:
    return f"tenants/{tenant}/tasks/{task}/attempts/{attempt}/{area}/{name}"


def _logger() -> Any:
    return build_logger(
        task_id="task_2",
        attempt_id="att_2",
        tenant_id=TENANT,
        generation=1,
        runner_profile="mock",
        stream=io.StringIO(),
    )


def _attachment(store: Any, name: str, body: bytes, **where: str) -> dict[str, Any]:
    key = _key(name, **where)
    store.upload_bytes(key, body)
    return {
        "name": name,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "content_type": "text/plain; charset=utf-8",
        "uri": store.uri(key),
    }


def _seed_answer(
    db: Any, store: Any, attachments: list[dict[str, Any]] | None, *,
    task_id: str = "task_await", filename: str = "review.json",
) -> None:
    key = _key(filename, task=task_id)
    store.upload_bytes(key, FORM)
    entry: dict[str, Any] = {
        "name": filename,
        "bytes": len(FORM),
        "sha256": hashlib.sha256(FORM).hexdigest(),
        "uri": store.uri(key),
    }
    if attachments is not None:
        entry["attachments"] = attachments
    db.seed(
        f"tasks/{task_id}",
        {
            "id": task_id,
            "tenant_id": TENANT,
            "state": TaskState.SUCCEEDED.value,
            "result_summary": {"artifacts": [entry]},
        },
    )


def _stage(db: Any, store: Any, work: Path, declaration: dict[str, Any], *,
           resumed: bool = False, max_total_bytes: int = 1 << 20) -> list[inputs_mod.StagedInput]:
    return inputs_mod.stage_inputs(
        inputs_mod.declared_inputs(declaration),
        work=work,
        store=store,
        db=db,
        tenant_id=TENANT,
        logger=_logger(),
        resumed=resumed,
        max_total_bytes=max_total_bytes,
        reserved=frozenset({"repo", "input.json", "result.json"}),
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def _files(work: Path) -> list[str]:
    return sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# the answer's files arrive beside its form
# ---------------------------------------------------------------------------


def test_an_entry_with_two_attachments_stages_them_under_its_files_directory(db, store, work):
    _seed_answer(db, store, [
        _attachment(store, "review.files/draft.md", DRAFT),
        _attachment(store, "review.files/notes.txt", NOTES),
    ])

    staged = _stage(db, store, work, {"input_from": {"task_await": "review.json"}})

    assert (work / "review.json").read_bytes() == FORM
    assert (work / "review.files" / "draft.md").read_bytes() == DRAFT
    assert (work / "review.files" / "notes.txt").read_bytes() == NOTES
    assert [item.path for item in staged] == [
        "review.json", "review.files/draft.md", "review.files/notes.txt",
    ]
    # The form is its upstream's FIRST staged entry: a `when` gate reads that
    # one as the verdict, and `review.files/…` sorts before `review.json`.
    assert staged[0].filename == "review.json" and staged[0].attachment_of is None
    draft = staged[1].as_dict()
    assert draft == {
        "task_id": "task_await",
        "filename": "review.files/draft.md",
        "path": "review.files/draft.md",
        "bytes": len(DRAFT),
        "uri": store.uri(_key("review.files/draft.md")),
        "sha256": hashlib.sha256(DRAFT).hexdigest(),
        "attachment_of": "review.json",
    }


def test_a_step_staged_by_parent_gets_the_files_beside_the_form_under_its_step(db, store, work):
    _seed_answer(db, store, [_attachment(store, "review.files/draft.md", DRAFT)])

    staged = _stage(db, store, work, {
        "input_from": {"task_await": "review.json"},
        "dispatch": {"input_parents": {"task_await": "review"}},
    })

    assert [item.path for item in staged] == ["review/review.json", "review/review.files/draft.md"]
    assert (work / "review" / "review.files" / "draft.md").read_bytes() == DRAFT


# ---------------------------------------------------------------------------
# a forged or malformed attachment stages nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "where",
    [
        pytest.param({"tenant": "tenant-other"}, id="another-tenant"),
        pytest.param({"task": "task_other"}, id="another-task"),
        pytest.param({"area": "checkpoints"}, id="outside-artifacts"),
    ],
)
def test_an_attachment_outside_the_upstreams_artifacts_is_refused_and_stages_nothing(
    db, store, work, where
):
    _seed_answer(db, store, [
        _attachment(store, "review.files/draft.md", DRAFT),
        _attachment(store, "review.files/notes.txt", NOTES, **where),
    ])

    with pytest.raises(InputUnavailable) as raised:
        _stage(db, store, work, {"input_from": {"task_await": "review.json"}})

    assert "review.files/notes.txt" in str(raised.value)
    assert _files(work) == [], "the form and its honest sibling must not have been fetched"


def test_an_attachment_with_no_bytes_is_refused(db, store, work):
    sizeless = _attachment(store, "review.files/draft.md", DRAFT)
    del sizeless["bytes"]
    _seed_answer(db, store, [sizeless])

    with pytest.raises(InputUnavailable) as raised:
        _stage(db, store, work, {"input_from": {"task_await": "review.json"}})

    assert "no size" in str(raised.value)
    assert _files(work) == []


@pytest.mark.parametrize(
    "attachments",
    [
        pytest.param("review.files/draft.md", id="not-a-list"),
        pytest.param(["review.files/draft.md"], id="entry-not-a-mapping"),
    ],
)
def test_a_malformed_attachments_list_is_refused_not_ignored(db, store, work, attachments):
    _seed_answer(db, store, attachments)

    with pytest.raises(InputUnavailable):
        _stage(db, store, work, {"input_from": {"task_await": "review.json"}})
    assert _files(work) == []


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("draft.md", id="beside-the-form"),
        pytest.param("other.files/draft.md", id="another-artifacts-directory"),
        pytest.param("review.files/../input.json", id="traversal"),
        pytest.param("review.files/sub/draft.md", id="nested"),
    ],
)
def test_an_attachment_named_outside_its_files_directory_is_refused(db, store, work, name):
    # Recorded, not uploaded: the store itself refuses to write a traversing key.
    recorded = {"name": name, "bytes": len(DRAFT), "uri": f"gs://bucket/{_key(name)}"}
    _seed_answer(db, store, [recorded])

    with pytest.raises(InputUnavailable):
        _stage(db, store, work, {"input_from": {"task_await": "review.json"}})
    assert _files(work) == []


def test_two_answers_whose_files_would_collide_are_refused(db, store, work):
    """`review.json` and `review.md` both name their files under `review.files/`."""
    _seed_answer(db, store, [_attachment(store, "review.files/draft.md", DRAFT)])
    _seed_answer(db, store, [_attachment(store, "review.files/draft.md", NOTES, task="task_two")],
                 task_id="task_two", filename="review.md")

    with pytest.raises(InputUnavailable) as raised:
        _stage(db, store, work, {"input_from": {"task_await": "review.json",
                                                "task_two": "review.md"}})

    assert "overwrite" in str(raised.value)
    assert _files(work) == []


# ---------------------------------------------------------------------------
# the cap and the checkpoint
# ---------------------------------------------------------------------------


def test_attachments_count_toward_the_cap_before_anything_is_downloaded(db, store, work):
    _seed_answer(db, store, [
        _attachment(store, "review.files/draft.md", b"d" * 600),
        _attachment(store, "review.files/notes.txt", b"n" * 600),
    ])

    with pytest.raises(InputUnavailable) as raised:
        _stage(db, store, work, {"input_from": {"task_await": "review.json"}},
               max_total_bytes=1000)

    assert f"{len(FORM) + 1200} bytes" in str(raised.value)
    assert _files(work) == []


def test_an_attachment_that_under_reports_is_caught_by_the_bytes_that_arrive(db, store, work):
    lying = _attachment(store, "review.files/draft.md", b"d" * 900)
    lying["bytes"] = 9
    _seed_answer(db, store, [lying])

    with pytest.raises(InputUnavailable) as raised:
        _stage(db, store, work, {"input_from": {"task_await": "review.json"}},
               max_total_bytes=500)

    assert "actually downloaded" in str(raised.value)
    assert not (work / "review.files" / "draft.md").exists()


def test_a_resumed_attempt_with_attachments_in_its_checkpoint_does_not_refetch(db, work):
    """`db` holds no upstream task, and no store is given: any read raises."""
    (work / "review.json").write_bytes(FORM)
    (work / "review.files").mkdir()
    (work / "review.files" / "draft.md").write_text("edited by the agent\n")
    (work / "review.files" / "notes.txt").write_bytes(NOTES)

    staged = _stage(db, None, work, {"input_from": {"task_await": "review.json"}}, resumed=True)

    assert [(item.path, item.from_checkpoint, item.attachment_of) for item in staged] == [
        ("review.json", True, None),
        ("review.files/draft.md", True, "review.json"),
        ("review.files/notes.txt", True, "review.json"),
    ]
    assert (work / "review.files" / "draft.md").read_text() == "edited by the agent\n"


# ---------------------------------------------------------------------------
# no attachments: exactly as today
# ---------------------------------------------------------------------------


def test_an_entry_without_attachments_stages_exactly_as_today(db, store, work):
    _seed_answer(db, store, None)

    reference = inputs_mod.artifact_reference(
        db.doc("tasks/task_await"), tenant_id=TENANT,
        upstream_task_id="task_await", filename="review.json",
    )
    staged = _stage(db, store, work, {"input_from": {"task_await": "review.json"}})

    assert reference.attachments == ()
    assert _files(work) == ["review.json"]
    assert [item.as_dict() for item in staged] == [{
        "task_id": "task_await",
        "filename": "review.json",
        "path": "review.json",
        "bytes": len(FORM),
        "uri": store.uri(_key("review.json")),
        "sha256": hashlib.sha256(FORM).hexdigest(),
    }]


def test_a_resumed_entry_without_attachments_reports_only_itself(db, work):
    (work / "summary.md").write_text("from the checkpoint\n")

    staged = _stage(db, None, work, {"input_from": {"task_up": "summary.md"}}, resumed=True)

    assert [(item.path, item.attachment_of) for item in staged] == [("summary.md", None)]
