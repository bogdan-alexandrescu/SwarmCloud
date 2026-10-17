"""An indexer checkpoint carries the graph, not the read-only checkout (INDEX-CKPT).

Measured 2026-10-16 on task_6bf9dc7cc95544f59f0f (attempt
att_03d0c976c35242a39874): its periodic ckpt-00005 was 417,940,037 bytes,
5,429 files, ~36 s to upload, and most of it was `repo/` -- `.git/objects`
259.7 MB, `docs/design` 134.6 MB, `web` 45 MB, `api` 27 MB. The indexer only
reads the checkout, and the checkout is reproducible from the manifest's
`clone_base`; what a resumed attempt needs is the graph beside it (owner
decision 2026-10-17).

Pinned here:

* the indexer's checkpoints leave `repo/` out, `.git` and all, and keep
  everything else in `work/` (the graph files, the phases file, the
  clone-base marker), through the build-directory filter every checkpoint
  already uses;
* a resumed indexer attempt whose checkpoint holds no checkout clones again,
  through the same clone call a first attempt makes, at the checkpoint's
  `clone_base`, with the history window and the token read at the clone;
* a checkpoint from before the change (with `repo/` in it) still restores
  the checkout and skips the clone;
* claude-code and every other profile still checkpoint the checkout and
  clone as before.
"""

from __future__ import annotations

import io
import secrets
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import indexrun, lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.gitops import CloneResult

from worker_seeds import seed_attempt

URL = "https://github.com/acme/widgets.git"
#: The commit the first attempt cloned, which the manifest records.
CLONE_BASE = "b3c11a81" + "0" * 32
#: Where `repository_ref` points by the time a retry runs: not the base.
MOVED = "c" * 40


def _names(store: Any, record: Any) -> list[str]:
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return archive.getnames()


