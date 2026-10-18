"""A checkpoint never carries the additional checkouts (MR-WORKER, #1434).

docs/design/multi-repository-steps.md §4.6: `work/repos/` holds one link per
additional repository, each to its checkout on the cache volume. Once the
lifecycle has made them, every checkpoint leaves `work/repos` out
(`checkpoint.EXTRA_REPOS_DIR`): a resumed attempt clones the same signed
commits again, so an archive never carries several codebases and a restore
never brings back one an agent edited. A task with none keeps an agent's own
`repos/` folder in its checkpoints.

MUTATIONS: drop the `skip` entry in `CheckpointManager.create` -- the archive
holds `repos/web`. Make the exclusion unconditional -- the control test loses
the agent's folder.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from typing import Any

import pytest

from agent_worker import checkpoint

from test_additional_repositories_clone import (
    entry,
    make_bare,
    make_worker,
    redirect_clones,
)
from test_strategy_end_to_end import local_urls  # noqa: F401 -- fixture

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _names(store: Any, record: Any) -> list[str]:
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return archive.getnames()


def test_a_checkpoint_archive_has_no_repos_member(
    worker_factory, monkeypatch, store, tmp_path, local_urls
):
    web, web_shas = make_bare(tmp_path, "web")
    redirect_clones(monkeypatch, {"web": web})
    task = {"task_id": "task_1", "additional_repositories": [entry("web", web_shas[-1])]}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)
    worker._clone_additional_keeping_lease(task)
    (worker.ws.work / "notes.md").write_text("the agent's notes\n")

    first = worker.checkpoints.create(worker.ws)
    (worker.ws.work / "more.md").write_text("later\n")
    # The chain's next link walks the same way.
    second = worker.checkpoints.create(worker.ws)

    for record in (first, second):
        names = _names(store, record)
        assert not [n for n in names if n == "repos" or n.startswith("repos/")], names
    assert "notes.md" in _names(store, first)
    assert "more.md" in _names(store, second)
    assert checkpoint.EXTRA_REPOS_DIR == "repos"


def test_a_task_without_them_keeps_an_agents_own_repos_folder(
    worker_factory, store, tmp_path
):
    task = {"task_id": "task_1"}
    worker, _ = make_worker(worker_factory, tmp_path, task=task)
    worker._clone_additional_keeping_lease(task)
    (worker.ws.work / "repos").mkdir()
    (worker.ws.work / "repos" / "own.md").write_text("the agent's own work\n")

    names = _names(store, worker.checkpoints.create(worker.ws))

    assert "repos/own.md" in names, names
