"""A step clones a known sha from its tenant's bundle, and writes one when it cloned first (#940).

MEASURED (#721): a step's clone spent a median 36.6 s (p90 69.5 s) in the TCP
connect to GitHub, against 1.7 s of transfer. A step whose commit is already
known -- a workflow step's base pin, a carried parent head -- takes it from a
one-commit git bundle in its own tenant's prefix instead, and GitHub is off
its start path. A branch-tip step seeds from the branch's last bundle and
fetches only the delta. Any miss or bundle error is today's clone.

Pinned here, through `Worker._clone_keeping_lease` (and `run()` where the
step's outcome is the point):

* a pinned step with a bundle checks out the same commit without one forge
  call; with none, or a corrupt one, it clones exactly as today -- and a
  carried pin still never takes the branch tip;
* a branch-tip step seeds from the head pointer's bundle and its only forge
  call is the delta fetch;
* the first clone of a sha writes its bundle (write-once) and head pointer;
  the second reads it and writes nothing; an upload failure never fails the
  step; an index run neither reads nor writes;
* `clone_timed` carries `source` and `bundle`, and its `seconds` and
  `total_seconds` include the download;
* every key the worker touches is under `tenants/<its own tenant>/bundles/`
  (invariant 9).

Offline: the repository is a local bare one, reached through a git wrapper
that rewrites the github.com URL to it with `url.<file>.insteadOf` and records
every call that would have gone to the forge.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from agent_worker import clonebundle, gitops, lifecycle
from agent_worker import workspace as workspace_mod
from agent_worker.errors import ExitCode, WorkerError
from agent_worker.logs import build_logger
from agent_worker.objectstore import LocalObjectStore
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
pytestmark = needs_git

URL = "https://github.com/acme/widgets.git"


def _fake_token() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "ghp_" + "x" * 36


# ---------------------------------------------------------------------------
# a forge on disk, and a git that reaches it as github.com and says when
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


class Forge:
    """A bare repository standing in for github.com/acme/widgets."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.seed = root / "seed"
        self.bare = root / "origin.git"
        self.calls = root / "forge-calls.jsonl"
        self.seed.mkdir(parents=True)
        _git(self.seed, "init", "--quiet", "--initial-branch=main", ".")
        self.commit("base")
        subprocess.run(["git", "clone", "--quiet", "--bare", str(self.seed), str(self.bare)],
                       check=True, capture_output=True)
        _git(self.seed, "remote", "add", "origin", str(self.bare))

    def commit(self, text: str, branch: str = "main") -> str:
        _git(self.seed, "checkout", "--quiet", "-B", branch)
        (self.seed / "README.md").write_text(text + "\n")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "--quiet", "-m", text)
        return _git(self.seed, "rev-parse", "HEAD")

    def push(self, branch: str = "main") -> None:
        _git(self.seed, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}")

    def tip(self, branch: str = "main") -> str:
        return _git(self.bare, "rev-parse", f"refs/heads/{branch}")

    def forge_calls(self) -> list[list[str]]:
        """Every git call that went to the forge: a clone or fetch of `origin` or its URL."""
        if not self.calls.exists():
            return []
        calls = [json.loads(line) for line in self.calls.read_text().splitlines() if line]
        return [
            argv for argv in calls
            if any(verb in argv for verb in ("clone", "fetch", "ls-remote"))
            and any(arg == "origin" or arg == URL for arg in argv)
        ]


_WRAPPER = """\
#!{python}
import json, os, sys
argv = sys.argv[1:]
with open({calls!r}, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
git = {git!r}
os.execv(git, [git, "-c", "url.file://{bare}.insteadOf={url}"] + argv)
"""


@pytest.fixture
def forge(tmp_path: Path, monkeypatch) -> Forge:
    forge = Forge(tmp_path / "forge")
    wrapper = tmp_path / "forge-git"
    wrapper.write_text(_WRAPPER.format(
        python=sys.executable, calls=str(forge.calls), git=shutil.which("git"),
        bare=str(forge.bare), url=URL,
    ))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for name in ("shallow_clone", "clone_at_commit", "clone_from_bundle",
                 "fetch_tip_onto_bundle", "write_clone_bundle"):
        real = getattr(gitops, name)
        monkeypatch.setattr(
            lifecycle, name,
            lambda real=real, **kwargs: real(**kwargs, git_binary=str(wrapper)),
            # Absent from a lifecycle that predates bundles: the red run fails
            # on what the step did, not on this fixture.
            raising=False,
        )
    return forge


def _logger():
    import io

    return build_logger(task_id="t", attempt_id="a", tenant_id=TENANT, generation=1,
                        runner_profile="mock", stream=io.StringIO())


