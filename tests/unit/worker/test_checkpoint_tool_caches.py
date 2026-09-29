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

1. the tool caches under HOME, and `node_modules` ANYWHERE -- inside the
   repository checkout too, the owner's decision of 2026-09-28 on PR #288 --
   are not in the archive, while the agent's work, the checkout and the CLIs'
   session transcripts (`.claude/`, `.codex/`) are;
2. a restore skips a SYMBOLIC link that escapes the workspace, logs its name
   (scrubbed, and at most 20 of them), and restores everything else -- but an
   archive that is inconsistent with itself (a hard link to anything but an
   earlier regular file, a member written through a symlink member, a hard
   link out of the archive) is refused whole, before a byte is written;
3. the lease is heartbeaten while a checkpoint is being written, for at most
   `heartbeat_meanwhile_max_seconds`, and not once the attempt is fenced or
   cancelled; and a checkpoint over its cap is stopped while it is written.

MUTATIONS: drop an entry from `checkpoint.TOOL_CACHES` (test 1 goes red on that
path); keep `node_modules` inside the checkout again (the `repo/node_modules`
assertions go red); raise again for an escaping symlink (test 2 raises); skip
the link without logging (test 2's log assertion); log every skipped link
(the cap test); accept a hard link whose target is not an earlier regular file
member, or drop the through-a-symlink check (the three review probes write
outside `work/` or stop raising); take the heartbeat thread out of
`_checkpoint` (test 3: `heartbeat_at` does not move); drop the bound or the
poll from the thread (the bound and the cancel tests count beats); check the
cap only after the archive is written (the oversized test sees `b.txt` added).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.checkpoint import CheckpointManager, CheckpointRecord, checkpoint_prefix
from agent_worker.errors import CheckpointError, ExitCode
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
    # And INSIDE it: left out too, the owner's decision of 2026-09-28 (PR
    # #288). It is rebuildable by `npm ci` and never the agent's work.
    _write(checkout / "node_modules/dep/index.js")
    _write(checkout / "apps/ui/node_modules/dep/index.js")

    # The agent's work, which must all be there.
    _write(work / "notes.md", "half done\n")
    _write(checkout / "src/app.py", "print('hi')\n")
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
                  "node_modules", "scratch/tool/node_modules",
                  "repo/node_modules", "repo/apps/ui/node_modules"):
        leaked = [n for n in names if n == cache or n.startswith(cache + "/")]
        assert not leaked, f"{cache} was archived: {leaked}"
    for kept in (
        "notes.md",
        "repo/src/app.py",
        "repo/.cache/keep.txt",
        ".claude/projects/-work-repo/session.jsonl",
        ".codex/sessions/2026/09/29/rollout.jsonl",
        ".local/share/other/keep.txt",
    ):
        assert kept in names, f"{kept} is the agent's work and was left out: {names}"
    assert record.file_count == 6, names

    # And the archive restores whole into a fresh workspace.
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, _logger(log_stream), attempt_id="att_2", generation=2)
    found = manager.find_latest()
    assert found is not None
    manager.restore(found, resumed)
    assert (resumed.work / "notes.md").read_text() == "half done\n"
    assert (resumed.checkout() / "src/app.py").exists()
    assert not os.path.lexists(resumed.checkout() / "node_modules")
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


# ---------------------------------------------------------------------------
# 2b. an archive inconsistent with itself is refused whole (the PR #288 review)
# ---------------------------------------------------------------------------