def _worker(
    db: Any, worker_factory: Any, monkeypatch: pytest.MonkeyPatch, *, profile: str,
    attempt_id: str = "att_1", token: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """A worker whose clone is recorded and lays down a small checkout."""
    seen: list[dict[str, Any]] = []

    def clone(**kwargs: Any) -> CloneResult:
        seen.append(kwargs)
        destination = Path(kwargs["destination"])
        (destination / ".git" / "objects").mkdir(parents=True, exist_ok=True)
        (destination / ".git" / "objects" / "pack").write_bytes(b"p" * 64)
        (destination / "docs").mkdir(exist_ok=True)
        (destination / "docs" / "design.md").write_text("design\n")
        return CloneResult(path=destination, url=kwargs["url"], ref=kwargs["ref"],
                           commit=CLONE_BASE, duration_seconds=0.1)

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    seed_attempt(db, runner_profile=profile, attempt_id=attempt_id)
    worker, config, _ = worker_factory(
        repository_url=URL, repository_ref=MOVED, runner_profile=profile,
        attempt_id=attempt_id, timeout_seconds=3600,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": "task_1"}
    monkeypatch.setattr(worker, "_git_token", lambda: token)
    worker._deadline = time.monotonic() + 3600
    return worker, seen


def _write_graph(ws: Any) -> None:
    (ws.work / indexrun.GRAPH_FILE).write_text('{"nodes": []}\n')
    (ws.work / indexrun.BASE_GRAPH_FILE).write_text('{"nodes": []}\n')
    (ws.work / indexrun.PHASES_FILE).write_text("[]\n")


# ---------------------------------------------------------------------------
# 1. what an indexer checkpoint holds
# ---------------------------------------------------------------------------


def test_an_indexer_checkpoint_leaves_the_checkout_out_and_keeps_the_graph(
    db, store, worker_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop the indexer's entry from `indexrun.checkpoint_excludes`
    (or stop `_maybe_clone` handing it to the checkpoints) and `repo/` and
    `repo/.git` are archived again."""
    worker, _ = _worker(db, worker_factory, monkeypatch, profile="indexer",
                        token=secrets.token_hex(20))
    worker._clone_keeping_lease(worker._task)
    _write_graph(worker.ws)

    names = _names(store, worker.checkpoints.create(worker.ws))

    assert not [n for n in names if n == "repo" or n.startswith("repo/")], names
    for kept in (indexrun.GRAPH_FILE, indexrun.BASE_GRAPH_FILE, indexrun.PHASES_FILE,
                 ".swarm/clone-base"):
        assert kept in names, (kept, names)


@pytest.mark.parametrize("profile", ["claude-code", "mock"])
def test_every_other_profile_still_checkpoints_the_checkout(
    db, store, worker_factory, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    worker, _ = _worker(db, worker_factory, monkeypatch, profile=profile,
                        token=secrets.token_hex(20))
    worker._clone_keeping_lease(worker._task)

    names = _names(store, worker.checkpoints.create(worker.ws))

    assert "repo/.git/objects/pack" in names, names
    assert "repo/docs/design.md" in names, names


def test_the_exclusion_is_the_build_directory_filters_and_exact_by_path(
    store, tmp_path: Path, log_stream
) -> None:
    """One exclusion path: `excluded_dirs` names `work/`-relative directories,
    and only those -- a `repo` deeper down is somebody's file, and stays."""
    from agent_worker.logs import build_logger

    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "repo" / ".git").mkdir(parents=True)
    (ws.work / "repo" / "a.txt").write_text("a\n")
    (ws.work / "notes" / "repo").mkdir(parents=True)
    (ws.work / "notes" / "repo" / "b.txt").write_text("b\n")
    manager = CheckpointManager(
        store=store, tenant_id="eng", task_id="task_1", attempt_id="att_1", generation=1,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id="eng",
                            generation=1, runner_profile="indexer", stream=log_stream),
    )
    manager.excluded_dirs = indexrun.checkpoint_excludes(indexrun.INDEXER_PROFILE)

    names = _names(store, manager.create(ws))

    assert not [n for n in names if n == "repo" or n.startswith("repo/")], names
    assert "notes/repo/b.txt" in names, names
    assert indexrun.checkpoint_excludes("claude-code") == frozenset()


# ---------------------------------------------------------------------------
# 2. a resumed indexer attempt clones again at the checkpoint's clone_base
# ---------------------------------------------------------------------------


def test_a_resumed_indexer_clones_again_at_the_checkpoints_clone_base(
    db, store, worker_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop `indexrun.resume_clone_ref` from `_maybe_clone`'s ref
    and the retry clones `repository_ref`, wherever it has moved since."""
    first, _ = _worker(db, worker_factory, monkeypatch, profile="indexer",
                       token=secrets.token_hex(20))
    first._clone_keeping_lease(first._task)
    _write_graph(first.ws)
    record = first.checkpoints.create(first.ws)
    assert record.clone_base == CLONE_BASE

    token = secrets.token_hex(20)
    second, seen = _worker(db, worker_factory, monkeypatch, profile="indexer",
                           attempt_id="att_2", token=token)
    second.checkpoints.restore(record, second.ws)
    second._restored_from = record
    assert not (second.ws.work / "repo").exists()
    assert (second.ws.work / indexrun.GRAPH_FILE).is_file()

    info = second._clone_keeping_lease(second._task)

    assert len(seen) == 1, seen
    assert seen[0]["ref"] == CLONE_BASE
    assert seen[0]["history_days"] == indexrun.HISTORY_DAYS
    # The token the first attempt's path reads, at the clone, handed to git's
    # credential file by `shallow_clone` and nowhere else.
    assert seen[0]["token"] == token
    assert info is not None and info["commit"] == CLONE_BASE
    assert not info.get("from_checkpoint")
    assert (second.ws.work / "repo" / "docs" / "design.md").is_file()
    assert second._publish_base == CLONE_BASE
    assert second.checkpoints.clone_base == CLONE_BASE
    assert token not in (second.ws.work / ".swarm" / "clone-base").read_text()


def test_a_checkpoint_from_before_the_change_still_restores_the_checkout(
    db, store, worker_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An indexer checkpoint written with `repo/` inside (the manager with no
    exclusion, as every one was before) restores it, and the clone is skipped."""
    first, _ = _worker(db, worker_factory, monkeypatch, profile="indexer",
                       token=secrets.token_hex(20))
    first._clone_keeping_lease(first._task)
    first.checkpoints.excluded_dirs = frozenset()
    record = first.checkpoints.create(first.ws)
    assert "repo/docs/design.md" in _names(store, record)

    second, seen = _worker(db, worker_factory, monkeypatch, profile="indexer",
                           attempt_id="att_2", token=secrets.token_hex(20))
    second.checkpoints.restore(record, second.ws)
    second._restored_from = record

    info = second._clone_keeping_lease(second._task)

    assert seen == []
    assert info is not None and info["from_checkpoint"] is True
    assert info["commit"] == CLONE_BASE
    assert (second.ws.work / "repo" / "docs" / "design.md").is_file()


def test_another_profile_resuming_without_a_checkout_clones_its_ref_as_before(
    db, store, worker_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, seen = _worker(db, worker_factory, monkeypatch, profile="claude-code",
                           token=secrets.token_hex(20))
    record = CheckpointManager(
        store=store, tenant_id=worker.cfg.tenant_id, task_id="task_1", attempt_id="att_0",
        generation=1, logger=worker.log,
    )
    record.clone_base = CLONE_BASE
    (worker.ws.work / "notes.md").write_text("n\n")
    worker._restored_from = record.create(worker.ws)

    worker._clone_keeping_lease(worker._task)

    assert len(seen) == 1
    assert seen[0]["ref"] == MOVED
    assert seen[0]["history_days"] is None


def test_resume_clone_ref_is_the_indexers_and_a_full_sha_only() -> None:
    assert indexrun.resume_clone_ref("indexer", CLONE_BASE) == CLONE_BASE
    assert indexrun.resume_clone_ref("claude-code", CLONE_BASE) is None
    assert indexrun.resume_clone_ref("indexer", None) is None
    assert indexrun.resume_clone_ref("indexer", "empty") is None
    assert indexrun.resume_clone_ref("indexer", CLONE_BASE[:12]) is None
