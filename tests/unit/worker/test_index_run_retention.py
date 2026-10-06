"""An index run keeps its index under repos/ and retires what no kept version names
(docs/repo-index.md §2 and §2.3, lane IX3, owner decision 2026-10-06).

The artifact lives under `tasks/`, which the bucket's lifecycle cold-stores
and expires. What is held here:

* after the upload the worker copies repo-index.json, byte for byte, to
  `tenants/<t>/repos/<repo_id>/index/<commit_sha>/repo-index.json` -- the
  commit is the signed `repository_ref`, the repo_id derived from the signed
  `repository_url` (invariant 9), never metadata;
* the graph write is given every commit whose version swarm-api still keeps
  (`--keep-commit`), plus the promoted one and its own, so its sweep retires
  the rest; a registration that is not this tenant's gives no keep set, and
  the sweep then retires nothing;
* an incremental run stages its base index from that copy, so a base whose
  artifact has expired is still built on; a copy that does not match the
  promoted digest is not used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_worker import indexrun
from swarm_api import repoindex

from conftest import TENANT
from test_index_run_incremental import (  # noqa: F401 - fixtures
    BASE,
    BASE_INDEX,
    HEAD,
    REPO_ID,
    URL,
    _flag,
    _order,
    _phases,
    _seed,
    checkout,
    index_tools,
)

ARTIFACT_KEY = f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/{indexrun.INDEX_FILE}"


def _kept_key(commit: str) -> str:
    return f"tenants/{TENANT}/repos/{REPO_ID}/index/{commit}/{indexrun.INDEX_FILE}"


def _run(db: Any, worker_factory: Any, timeout: int = 1800) -> None:
    db.doc("tasks/task_1")["repository_ref"] = HEAD
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=timeout,
                                  repository_url=URL)
    worker.run()


def _keep(order) -> list[str]:
    [write] = [row for name, row in _order(order) if name == "graph_write"]
    argv = write["argv"]
    return [argv[i + 1] for i, part in enumerate(argv) if part == "--keep-commit"]


def test_the_worker_and_the_api_build_the_same_index_key():
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    assert where.index_key(HEAD) == repoindex.index_key(TENANT, REPO_ID, HEAD) == _kept_key(HEAD)


def test_the_index_is_copied_under_repos_byte_for_byte(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    _run(db, worker_factory)

    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"
    artifact = store.download_bytes(ARTIFACT_KEY)
    # The graph write set the manifest digest before the upload: the copy is
    # the document promotion validates, not the agent's first draft.
    assert json.loads(artifact)["graph"]["manifest_digest"]
    assert store.download_bytes(_kept_key(HEAD)) == artifact


def test_the_copy_follows_the_signed_spec_not_the_metadata(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    other = hashlib.sha1(b"elsewhere").hexdigest()
    db.doc("tasks/task_1")["metadata"].update(repo_index="repo_elsewhere", commit_sha=other)
    _run(db, worker_factory)

    assert store.exists(_kept_key(HEAD))
    assert not store.exists(_kept_key(other))
    assert not store.list_keys(f"tenants/{TENANT}/repos/repo_elsewhere/")


def test_the_graph_write_is_given_every_kept_commit(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    older = [hashlib.sha1(f"v{n}".encode()).hexdigest() for n in range(3)]
    for commit in older:
        db.seed(f"repositories/{REPO_ID}/index_versions/{commit}",
                {"commit_sha": commit, "task_id": f"task_{commit[:6]}"})
    db.doc(f"repositories/{REPO_ID}")["index"] = {"current_sha": older[0]}
    _run(db, worker_factory)

    assert _keep(index_tools) == sorted({BASE, HEAD, *older})


def test_another_tenants_registration_gives_no_keep_set(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    db.doc(f"repositories/{REPO_ID}")["tenant_id"] = "research"
    _run(db, worker_factory)

    assert _keep(index_tools) == []
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_a_version_naming_no_commit_retires_nothing(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    db.seed(f"repositories/{REPO_ID}/index_versions/broken", {"task_id": "task_x"})
    _run(db, worker_factory)

    assert _keep(index_tools) == []


def test_the_base_is_staged_from_its_copy_when_the_artifact_has_expired(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store)
    store.upload_bytes(_kept_key(BASE), BASE_INDEX)
    # The lifecycle took the artifact and nothing reads the task any more.
    store.delete(f"tenants/{TENANT}/tasks/task_base/attempts/att_9/artifacts/repo-index.json")
    db.documents.pop("tasks/task_base")
    _run(db, worker_factory, timeout=900)

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["read", "extract", "agent", "graph_write"], rows
    extract = rows[1][1]
    assert extract["seen"]["--base-index"] == BASE_INDEX.decode()
    stage = _phases(db)["stage_base"]
    assert stage["status"] == "ok" and "repos/" in stage["reason"], stage
    assert _flag(rows[3][1]["argv"], "--base-commit") == BASE


def test_a_copy_that_does_not_match_the_promoted_digest_is_not_built_on(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store)
    store.upload_bytes(_kept_key(BASE), b'{"rewritten": true}')
    _run(db, worker_factory, timeout=900)

    stage = _phases(db)["stage_base"]
    # The artifact still matches, so the run is incremental from it.
    assert stage["status"] == "ok" and "artifact" in stage["reason"], stage
    rows = _order(index_tools)
    assert rows[1][1]["seen"]["--base-index"] == BASE_INDEX.decode()