def _sym(name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def _hard(name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    return info, None


def _file(name, body=b"x\n"):
    info = tarfile.TarInfo(name)
    info.size = len(body)
    return info, body


def _dir(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, None


def _crafted(store, tmp_path, logger, members):
    """A checkpoint of THIS task holding exactly `members`, and a fresh
    workspace to restore it into. The archive is written by hand because this
    platform's archiver never writes these shapes: they are what a tampered
    archive, or a future archiver's bug, would hold."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, body in members:
            tar.addfile(info, io.BytesIO(body) if body is not None else None)
    data = buf.getvalue()
    prefix = checkpoint_prefix(
        tenant_id=TENANT, task_id="task_1", attempt_id="att_1", checkpoint_id="ckpt-00001"
    )
    store.upload_bytes(f"{prefix}/archive.tar.gz", data)
    record = CheckpointRecord(
        checkpoint_id="ckpt-00001", seq=1, task_id="task_1", attempt_id="att_1",
        tenant_id=TENANT, generation=1, created_at="2026-09-29T00:00:00Z",
        archive_key=f"{prefix}/archive.tar.gz", manifest_key=f"{prefix}/manifest.json",
        archive_bytes=len(data), archive_sha256=hashlib.sha256(data).hexdigest(),
        file_count=len(members), uri=store.uri(f"{prefix}/"),
    )
    resumed = workspace_mod.create(tmp_path / "ws2", "att_2")
    manager = _manager(store, logger, attempt_id="att_2", generation=2)
    return manager, record, resumed


def _outside_work(ws):
    """Every path under the attempt's root that is not under `work/` or the
    restore staging directory (where the downloaded archive lands), with the
    bytes of each file: what a restore must never change."""
    seen = {}
    for path in sorted(ws.root.rglob("*")):
        if path == ws.work or ws.work in path.parents:
            continue
        if path == ws.restore or ws.restore in path.parents:
            continue
        is_file = path.is_file() and not path.is_symlink()
        seen[str(path)] = path.read_bytes() if is_file else None
    return seen


def _assert_refused(manager, record, resumed):
    (resumed.private / "secret.txt").write_text("orig\n")
    before = _outside_work(resumed)
    with pytest.raises(CheckpointError):
        manager.restore(record, resumed)
    assert _outside_work(resumed) == before, "the restore wrote outside work/"
    assert (resumed.private / "secret.txt").read_text() == "orig\n"
    written = sorted(p.name for p in resumed.work.iterdir())
    assert not written, f"a refused archive wrote into work/: {written}"


def test_a_hard_link_to_a_skipped_escaping_symlink_refuses_the_archive(
    store, tmp_path, log_stream
):
    """Review probe 1: the symlink is skipped, and the hard link to it would
    otherwise be made by tarfile's fallback, which extracts the link's TARGET
    member in its place (the CVE-2025-4330 class); a member is then written
    through it."""
    manager, record, resumed = _crafted(store, tmp_path, _logger(log_stream), [
        _file("notes.md"),
        _sym("esc", "../private"),
        _hard("hl", "esc"),
        _file("hl/pwned", b"pwned\n"),
    ])
    _assert_refused(manager, record, resumed)
    assert not (resumed.private / "pwned").exists()


def test_a_member_written_through_a_symlink_chain_refuses_the_archive(
    store, tmp_path, log_stream
):
    """Review probe 2: `d -> .` stays inside, `c -> d/..` leaves, and `c/x`
    is written through `c`."""
    manager, record, resumed = _crafted(store, tmp_path, _logger(log_stream), [
        _sym("d", "."),
        _sym("c", "d/.."),
        _file("c/x", b"pwned\n"),
    ])
    _assert_refused(manager, record, resumed)
    assert not (resumed.root / "x").exists()


def test_a_parent_relative_hard_link_out_of_the_archive_refuses_it(store, tmp_path, log_stream):
    """Review probe 3: a hard link's target is relative to the ARCHIVE ROOT,
    and `../private/secret.txt` from there is outside; a regular member of the
    same name then writes through it into the file outside."""
    manager, record, resumed = _crafted(store, tmp_path, _logger(log_stream), [
        _file("notes.md"),
        _hard("h", "../private/secret.txt"),
        _file("h", b"pwned\n"),
    ])
    _assert_refused(manager, record, resumed)


def test_an_absolute_hard_link_refuses_the_archive(store, tmp_path, log_stream):
    manager, record, resumed = _crafted(store, tmp_path, _logger(log_stream), [
        _file("kept.txt"),
        _hard("passwd", "/etc/passwd"),
    ])
    _assert_refused(manager, record, resumed)


def test_a_hard_link_to_an_earlier_regular_file_is_restored(store, tmp_path, log_stream):
    """The control: the archiver writes a second name of one inode as a hard
    link to the first, and that is restored as one."""
    manager, record, resumed = _crafted(store, tmp_path, _logger(log_stream), [
        _dir("a"),
        _file("a/one.txt", b"same\n"),
        _hard("a/two.txt", "a/one.txt"),
    ])
    manager.restore(record, resumed)
    one, two = resumed.work / "a/one.txt", resumed.work / "a/two.txt"
    assert two.read_text() == "same\n"
    assert os.stat(one).st_ino == os.stat(two).st_ino


def test_uvs_absolute_python_link_is_skipped_and_the_rest_restores(store, tmp_path, log_stream):
    """The #286 archive: `bin/python -> /usr/local/bin/python3.11`, under a
    path `TOOL_CACHES` now leaves out, in an archive written before it did."""
    manager, record, resumed = _crafted(store, tmp_path, _logger(log_stream), [
        _dir(".cache"),
        _dir(".cache/uv"),
        _dir(".cache/uv/builds-v0"),
        _dir(".cache/uv/builds-v0/.tmp48eWD9"),
        _dir(".cache/uv/builds-v0/.tmp48eWD9/bin"),
        _sym(UV_PYTHON, "/usr/local/bin/python3.11"),
        _file(".cache/uv/builds-v0/.tmp48eWD9/pyvenv.cfg", b"home = /usr/local/bin\n"),
        _file("notes.md", b"half done\n"),
        _sym("notes-link", "notes.md"),
    ])
    manager.restore(record, resumed)
    assert (resumed.work / "notes.md").read_text() == "half done\n"
    assert (resumed.work / ".cache/uv/builds-v0/.tmp48eWD9/pyvenv.cfg").exists()
    assert os.readlink(resumed.work / "notes-link") == "notes.md"
    assert not os.path.lexists(resumed.work / UV_PYTHON)
    assert UV_PYTHON in log_stream.getvalue()


def _records(log_stream):
    return [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]


def test_a_secret_in_a_skipped_links_name_or_target_is_not_logged(store, tmp_path, log_stream):
    secret = "sk-ant-api03-" + "Q7" * 24
    logger = _logger(log_stream)
    logger.register_secret(secret)
    manager, record, resumed = _crafted(store, tmp_path, logger, [
        _file("notes.md"),
        _dir("venv"),
        _sym(f"venv/{secret}", f"/opt/{secret}/python"),
    ])
    manager.restore(record, resumed)
    logged = log_stream.getvalue()
    assert secret not in logged
    assert "venv/" in logged, f"the skipped link is not named at all: {logged}"
    assert (resumed.work / "notes.md").exists()


def test_skipped_links_are_named_at_most_twenty_then_counted(store, tmp_path, log_stream):
    links = [_sym(f"links/l{i:02d}", f"/abs/t{i:02d}") for i in range(25)]
    manager, record, resumed = _crafted(
        store, tmp_path, _logger(log_stream), [_file("notes.md"), _dir("links"), *links]
    )
    manager.restore(record, resumed)
    logged = log_stream.getvalue()
    assert "links/l00" in logged and "links/l19" in logged, logged
    for i in range(20, 25):
        assert f"links/l{i:02d}" not in logged, f"link {i} was named past the cap of 20"
    counts = [r.get("skipped") for r in _records(log_stream) if "skipped" in r]
    assert 25 in counts, f"the 25 skipped links are not counted: {counts}"


# ---------------------------------------------------------------------------
# 2c. a checkpoint over its cap is stopped while it is written
# ---------------------------------------------------------------------------


def test_an_oversized_tree_aborts_the_checkpoint_while_it_is_written(
    store, tmp_path, log_stream, monkeypatch
):
    """The cap was checked on the finished archive, so a tree far over it was
    compressed whole -- on the heartbeat's clock -- before being refused. The
    first file alone is 16 times the cap and incompressible; the second must
    never be reached."""
    ws = workspace_mod.create(tmp_path / "ws", "att_1")
    (ws.work / "a.bin").write_bytes(os.urandom(1024 * 1024))
    _write(ws.work / "b.txt")
    added: list[str] = []
    real_add = CheckpointManager._add_entry

    def recording_add(*args, **kwargs):
        added.append(args[-1])
        return real_add(*args, **kwargs)

    monkeypatch.setattr(CheckpointManager, "_add_entry", staticmethod(recording_add))
    manager = CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id="att_1",
        generation=1, logger=_logger(log_stream), max_bytes=64 * 1024,
    )
    with pytest.raises(CheckpointError):
        manager.create(ws)
    assert "a.bin" in added
    assert "b.txt" not in added, "the checkpoint went on writing past its cap"
    assert not store.list_keys(manager.own_prefix), "an oversized checkpoint was uploaded"


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


def _count_beats(worker):
    beats: list[float] = []
    real = worker._heartbeat

    def counted():
        beats.append(time.monotonic())
        return real()

    worker._heartbeat = counted  # type: ignore[method-assign]
    return beats


def test_the_heartbeat_during_a_checkpoint_stops_at_its_bound(db, worker_factory):
    """A checkpoint that never ends -- a wedged upload -- must not keep the
    lease, and the capacity behind it, alive for ever. The bound here is 2 s
    against a 1 s interval, and the stand-in checkpoint holds for 6 s: an
    unbounded thread beats 5 or 6 times, a bounded one twice."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=1, checkpoint_interval_seconds=60, control_poll_seconds=60,
        heartbeat_meanwhile_max_seconds=2,
    )
    beats = _count_beats(worker)
    real_create = worker.checkpoints.create
    during: list[int] = []

    def slow_create(ws, *, label="periodic"):
        if during:
            return real_create(ws, label=label)
        start = len(beats)
        time.sleep(6)
        during.append(len(beats) - start)
        return real_create(ws, label=label)

    worker.checkpoints.create = slow_create  # type: ignore[method-assign]
    worker.run()
    assert during, "no checkpoint was written"
    assert 1 <= during[0] <= 3, f"{during[0]} heartbeats in 6 s against a 2 s bound"


def test_the_heartbeat_during_a_checkpoint_stops_once_the_task_is_cancelled(db, worker_factory):
    """Cancelled mid-checkpoint: the heartbeat itself still succeeds, so only
    the thread's own poll stops it. At most the beat already in flight lands
    after the cancel."""
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=1, checkpoint_interval_seconds=60, control_poll_seconds=60,
    )
    beats = _count_beats(worker)
    real_create = worker.checkpoints.create
    after_cancel: list[int] = []

    def slow_create(ws, *, label="periodic"):
        if after_cancel:
            return real_create(ws, label=label)
        time.sleep(1.5)
        db.doc("tasks/task_1").update({"cancel_requested": True})
        mark = len(beats)
        time.sleep(4)
        after_cancel.append(len(beats) - mark)
        return real_create(ws, label=label)

    worker.checkpoints.create = slow_create  # type: ignore[method-assign]
    worker.run()
    assert after_cancel, "no checkpoint was written"
    assert after_cancel[0] <= 1, f"{after_cancel[0]} heartbeats in 4 s after the cancel"


def test_the_heartbeat_during_a_checkpoint_stops_once_the_attempt_is_fenced(db, worker_factory):
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory(
        heartbeat_interval_seconds=1, checkpoint_interval_seconds=60, control_poll_seconds=60,
    )
    beats = _count_beats(worker)
    real_create = worker.checkpoints.create
    after_fence: list[int] = []

    def slow_create(ws, *, label="periodic"):
        if after_fence:
            return real_create(ws, label=label)
        time.sleep(1.5)
        db.doc("tasks/task_1").update({"current_generation": 2})
        mark = len(beats)
        time.sleep(4)
        after_fence.append(len(beats) - mark)
        return real_create(ws, label=label)

    worker.checkpoints.create = slow_create  # type: ignore[method-assign]
    worker.run()
    assert after_fence, "no checkpoint was written"
    assert after_fence[0] <= 1, f"{after_fence[0]} heartbeats in 4 s after the fence"


# ---------------------------------------------------------------------------
# 4. the worker refuses to run on a Python without tarfile's extraction filters
# ---------------------------------------------------------------------------


def test_the_checkpoint_module_refuses_a_tarfile_without_data_filter():
    """`restore` extracts through `tarfile.data_filter` (3.11.4 and later).
    On an older interpreter the module must fail at import, naming the cause,
    not at the first resume. MUTATION: make `_require_data_filter` a no-op and
    the stand-in module without the filter is accepted."""
    import types

    from agent_worker.checkpoint import _require_data_filter

    _require_data_filter(tarfile)  # this interpreter has it, or the import above failed

    old = types.SimpleNamespace(FilterError=Exception)  # a 3.11.3 tarfile, as far as this goes
    with pytest.raises(ImportError, match=r"tarfile\.data_filter \(Python 3\.11\.4 or later\)"):
        _require_data_filter(old)
