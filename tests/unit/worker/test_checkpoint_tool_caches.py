"""A resume survives an agent that ran uv or npm (#286).

HOME is `work/` (`workspace.child_env`), and since #261 an agent runs the
offline tests in its container, so uv and npm write their caches into
`work/.cache`, `work/.npm` and the like. Every checkpoint archived them, and
one absolute link among them -- uv's build environment's
`.cache/uv/builds-v0/.tmpX/bin/python -> /usr/local/bin/python3.11` -- made
`_safe_members` refuse the WHOLE archive on restore
(task_c42fb5cfe0db44198b63, 2026-09-29). And the checkpoint ran on the loop
that heartbeats the lease, so a large one delayed the heartbeat; the attempt
was fenced 64 s after `checkpoint_started`.

Three properties, one per defect:

1. the tool caches under HOME are not in the archive, while the agent's work,
   the repository checkout (its own `node_modules` included) and the CLIs'
   session transcripts (`.claude/`, `.codex/`) are;
2. a restore skips a link that escapes the workspace, logs its name, and
   restores everything else;
3. the lease is heartbeaten while a checkpoint is being written.

MUTATIONS: drop an entry from `checkpoint.TOOL_CACHES` (test 1 goes red on that
path); exclude `node_modules` inside the checkout too (the `repo/node_modules`
control goes red); raise again in `_safe_members` for an escaping link (test 2
raises); skip the link without logging (test 2's log assertion); take the
heartbeat thread out of `_checkpoint` (test 3: the lease's `heartbeat_at` does
not move during the checkpoint).
"""

from __future__ import annotations

import io
import os
import tarfile
import time

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger

from conftest import TENANT, seed_attempt

#: What uv's build environment really holds (the #286 archive's refusal).
UV_PYTHON = ".cache/uv/builds-v0/.tmp48eWD9/bin/python"


def _logger(log_stream):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="claude-code", stream=log_stream,
    )


def _manager(store, logger, attempt_id="att_1", generation=1):
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id,
        generation=generation, logger=logger,
    )


def _write(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _names(store, record):
    data = store.download_bytes(record.archive_key)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return archive.getnames()


# ---------------------------------------------------------------------------
# 1. the caches are left out; the work, the checkout and the transcripts stay
# ---------------------------------------------------------------------------


def test_tool_caches_under_home_are_not_archived_and_the_work_is(store, tmp_path, log_stream):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    work = ws.work
    checkout = ws.checkout()

    # The caches, as uv, npm, pnpm and yarn lay them out under HOME.
    (work / UV_PYTHON).parent.mkdir(parents=True)
    os.symlink("/usr/local/bin/python3.11", work / UV_PYTHON)
    _write(work / ".cache/uv/wheels-v1/pypi/pytest/pytest-8.3.3-py3-none-any.whl")
    _write(work / ".cache/pip/http/0/a")
    _write(work / ".npm/_cacache/index-v5/aa/bb")
    _write(work / ".local/share/uv/python/cpython-3.11/bin/python3")
    _write(work / ".local/share/pnpm/store/v3/files/00/abc")
    _write(work / ".yarn/cache/left-pad-npm-1.3.0.zip")
    # node_modules OUTSIDE the checkout: an agent's scratch install.
    _write(work / "node_modules/left-pad/index.js")
    _write(work / "scratch/tool/node_modules/left-pad/index.js")

    # The agent's work, which must all be there.
    _write(work / "notes.md", "half done\n")
    _write(checkout / "src/app.py", "print('hi')\n")
    # The checkout's own node_modules is the repository's tree, not HOME's
    # cache: it is kept (see `checkpoint.TOOL_CACHES`).
    _write(checkout / "node_modules/dep/index.js")
    _write(checkout / "apps/ui/node_modules/dep/index.js")
    # A `.cache` INSIDE the checkout is not HOME's; the rule is HOME-relative.
    _write(checkout / ".cache/keep.txt")
    # The CLIs' session transcripts stay in (a resumed CLI reads them).
    _write(work / ".claude/projects/-work-repo/session.jsonl", '{"turn": 1}\n')
    _write(work / ".codex/sessions/2026/09/29/rollout.jsonl", '{"turn": 1}\n')
    # Other things under .local/share are not a listed cache.
    _write(work / ".local/share/other/keep.txt")

    record = _manager(store, _logger(log_stream)).create(ws)
    names = _names(store, record)

    for cache in (".cache", ".npm", ".local/share/uv", ".local/share/pnpm", ".yarn/cache",
                  "node_modules", "scratch/tool/node_modules"):
        leaked = [n for n in names if n == cache or n.startswith(cache + "/")]
        assert not leaked, f"{cache} was archived: {leaked}"
    for kept in (
        "notes.md",
        "repo/src/app.py",
        "repo/node_modules/dep/index.js",
        "repo/apps/ui/node_modules/dep/index.js",
        "repo/.cache/keep.txt",
        ".claude/projects/-work-repo/session.jsonl",
        ".codex/sessions/2026/09/29/rollout.jsonl",
        ".local/share/other/keep.txt",
    ):
        assert kept in names, f"{kept} is the agent's work and was left out: {names}"
    assert record.file_count == 8, names

    # And the archive restores whole into a fresh workspace.
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, _logger(log_stream), attempt_id="att_2", generation=2)
    found = manager.find_latest()
    assert found is not None
    manager.restore(found, resumed)
    assert (resumed.work / "notes.md").read_text() == "half done\n"
    assert (resumed.checkout() / "node_modules/dep/index.js").exists()
    assert not os.path.lexists(resumed.work / ".cache")


