"""`swarm_result` shows a review's verdict (#2837).

Reported 2026-10-30: a re-review's NOT_YET lived only in its handoff, and
`swarm_result` gave the reader nothing to see it by. Now a task whose
artifacts hold `verdict.json` carries `verdict`: MERGE or NOT_YET, and how many
findings the file lists, read through the redacted artifacts route. A task
with no such file has no `verdict` key and pays no extra read; a file that
does not read says why, never a guessed verdict.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp import server

from control_plane.conftest import PROJECT
from test_follow_cursor import NOW, TENANT, World, swarm, world  # noqa: F401 - fixtures

BUCKET = f"swarm-artifacts-{PROJECT}"
NAME = "verdict.json"


def _finish(world: World, task_id: str, *, body: str | None) -> None:
    world.task(task_id, state="SUCCEEDED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    entries = []
    if body is not None:
        key = f"tenants/{TENANT}/tasks/{task_id}/attempts/att_1/artifacts/{NAME}"
        world.objects.put(key, body)
        entries.append({"name": NAME, "bytes": len(body.encode("utf-8")), "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {
        "runner": {"summary": "done"},
        "artifacts": entries,
        "artifact_bytes": sum(e["bytes"] for e in entries),
        "logs": {},
    }


def _result(swarm, task_id: str) -> dict[str, Any]:
    return json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))


def test_a_not_yet_shows_with_its_findings_count(swarm, world):
    body = json.dumps({"verdict": "NOT_YET", "findings": ["a.py:1 is wrong", "b.py:2 is wrong"]})
    _finish(world, "task_r", body=body)
    # MUTATION: leave `verdict` out of `_result_of` and this is a KeyError.
    assert _result(swarm, "task_r")["verdict"] == {"verdict": "NOT_YET", "findings": 2, "file": NAME}


def test_a_merge_shows_with_no_findings(swarm, world):
    _finish(world, "task_m", body=json.dumps({"verdict": "merge", "findings": []}))
    assert _result(swarm, "task_m")["verdict"] == {"verdict": "MERGE", "findings": 0, "file": NAME}


def test_a_task_with_no_verdict_file_has_no_verdict_and_reads_nothing(swarm, world, monkeypatch):
    _finish(world, "task_n", body=None)

    def _no_read(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("verdict.json must not be read for a task that has none")

    monkeypatch.setattr(swarm, "artifact_content", _no_read)
    assert "verdict" not in _result(swarm, "task_n")


def test_a_verdict_file_that_does_not_read_says_so(swarm, world):
    _finish(world, "task_u", body=json.dumps({"verdict": "LGTM"}))
    shown = _result(swarm, "task_u")["verdict"]
    assert shown["verdict"] is None
    assert "LGTM" in shown["unavailable_because"]
