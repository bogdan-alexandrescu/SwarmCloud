"""`GET /v1/tasks/{id}/artifacts` answers about artifacts that exist.

THE DEFECT, and why an empty list was the worst possible shape for it:

    The route streamed the Firestore subcollection `tasks/<id>/artifacts`.
    Nothing in this repository writes that subcollection -- `ARTIFACTS` was
    referenced in exactly one place, the query itself. The worker's manifest goes
    somewhere else entirely: `AgentLifecycle._finalize` builds
    `[{"name", "bytes", "uri"}, ...]` and `control.finish()` stores it as
    `task.result_summary["artifacts"]`, which is also where
    `agent_worker.inputs` reads a previous step's outputs from.

    So the endpoint returned `{"artifacts": []}` with a 200, for every task, for
    ever -- and "200, none" is indistinguishable from the truth. A 500 would have
    been found in a week. apps/swarm-ui/src/api.ts had instead written the
    workaround into a comment: "deliberately NOT called: it reads a Firestore
    subcollection nothing writes".

The fix points the reader at the existing writer. These tests pin that, and pin
the two distinctions an artifact list has to keep: unfinished vs. produced
nothing, and dropped-at-the-cap vs. never-written.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .conftest import auth_header, seed_task, seed_tenant

BUCKET = "swarm-artifacts-saga-agents-staging"

#: The exact shape `AgentLifecycle._finalize` writes. Kept literal rather than
#: imported so that a change on the worker side shows up here as a failure
#: rather than being silently followed.
FINISHED_SUMMARY = {
    "artifacts": [
        {"name": "report.md", "bytes": 8241,
         "uri": f"gs://{BUCKET}/tenants/eng/tasks/task_a/attempts/att_1/artifacts/report.md"},
        {"name": "diff.patch", "bytes": 91233,
         "uri": f"gs://{BUCKET}/tenants/eng/tasks/task_a/attempts/att_1/artifacts/diff.patch"},
    ],
    "artifact_bytes": 99474,
    "artifacts_skipped": ["core.dump"],
    "logs": {"stdout": f"gs://{BUCKET}/tenants/eng/logs/stdout.log"},
}


def _finish(db, task_id: str, summary: dict) -> None:
    db.docs[f"tasks/{task_id}"]["state"] = "SUCCEEDED"
    db.docs[f"tasks/{task_id}"]["completed_at"] = datetime.now(timezone.utc)
    db.docs[f"tasks/{task_id}"]["result_summary"] = summary


def test_the_worker_manifest_is_what_the_route_returns(client, db) -> None:
    """The regression. Before this, every one of these was absent from a 200."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _finish(db, "task_a", FINISHED_SUMMARY)

    response = client.get("/v1/tasks/task_a/artifacts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["task_id"] == "task_a"
    assert [a["name"] for a in body["artifacts"]] == ["report.md", "diff.patch"]
    assert body["artifacts"][0]["uri"].startswith(f"gs://{BUCKET}/tenants/eng/")
    assert body["artifacts"][0]["bytes"] == 8241
    assert body["artifact_bytes"] == 99474


def test_an_unfinished_task_says_so_instead_of_saying_none(client, db) -> None:
    """`result_summary` is written once, at terminal state.

    Without `complete`, a RUNNING task and a task that genuinely produced nothing
    return byte-identical bodies, and a caller polling for output cannot tell
    whether to keep waiting.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_running", tenant_id="eng", state="RUNNING")

    body = client.get(
        "/v1/tasks/task_running/artifacts", headers=auth_header("alice")
    ).json()
    assert body["artifacts"] == []
    assert body["complete"] is False

    _finish(db, "task_running", {"artifacts": [], "artifact_bytes": 0, "logs": {}})
    body = client.get(
        "/v1/tasks/task_running/artifacts", headers=auth_header("alice")
    ).json()
    assert body["artifacts"] == []
    assert body["complete"] is True, "a finished task that produced nothing is not 'pending'"


def test_files_dropped_at_the_size_cap_are_named(client, db) -> None:
    """A short list with no reason for being short is the same lie, smaller.

    The worker stops uploading once the attempt passes `max_artifact_bytes` and
    records the names it dropped. Omitting them leaves the caller looking for a
    file that the platform decided not to keep.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _finish(db, "task_a", FINISHED_SUMMARY)

    body = client.get("/v1/tasks/task_a/artifacts", headers=auth_header("alice")).json()
    assert body["artifacts_skipped"] == ["core.dump"]


def test_another_tenants_task_is_a_404_not_an_empty_list(client, db) -> None:
    """The tenant check has to happen before the read, not after.

    A dead endpoint hid this: returning `[]` for a task in another tenant looks
    identical to returning `[]` for every task. Now that the route answers with
    real data, the boundary is the only thing keeping bob out of eng's manifest.
    """
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _finish(db, "task_a", FINISHED_SUMMARY)

    response = client.get("/v1/tasks/task_a/artifacts", headers=auth_header("bob"))
    assert response.status_code == 404, response.text
    assert "report.md" not in response.text


def test_the_limit_applies_to_the_manifest(client, db) -> None:
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _finish(db, "task_a", FINISHED_SUMMARY)

    body = client.get(
        "/v1/tasks/task_a/artifacts?limit=1", headers=auth_header("alice")
    ).json()
    assert [a["name"] for a in body["artifacts"]] == ["report.md"]


def test_a_malformed_summary_does_not_500(client, db) -> None:
    """`result_summary` is a free-form dict on the frozen Task.

    An older worker, a partial write or a hand-edited document can put anything
    in it. The read path answers "nothing to show" rather than raising, because
    a 500 on the artifact route is how a finished task becomes unreadable.
    """
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    _finish(db, "task_a", {"artifacts": "not-a-list", "artifacts_skipped": 7})

    response = client.get("/v1/tasks/task_a/artifacts", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["artifacts"] == []
    assert body["artifacts_skipped"] == []


def test_nothing_reads_a_task_artifacts_subcollection_any_more() -> None:
    """The dead read must not come back, in this or any other component.

    It was survivable for so long precisely because it was invisible: one
    `.collection("artifacts")` under a task document, answering 200 with `[]`.
    """
    from pathlib import Path

    apps = Path(__file__).resolve().parents[3] / "apps"
    assert apps.is_dir(), f"{apps} is not the apps tree; this check would scan nothing"
    offenders = []
    for path in apps.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if ".collection(ARTIFACTS)" in line or '.collection("artifacts")' in line:
                offenders.append(f"{path}:{number}: {line.strip()}")

    assert not offenders, (
        "nothing writes tasks/<id>/artifacts; the manifest is "
        "task.result_summary['artifacts']:\n  " + "\n  ".join(offenders)
    )