# ---------------------------------------------------------------------------
# 2. an escaping link is skipped and named, never a reason to refuse the rest
# ---------------------------------------------------------------------------


def test_a_restore_skips_a_link_escaping_the_workspace_logs_it_and_restores_the_rest(
    store, tmp_path, log_stream
):
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    _write(ws.work / "notes.md", "half done\n")
    _write(ws.work / "venv/lib/site.py", "pass\n")
    # Outside every cache path, so the archive carries them as the agent made them.
    os.symlink("/usr/local/bin/python3.11", ws.work / "venv/python")
    os.symlink("../../../outside/secret.txt", ws.work / "venv/escape-relative")
    # A link that stays inside is ordinary work and is restored as a link.
    os.symlink("lib/site.py", ws.work / "venv/site-link")

    logger = _logger(log_stream)
    record = _manager(store, logger).create(ws)
    names = _names(store, record)
    assert "venv/python" in names and "venv/escape-relative" in names, names

    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, logger, attempt_id="att_2", generation=2)
    manager.restore(record, resumed)

    assert (resumed.work / "notes.md").read_text() == "half done\n"
    assert (resumed.work / "venv/lib/site.py").read_text() == "pass\n"
    assert os.readlink(resumed.work / "venv/site-link") == "lib/site.py"
    assert not os.path.lexists(resumed.work / "venv/python"), "an absolute link was made"
    assert not os.path.lexists(resumed.work / "venv/escape-relative"), "an escaping link was made"
    logged = log_stream.getvalue()
    assert "venv/python" in logged, f"the skipped absolute link is not named: {logged}"
    assert "venv/escape-relative" in logged, f"the skipped relative link is not named: {logged}"


def test_a_hard_link_escaping_the_workspace_is_skipped_too(store, tmp_path, log_stream):
    """A hard link to a file outside is still a link out; the rest restores."""
    from agent_worker.checkpoint import _safe_members

    destination = tmp_path / "att" / "work"
    destination.mkdir(parents=True)
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        body = b"kept\n"
        info = tarfile.TarInfo("kept.txt")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        hard = tarfile.TarInfo("passwd")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "/etc/passwd"
        tar.addfile(hard)

    skipped: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        members = _safe_members(tar, destination, on_skip=lambda name, link: skipped.append(name))
    assert [m.name for m in members] == ["kept.txt"]
    assert skipped == ["passwd"]


# ---------------------------------------------------------------------------
# 3. the lease is heartbeaten while a checkpoint is written
# ---------------------------------------------------------------------------


def test_the_lease_is_heartbeaten_while_a_checkpoint_is_written(db, worker_factory):
    """A checkpoint of a large tree ran on the loop that heartbeats the lease,
    so the lease aged for as long as the archive and the upload took. The
    stand-in `create` holds for 2.5 heartbeat intervals; the lease's
    `heartbeat_at` must move while it does."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=1, checkpoint_interval_seconds=60, control_poll_seconds=60,
    )
    real_create = worker.checkpoints.create
    seen: list[tuple[str, object, object]] = []

    def slow_create(ws, *, label="periodic"):
        before = db.doc("leases/lease_1").get("heartbeat_at")
        time.sleep(2.5)
        after = db.doc("leases/lease_1").get("heartbeat_at")
        seen.append((label, before, after))
        return real_create(ws, label=label)

    worker.checkpoints.create = slow_create  # type: ignore[method-assign]

    assert worker.run() == ExitCode.OK
    assert seen, "no checkpoint was written"
    for label, before, after in seen:
        assert after is not None and after != before, (
            f"the {label} checkpoint held the lease's heartbeat for 2.5 intervals"
        )