def _publish_bundle_of_tip(store, forge: Forge, tmp_path: Path, *, tenant: str = TENANT,
                           branch: str = "main", head: bool = False) -> str:
    """Bundle the forge's current tip as an earlier step would have; its sha."""
    scratch = tmp_path / f"seed-bundle-{time.monotonic_ns()}"
    logs = scratch / "logs"
    logs.mkdir(parents=True)
    url = f"file://{forge.bare}"
    real_validate = gitops.validate_repository_url
    gitops.validate_repository_url = lambda u: u  # type: ignore[assignment]
    try:
        clone = gitops.shallow_clone(url=url, ref=branch, destination=scratch / "repo",
                                     private_dir=scratch / "p", logs_dir=logs,
                                     timeout_seconds=30, logger=_logger())
    finally:
        gitops.validate_repository_url = real_validate  # type: ignore[assignment]
    out = scratch / "b.swarm-clone.bundle"
    gitops.write_clone_bundle(clone=scratch / "repo", commit=clone.commit, out=out,
                              private_dir=scratch / "p", logs_dir=logs, timeout_seconds=30,
                              logger=_logger())
    store.upload_file(clonebundle.bundle_key(tenant, URL, clone.commit), out)
    if head:
        clonebundle.publish_head(store, clonebundle.head_key(tenant, URL, branch), clone.commit)
    return clone.commit


