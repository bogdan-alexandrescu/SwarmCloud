"""The worker writes `Attempt.checkpoint_sha256` in the shape its readers decode (contract request 51).

Since #348 the worker records each checkpoint's archive digest in the attempt
document, and a retry restores only a checkpoint whose archive matches it
(#347). Until request 51 the map had no name in the frozen contract: it was
`agent_worker.control.CHECKPOINT_DIGESTS_FIELD`, a free string, and swarm-api's
`codec.attempt_from_dict` dropped it without anyone deciding to.

These hold the three sides together over the production code paths:

* the worker's key IS the frozen `Attempt` field, of the type the request
  gave it, empty by default;
* what `ControlPlane.record_checkpoint` writes decodes, through both readers
  (the worker's `recorded_checkpoint_digests` and swarm-api's
  `attempt_from_dict`), to exactly the digests it was given;
* a missing digest is "no digest recorded" to both readers -- an empty map,
  never "anything goes" -- and `attempt_to_api` serves the field to no caller.
"""

from __future__ import annotations

import dataclasses
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.control import CHECKPOINT_DIGESTS_FIELD, recorded_checkpoint_digests
from swarm_api import codec
from swarm_common import models
from swarm_common.models import Attempt

from worker_seeds import TENANT, seed_attempt

RUN = {"prompt": "x", "steps": 1, "sleep_seconds": 0.05}


class _Quiet:
    def info(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...


def _attempt_document(**extra: Any) -> dict[str, Any]:
    return {
        "attempt_id": "att_1",
        "task_id": "task_1",
        "tenant_id": TENANT,
        "generation": 1,
        "lease_id": "lease_1",
        "backend": "cloud_run",
        "created_at": datetime(2026, 10, 9, tzinfo=timezone.utc),
        "checkpoints": [],
        **extra,
    }


def _record(worker: Any, manager: CheckpointManager, tmp_path: Path, *, label: str) -> Any:
    ws = workspace_mod.create(tmp_path / f"ws-{label}", "att_1")
    (ws.work / "notes.md").write_text(f"{label}\n")
    record = manager.create(ws, label=label)
    worker.control.record_checkpoint(
        checkpoint_id=record.checkpoint_id, uri=record.uri, size_bytes=record.archive_bytes,
        seq=record.seq, archive_sha256=record.archive_sha256,
    )
    return record


# ---------------------------------------------------------------------------
# the key is the frozen field
# ---------------------------------------------------------------------------


def test_the_worker_key_is_the_frozen_attempt_field():
    by_name = {f.name: f for f in dataclasses.fields(Attempt)}
    assert CHECKPOINT_DIGESTS_FIELD == "checkpoint_sha256"
    assert CHECKPOINT_DIGESTS_FIELD in by_name
    hints = typing.get_type_hints(Attempt, globalns=vars(models))
    assert hints[CHECKPOINT_DIGESTS_FIELD] == dict[str, str]
    # An empty default through a factory, as for `checkpoints`: every stored
    # attempt still decodes, and no two attempts share one map.
    assert by_name[CHECKPOINT_DIGESTS_FIELD].default_factory is dict
    first = codec.attempt_from_dict(_attempt_document())
    second = codec.attempt_from_dict(_attempt_document())
    assert first.checkpoint_sha256 == {}
    assert first.checkpoint_sha256 is not second.checkpoint_sha256


# ---------------------------------------------------------------------------
# what the worker writes is what both readers decode
# ---------------------------------------------------------------------------


def test_what_record_checkpoint_writes_decodes_to_the_typed_field(
    db, store, tmp_path, worker_factory
):
    seed_attempt(db, task_input=RUN)
    worker, _, _ = worker_factory()
    # The attempt's first write, as in production: the document the
    # checkpoints then merge into.
    worker.control.record_attempt_start(backend="cloud_run", execution_name=None)
    manager = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=_Quiet(),
    )
    first = _record(worker, manager, tmp_path, label="first")
    second = _record(worker, manager, tmp_path, label="second")
    assert first.checkpoint_id != second.checkpoint_id

    document = db.doc("attempts/att_1")
    expected = {
        first.checkpoint_id: first.archive_sha256,
        second.checkpoint_id: second.archive_sha256,
    }
    # The stored value is already the typed shape, not merely decodable to it.
    stored = document[CHECKPOINT_DIGESTS_FIELD]
    assert type(stored) is dict
    assert all(type(k) is str and type(v) is str for k, v in stored.items())
    assert stored == expected

    assert recorded_checkpoint_digests(document) == expected
    decoded = codec.attempt_from_dict(document)
    assert decoded.checkpoint_sha256 == expected
    assert decoded.checkpoints == [first.checkpoint_id, second.checkpoint_id]
    # Every listed checkpoint has its digest: the merge that lists the id
    # writes the digest with it.
    assert set(decoded.checkpoints) == set(decoded.checkpoint_sha256)


# ---------------------------------------------------------------------------
# a missing digest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(dataclasses.MISSING, id="absent (written before #348)"),
        pytest.param(None, id="null"),
        pytest.param([], id="not a map"),
        pytest.param("ckpt-00001", id="a string"),
        pytest.param({}, id="empty"),
    ],
)
def test_a_missing_digest_map_is_no_digest_recorded_to_both_readers(stored):
    document = _attempt_document(checkpoints=["ckpt-00001"])
    if stored is not dataclasses.MISSING:
        document[CHECKPOINT_DIGESTS_FIELD] = stored
    assert recorded_checkpoint_digests(document) == {}
    assert codec.attempt_from_dict(document).checkpoint_sha256 == {}


def test_only_str_to_str_entries_survive_either_reader():
    good = "a" * 64
    document = _attempt_document(
        checkpoints=["ckpt-00001", "ckpt-00002", "ckpt-00003"],
        **{CHECKPOINT_DIGESTS_FIELD: {"ckpt-00001": good, "ckpt-00002": None, "ckpt-00003": 7}},
    )
    assert recorded_checkpoint_digests(document) == {"ckpt-00001": good}
    assert codec.attempt_from_dict(document).checkpoint_sha256 == {"ckpt-00001": good}


def test_no_attempt_document_is_no_digest_recorded():
    assert recorded_checkpoint_digests(None) == {}


def test_the_api_serves_no_caller_the_digest_map():
    """Request 51: `attempt_to_api` does not serve it, so the response shape
    is unchanged (invariant 10)."""
    document = _attempt_document(
        checkpoints=["ckpt-00001"], **{CHECKPOINT_DIGESTS_FIELD: {"ckpt-00001": "b" * 64}}
    )
    served = codec.attempt_to_api(codec.attempt_from_dict(document))
    assert served["checkpoints"] == ["ckpt-00001"]
    assert CHECKPOINT_DIGESTS_FIELD not in served
