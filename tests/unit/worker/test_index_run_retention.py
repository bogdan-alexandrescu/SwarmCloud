"""An index run keeps its index under repos/ and retires what no kept version names
(docs/repo-index.md §2 and §2.3, lane IX3, owner decision 2026-10-06).

The artifact lives under `tasks/`, which the bucket's lifecycle cold-stores
and expires. What is held here:

* after the upload the worker copies repo-index.json, byte for byte, to
  `tenants/<t>/repos/<repo_id>/index/<commit_sha>/repo-index.json` -- the
  commit is the signed `repository_ref`, the repo_id derived from the signed
  `repository_url` (invariant 9), never metadata;
* the graph write never sweeps (`--no-sweep`); a separate best-effort
  `index_sweep` phase runs `swarm-repo-graph sweep` with every commit
  swarm-api named in the SIGNED prompt (`KEEP_LINE`), plus its own, and the
  worker reads no Firestore document for it (lane INDEX-RETENTION, #1693:
  the direct read was PermissionDenied on every run); no keep line, no
  sweep, and the phase says why; a sweep that fails never fails the run;
* an incremental run stages its base index from that copy, so a base whose
  artifact has expired is still built on; a copy that does not match the
  promoted digest is not used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from agent_worker import indexrun
from swarm_api import repoindex

from worker_seeds import TENANT
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


def _keep_prompt(*commits: str) -> str:
    return f"index it\n{indexrun.KEEP_LINE}{' '.join(commits)}\n"


def test_the_kept_commits_are_read_from_the_signed_prompt_only():
    older = [hashlib.sha1(f"v{n}".encode()).hexdigest() for n in range(3)]
    task_input = {"prompt": _keep_prompt(*older)}
    # MUTATION: dropping the run's own commit from the keep set deletes what it just wrote.
    assert indexrun.kept_commits(task_input, own=HEAD) == sorted({*older, HEAD})
    # Absent, doubled or malformed: no keep set, so nothing is retired, and why in words.
    assert isinstance(indexrun.kept_commits({"prompt": "index it"}, own=HEAD), str)
    doubled = {"prompt": _keep_prompt(older[0]) + _keep_prompt(older[1])}
    assert isinstance(indexrun.kept_commits(doubled, own=HEAD), str)
    assert isinstance(indexrun.kept_commits({"prompt": _keep_prompt("main")}, own=HEAD), str)
    assert isinstance(indexrun.kept_commits(None, own=HEAD), str)


def test_the_graph_write_never_sweeps(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full", prompt=_keep_prompt(BASE, HEAD))
    _run(db, worker_factory)

    [write] = [row for name, row in _order(index_tools) if name == "graph_write"]
    # MUTATION: `graph_write_argv` without --no-sweep, or still passing --keep-commit.
    assert "--no-sweep" in write["argv"]
    assert "--keep-commit" not in write["argv"]


def test_the_sweep_phase_is_given_the_signed_kept_commits_and_reads_no_database(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed, monkeypatch
):
    older = [hashlib.sha1(f"v{n}".encode()).hexdigest() for n in range(3)]
    _seed(db, store, kind="full", prompt=_keep_prompt(*older))
    # Versions in Firestore that disagree with the prompt: the worker must not read them.
    db.seed(f"repositories/{REPO_ID}/index_versions/{BASE}", {"commit_sha": "x"})
    read: list[str] = []
    real = db.collection

    def watched(name: str, *args: Any, **kwargs: Any) -> Any:
        read.append(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(db, "collection", watched)
    _run(db, worker_factory)

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["extract", "agent", "graph_write", "sweep"], rows
    argv = rows[3][1]["argv"]
    kept = [argv[i + 1] for i, part in enumerate(argv) if part == "--keep-commit"]
    # MUTATION: the worker reading index_versions (the PermissionDenied of #1693) again.
    assert kept == sorted({*older, HEAD})
    assert indexrun.REPOSITORIES_COLLECTION not in read
    assert _flag(argv, "--repo-id") == REPO_ID
    assert _flag(argv, "--destination") == f"tenants/{TENANT}/repos/{REPO_ID}/graph"
    sweep = _phases(db)["index_sweep"]
    assert sweep["status"] == "ok", sweep
    # What the sweep did, in the run's phases (D of the lane).
    assert sweep["retention"]["deleted_copies"] == 3
    assert sweep["retention"]["deleted_bytes"] == 7340032
    assert sweep["retention"]["kept_commits"] == 4
    assert sweep["retention"]["held_copies"] == 4
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_without_a_keep_line_retention_is_skipped_in_words(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    _run(db, worker_factory)

    # MUTATION: sweeping with keep=None, or recording nothing.
    assert "sweep" not in [name for name, _ in _order(index_tools)]
    sweep = _phases(db)["index_sweep"]
    assert sweep["status"] == "skipped"
    assert "kept commits" in sweep["reason"]
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_a_failing_sweep_does_not_fail_the_run(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed, monkeypatch
):
    monkeypatch.setenv("FAIL_SWEEP", "1")
    monkeypatch.setattr(indexrun, "PASS_THROUGH_ENV", indexrun.PASS_THROUGH_ENV + ("FAIL_SWEEP",))
    _seed(db, store, kind="full", prompt=_keep_prompt(HEAD))
    _run(db, worker_factory)

    sweep = _phases(db)["index_sweep"]
    # MUTATION: returning an Outcome (a failed run) from a failed sweep.
    assert sweep["status"] == "failed" and "AccessDenied" in sweep["reason"], sweep
    assert _phases(db)["graph_write"]["status"] == "ok"
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_the_sweep_has_its_own_budget_reserved_after_the_write(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full", prompt=_keep_prompt(HEAD))
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=1800,
                                  repository_url=URL)
    budget = indexrun.budgets(1800)
    # MUTATION: no sweep reserve, and the sweep gets only what the agent left.
    assert budget.sweep >= indexrun.INDEX_SWEEP_MIN_SECONDS
    assert worker._deadline - worker._runner_deadline == \
        pytest.approx(budget.graph_write + budget.sweep, abs=0.01)


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