class RecordingStore(LocalObjectStore):
    """The test's object store, recording every key the worker touched."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.touched: list[tuple[str, str]] = []
        self.download_delay = 0.0
        self.fail_uploads = False

    def download_file(self, key, destination):
        self.touched.append(("download_file", key))
        time.sleep(self.download_delay)
        return super().download_file(key, destination)

    def download_bytes(self, key):
        self.touched.append(("download_bytes", key))
        return super().download_bytes(key)

    def upload_file_if_absent(self, key, source, content_type=None):
        self.touched.append(("upload_file_if_absent", key))
        if self.fail_uploads:
            raise RuntimeError("the bucket said no")
        return super().upload_file_if_absent(key, source, content_type)

    def upload_bytes(self, key, data, content_type=None):
        self.touched.append(("upload_bytes", key))
        return super().upload_bytes(key, data, content_type)

    def exists(self, key):
        self.touched.append(("exists", key))
        return super().exists(key)

    def bundle_touches(self) -> list[tuple[str, str]]:
        return [(op, key) for op, key in self.touched if "/bundles/" in key
                or key.endswith((clonebundle.BUNDLE_SUFFIX, clonebundle.HEAD_SUFFIX))]


@pytest.fixture
def store(tmp_path: Path) -> RecordingStore:  # the conftest's, recording
    from worker_seeds import BUCKET

    return RecordingStore(tmp_path / "gcs", bucket=BUCKET)


def _worker(db, worker_factory, monkeypatch, **kwargs):
    seed_attempt(db)
    kwargs.setdefault("repository_url", URL)
    kwargs.setdefault("repository_ref", "main")
    worker, config, _ = worker_factory(timeout_seconds=600, **kwargs)
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": "task_1"}
    monkeypatch.setattr(worker, "_git_token", _fake_token)
    worker._heartbeat()
    return worker


def _pin(monkeypatch, worker, sha: str) -> None:
    monkeypatch.setattr(
        worker, "_upstream_base_pin",
        lambda task: (sha, {"pinned": True, "sha": sha, "from": ["task_up"]}),
    )


def _clone_marks(db) -> list[dict]:
    return [
        event["detail"]["clone"] for event in db.events("task_1")
        if (event.get("detail") or {}).get("cause") == "clone_timed"
    ]


def _head(worker) -> str:
    return _git(worker.ws.work / lifecycle.REPO_DIR_NAME, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# a known sha: from the bundle, or exactly as today
# ---------------------------------------------------------------------------


def test_a_pinned_step_with_a_bundle_clones_the_same_commit_without_contacting_the_forge(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    """MUTATION: drop the bundle read and the clone fetches the sha from the forge."""
    pinned = _publish_bundle_of_tip(store, forge, tmp_path)
    forge.commit("moved on")
    forge.push()
    worker = _worker(db, worker_factory, monkeypatch)
    _pin(monkeypatch, worker, pinned)

    info = worker._clone_keeping_lease(worker._task)
    worker._join_clone_bundle_upload()

    assert forge.forge_calls() == [], "a bundled pin contacted the forge"
    assert info["commit"] == pinned and _head(worker) == pinned
    assert worker._clone_commit == pinned and worker._clone_base == pinned
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    assert _git(repo, "remote", "get-url", "origin") == URL
    (mark,) = _clone_marks(db)
    assert mark["source"] == "bundle" and mark["bundle"]["hit"] is True
    assert mark["pinned"] is True and mark["ok"] is True
    # Nothing written: the bundle of this sha is the one just read.
    assert mark["bundle"]["written"] is False
    assert not [t for t in store.touched if t[0] == "upload_file_if_absent"]


def test_a_missing_bundle_falls_back_to_todays_pinned_clone(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    pinned = forge.tip()
    forge.commit("moved on")
    forge.push()
    worker = _worker(db, worker_factory, monkeypatch)
    _pin(monkeypatch, worker, pinned)

    info = worker._clone_keeping_lease(worker._task)
    worker._join_clone_bundle_upload()

    assert info["commit"] == pinned and _head(worker) == pinned
    assert forge.forge_calls(), "the fallback never reached the forge"
    (mark,) = _clone_marks(db)
    assert mark["source"] == "forge" and mark["pinned"] is True
    assert mark["bundle"]["hit"] is False and mark["bundle"]["miss_reason"] == "miss"
    # Looked for first, under its own tenant's key.
    assert ("download_file", clonebundle.bundle_key(TENANT, URL, pinned)) in store.touched


def test_a_corrupt_bundle_falls_back_and_the_carried_pin_still_never_takes_the_tip(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    branch = "swarm/task_parent"
    head = forge.commit("the parent's head", branch=branch)
    forge.push(branch)
    store.upload_bytes(clonebundle.bundle_key(TENANT, URL, head), b"not a bundle at all\n")
    worker = _worker(db, worker_factory, monkeypatch)
    monkeypatch.setattr(worker, "_carrier_parent", lambda task, url: ("task_parent", head))

    info = worker._clone_keeping_lease(worker._task)

    assert info["commit"] == head and info["carried_head"] == head
    (mark,) = _clone_marks(db)
    assert mark["source"] == "forge"
    assert mark["bundle"]["hit"] is False and mark["bundle"]["miss_reason"] == "bundle_error"

    # And a carried head the forge no longer has, with a corrupt bundle of it:
    # the step fails, as today, rather than cloning the branch's tip.
    gone = "f" * 40
    store.upload_bytes(clonebundle.bundle_key(TENANT, URL, gone), b"garbage\n")
    forge.commit("someone else's push", branch=branch)
    forge.push(branch)
    db2_worker = _worker(db, worker_factory, monkeypatch)
    monkeypatch.setattr(db2_worker, "_carrier_parent", lambda task, url: ("task_parent", gone))
    with pytest.raises(WorkerError, match="could not be fetched"):
        db2_worker._clone_keeping_lease(db2_worker._task)


# ---------------------------------------------------------------------------
# a branch tip: the last bundle plus the delta
# ---------------------------------------------------------------------------


def test_a_branch_tip_step_seeds_from_the_last_bundle_and_fetches_only_the_delta(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    """MUTATION: skip the head pointer and the step makes a full forge clone."""
    _publish_bundle_of_tip(store, forge, tmp_path, head=True)
    tip = forge.commit("newer")
    forge.push()
    worker = _worker(db, worker_factory, monkeypatch)

    info = worker._clone_keeping_lease(worker._task)
    worker._join_clone_bundle_upload()

    assert info["commit"] == tip and _head(worker) == tip
    calls = forge.forge_calls()
    assert len(calls) == 1 and "fetch" in calls[0] and "clone" not in calls[0], calls
    assert "--negotiation-tip=HEAD" in calls[0]
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    (mark,) = _clone_marks(db)
    assert mark["source"] == "bundle+delta" and mark["bundle"]["hit"] is True
    assert mark["pinned"] is False and mark["tries"] == 1
    # The new tip is bundled in turn, and the branch's pointer moved to it.
    assert mark["bundle"]["written"] is True
    assert store.exists(clonebundle.bundle_key(TENANT, URL, tip))
    assert clonebundle.read_head(store, clonebundle.head_key(TENANT, URL, "main")) == tip


# ---------------------------------------------------------------------------
# writing: once per sha, never failing the step, never for an index run
# ---------------------------------------------------------------------------


def test_the_first_clone_of_a_sha_writes_its_bundle_and_the_second_does_not(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    tip = forge.tip()
    first = _worker(db, worker_factory, monkeypatch)
    first._clone_keeping_lease(first._task)
    first._join_clone_bundle_upload()

    key = clonebundle.bundle_key(TENANT, URL, tip)
    assert store.exists(key)
    assert clonebundle.read_head(store, clonebundle.head_key(TENANT, URL, "main")) == tip
    assert _clone_marks(db)[-1]["bundle"]["written"] is True
    assert _clone_marks(db)[-1]["source"] == "forge"
    # No forge token anywhere in what was written.
    raw = (store.root / key).read_bytes()
    assert raw and _fake_token().encode() not in raw
    written_at = (store.root / key).stat().st_mtime_ns

    before = len(forge.forge_calls())
    second = _worker(db, worker_factory, monkeypatch)
    _pin(monkeypatch, second, tip)
    second._clone_keeping_lease(second._task)
    second._join_clone_bundle_upload()

    mark = _clone_marks(db)[-1]
    assert mark["source"] == "bundle" and mark["bundle"]["written"] is False
    assert len(forge.forge_calls()) == before
    assert [k for op, k in store.touched if op == "upload_file_if_absent"] == [key]
    assert (store.root / key).stat().st_mtime_ns == written_at


def test_a_bundle_upload_failure_never_fails_the_step(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    seed_attempt(db)
    store.fail_uploads = True
    worker, _, _ = worker_factory(repository_url=URL, repository_ref="main")
    monkeypatch.setattr(worker, "_git_token", _fake_token)
    worker.egress_connect = lambda target, timeout=None: types.SimpleNamespace(
        close=lambda: None, getpeername=lambda: ("192.0.2.10", 443))

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    assert ("upload_file_if_absent", clonebundle.bundle_key(TENANT, URL, forge.tip())) \
        in store.touched


def test_an_index_run_neither_reads_nor_writes_a_bundle(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    _publish_bundle_of_tip(store, forge, tmp_path, head=True)
    forge.commit("newer")
    forge.push()
    store.touched.clear()
    worker = _worker(db, worker_factory, monkeypatch, runner_profile="indexer")

    info = worker._clone_keeping_lease(worker._task)
    worker._join_clone_bundle_upload()

    assert info["commit"] == forge.tip()
    assert store.bundle_touches() == []
    (mark,) = _clone_marks(db)
    assert mark["source"] == "forge" and mark["bundle"]["written"] is False
    assert mark["bundle"]["miss_reason"] == "index_run"


# ---------------------------------------------------------------------------
# what clone_timed says
# ---------------------------------------------------------------------------


def test_clone_timed_records_source_bundle_and_total_seconds_including_the_download(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    pinned = _publish_bundle_of_tip(store, forge, tmp_path)
    size = (store.root / clonebundle.bundle_key(TENANT, URL, pinned)).stat().st_size
    store.download_delay = 0.4
    worker = _worker(db, worker_factory, monkeypatch)
    _pin(monkeypatch, worker, pinned)

    worker._clone_keeping_lease(worker._task)

    (mark,) = _clone_marks(db)
    bundle = mark["bundle"]
    assert set(bundle) >= {"hit", "download_seconds", "bytes", "written", "miss_reason"}
    assert bundle["hit"] is True and bundle["bytes"] == size and bundle["miss_reason"] is None
    assert bundle["download_seconds"] >= 0.4
    assert mark["total_seconds"] >= bundle["download_seconds"]
    assert mark["seconds"] >= bundle["download_seconds"]
    assert mark["total_seconds"] > bundle["download_seconds"], "the git time is not in it"
    # Every field earlier readers read, still there.
    for name in ("tries", "pinned", "try_log", "ok", "probe_peer", "git_peer", "peer_pinned"):
        assert name in mark, name
    assert mark["tries"] == 0 and mark["try_log"] == []


# ---------------------------------------------------------------------------
# invariant 9: the worker's own tenant only
# ---------------------------------------------------------------------------


def test_the_bundle_is_read_and_written_only_under_the_workers_own_tenant_prefix(
    db, store, worker_factory, monkeypatch, tmp_path, forge
):
    """Another tenant's bundle of the very same commit is never read: the key
    is built from the worker's own tenant id, never the task's input."""
    pinned = _publish_bundle_of_tip(store, forge, tmp_path, tenant="other", head=True)
    store.touched.clear()
    worker = _worker(db, worker_factory, monkeypatch)
    _pin(monkeypatch, worker, pinned)

    info = worker._clone_keeping_lease(worker._task)
    worker._join_clone_bundle_upload()

    assert info["commit"] == pinned
    (mark,) = _clone_marks(db)
    assert mark["source"] == "forge" and mark["bundle"]["hit"] is False
    touched = store.bundle_touches()
    assert touched, "the worker looked for no bundle at all"
    assert all(key.startswith(f"tenants/{TENANT}/bundles/") for _, key in touched), touched
    # And it wrote its own, under its own prefix.
    assert store.exists(clonebundle.bundle_key(TENANT, URL, pinned))
