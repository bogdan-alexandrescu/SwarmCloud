"""The `graph_ready` start-up mark (lane GS-MEASURE, part of #1384).

docs/design/graph-service.md §9.1: staging had no timer, its bytes lived only
in the pod-local `stage.json`, and no start-up mark isolated it -- its time
was folded into the `credentials` phase. §9.3: `graphstage.prepare` times
`_stage`, and the lifecycle emits a RUNNING start-up mark `graph_ready` with
`{delivery, seconds, bytes, blobs, reason}` beside `clone_timed`. A week of
those readings on `staged` is the baseline the `service` delivery is judged
against (§8.2 steps 1 and 5).

Offline: a LocalObjectStore, the worker's FakeFirestore and a local git repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_worker import graphstage, lifecycle
from agent_worker.errors import ExitCode
from swarm_common.states import EventType

from test_graphstage import (  # noqa: F401 -- the `clone` fixture
    URL,
    _arm_task,
    _BrokenStore,
    _no_ab,
    _promote,
    _store_for_seed,
    clone,
)
from worker_seeds import TENANT, seed_attempt

ARGV = ["python", "-m", "agent_worker.runners.claude_code"]
FIELDS = {"delivery", "seconds", "bytes", "blobs", "reason"}


def _prepare(db: Any, store: Any, checkout: Path, base: str, *, task_id: str = "task_b"):
    marks: list[dict[str, Any]] = []
    env = graphstage.prepare(checkout.parent, profile_name="claude-code", runner_argv=ARGV,
                             db=db, store=store, tenant_id=TENANT, repository_url=URL,
                             task_id=task_id, checkout=checkout, base_sha=base,
                             mark=marks.append)
    return env, marks


def _graph_marks(db: Any) -> list[dict[str, Any]]:
    return [
        event["detail"] for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == graphstage.READY_MARK
    ]


# --------------------------------------------------------------------------
# what `prepare` hands the lifecycle
# --------------------------------------------------------------------------


def test_the_mark_is_named_graph_ready():
    assert graphstage.READY_MARK == "graph_ready"


def test_a_staged_graph_is_marked_with_its_delivery_seconds_bytes_and_blobs(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)

    env, marks = _prepare(db, store, checkout, second)

    assert graphstage.SNAPSHOT_ENV in env
    (detail,) = marks
    graph = detail["graph"]
    assert set(graph) == FIELDS
    assert graph["delivery"] == "staged"
    assert graph["blobs"] == 1
    record = json.loads((checkout.parent / graphstage.CONTEXT_DIR_NAME /
                         graphstage.STAGE_RECORD_NAME).read_text())
    # The mark and stage.json say the same thing: one measurement, two readers.
    assert graph["bytes"] == record["bytes"] > 0
    assert graph["seconds"] == record["seconds"] >= 0.0
    assert graph["reason"] == record["reason"] and graph["reason"].startswith("staged the graph")


def test_a_graph_that_could_not_be_staged_is_marked_none_with_its_reason(db, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, _store_for_seed(checkout), first)

    env, marks = _prepare(db, _BrokenStore(), checkout, second)

    assert set(env) == {graphstage.UNSTAGED_ENV}
    (detail,) = marks
    graph = detail["graph"]
    assert set(graph) == FIELDS
    assert graph["delivery"] == "none"
    assert "OSError" in graph["reason"]
    # Nothing was delivered, so nothing is counted: never a zero that reads
    # as "an empty graph was staged".
    assert graph["bytes"] is None and graph["blobs"] is None
    assert isinstance(graph["seconds"], float)


def test_a_step_the_setting_did_not_ask_for_is_not_marked(db, store, clone):
    """Arm A, or context off: there was no graph to deliver, so no reading."""
    checkout, first, second = clone
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "ab"}})
    _promote(db, store, first)

    env, marks = _prepare(db, store, checkout, second, task_id=_arm_task("A"))

    assert env == {} and marks == []


def test_a_runner_that_reads_no_graph_is_not_marked(db, store, clone):
    checkout, first, second = clone
    _promote(db, store, first)
    marks: list[dict[str, Any]] = []
    graphstage.prepare(checkout.parent, profile_name="mock",
                       runner_argv=["python", "-m", "agent_worker.runners.mock"], db=db,
                       store=store, tenant_id=TENANT, repository_url=URL, task_id="task_b",
                       checkout=checkout, base_sha=second, mark=marks.append)
    assert marks == []


def test_a_mark_that_fails_never_costs_the_graph(db, store, clone):
    checkout, first, second = clone
    _no_ab(db)
    _promote(db, store, first)

    def broken(_detail: dict[str, Any]) -> None:
        raise RuntimeError("events unavailable")

    env = graphstage.prepare(checkout.parent, profile_name="claude-code", runner_argv=ARGV,
                             db=db, store=store, tenant_id=TENANT, repository_url=URL,
                             task_id="task_b", checkout=checkout, base_sha=second,
                             mark=broken)
    assert graphstage.SNAPSHOT_ENV in env


def test_the_mark_carries_numbers_and_the_reason_only():
    """No path, no URL, no digest: the event stream is served as stored."""
    result = graphstage.StageResult(
        True, "staged the graph of 0123456789ab (git)",
        snapshot=Path("/work/.swarm-context/snapshot"), seconds=1.5,
        facts={"index_sha": "0" * 40, "format_version": 3, "blobs": 4, "bytes": 2048,
               "behind_by": 0, "basis": "same", "changed": 0})
    assert graphstage.ready_detail(result) == {"graph": {
        "delivery": "staged", "seconds": 1.5, "bytes": 2048, "blobs": 4,
        "reason": "staged the graph of 0123456789ab (git)"}}


# --------------------------------------------------------------------------
# the lifecycle emits it, once per attempt, on the start-up mark path
# --------------------------------------------------------------------------


def test_the_lifecycle_emits_graph_ready_once_as_a_running_mark(
        db, worker_factory, monkeypatch):
    calls: list[str] = []
    detail = {"graph": {"delivery": "staged", "seconds": 3.25, "bytes": 9_319_749,
                        "blobs": 782, "reason": "staged the graph of 4f54cef00000 (git)"}}

    def fake_prepare(*_args: Any, mark: Any = None, **_kwargs: Any) -> dict[str, str]:
        calls.append("prepare")
        assert mark is not None, "the lifecycle passed no mark to staging"
        mark(dict(detail))
        # A second report in one attempt is not a second reading.
        mark(dict(detail))
        return {}

    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", fake_prepare)
    seed_attempt(db)
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")

    assert calls == ["prepare"]
    (mark,) = _graph_marks(db)
    assert mark == {"cause": "graph_ready", **detail}
    (event,) = [e for e in db.events("task_1")
                if (e.get("detail") or {}).get("cause") == "graph_ready"]
    assert event["type"] == EventType.RUNNING.value


def test_no_staging_report_means_no_graph_ready_mark(db, worker_factory, monkeypatch):
    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare",
                        lambda *_a, **_k: {})
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert _graph_marks(db) == []
