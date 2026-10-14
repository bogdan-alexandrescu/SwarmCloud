"""The effective merge mode is signed into the merge step at submission (TRAIN-WS).

docs/merge-trains.md §5.1. When a workflow gets a merge step, swarm-api reads
the workspace's merge settings and the repository registration's override and,
when the effective mode is `train`, writes `merge_target.merge_mode: "train"`
inside the signed dispatch block. Direct mode writes nothing, so every merge
step submitted in a workspace that never chose trains stores exactly the block
it stored before this lane.
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_api import mergesettings
from swarm_api.repositories import repo_id_for

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"


def _spec() -> dict:
    return {
        "strategy": "direct-pr",
        "repository_url": REPO,
        "metadata": {"merge": "on"},
        "steps": [{"step_id": "implement", "runner_profile": "mock",
                   "input": {"prompt": "implement the change"}}],
    }


def _merge_target(client, db) -> dict:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec())
    assert response.status_code == 201, response.text
    (step,) = [s for s in response.json()["workflow"]["steps"] if s["runner_profile"] == "merge"]
    return db.docs[f"tasks/{step['task_id']}"]["metadata"]["dispatch"]["merge_target"]


def _register(db, **fields):
    repo_id = repo_id_for("eng", "octo-org", "widget-shop")
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": "eng", "owner": "octo-org", "repo": "widget-shop",
        "default_branch": "main", "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        **fields,
    }
    return repo_id


def _workspace_mode(db, mode: str) -> None:
    db.docs[f"{mergesettings.COLLECTION}/eng"] = {"tenant_id": "eng", "mode": mode}


def test_a_workspace_that_never_chose_trains_signs_no_merge_mode(client, db):
    target = _merge_target(client, db)
    assert "merge_mode" not in target


def test_a_train_workspace_signs_train_into_the_merge_target(client, db):
    _workspace_mode(db, "train")
    assert _merge_target(client, db)["merge_mode"] == "train"


def test_a_repository_override_to_direct_beats_a_train_workspace(client, db):
    _workspace_mode(db, "train")
    _register(db, **{mergesettings.REPO_FIELD: "direct"})
    assert "merge_mode" not in _merge_target(client, db)


def test_a_repository_override_to_train_beats_a_direct_workspace(client, db):
    _register(db, **{mergesettings.REPO_FIELD: "train"})
    assert _merge_target(client, db)["merge_mode"] == "train"
