"""The attempt's npm cache is seeded from the image's (AU8-SEED, part of #1933).

AU8-IMAGE-NPM (PR 1949) put a read-only npm cache, warmed from
`apps/swarm-ui/package-lock.json`, into `agent-runtime-base` at
`/opt/npm-cache` (about 52 MB). It measured that no agent's npm reads it: the
worker sets `npm_config_cache` to the attempt's own writable cache
(`cachedirs.CACHE_SUBDIRS`, CACHE-SCRATCH), which overrides every npmrc; and
npm 11.19.0 cannot install from a read-only cache when any package is missing
(EACCES on `_cacache/tmp`, no network fallback). Owner decision 2026-10-22:
"Seed the attempt cache from the image".

What holds now, one test per property:

1. after the seed the attempt's npm cache holds the image cache's entries, in
   the same `_cacache` layout, and every directory and file in it is writable,
   although the image copy is read-only;
2. `npm_config_cache` still names the attempt's own cache, never the image's;
3. an absent or empty image cache changes nothing;
4. a second seed into a cache that already holds content copies nothing;
5. a copy that fails leaves an empty, writable npm cache and does not raise.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from agent_worker import cachedirs
from agent_worker import workspace as workspace_mod
from agent_worker.cachedirs import CACHE_SUBDIRS, seed_npm_cache
from agent_worker.logs import build_logger

#: A fake `_cacache`: an index entry, two content blobs, and npm's tmp dir.
IMAGE_FILES = {
    "_cacache/index-v5/1a/2b/abcdef": b'{"key":"make-fetch-happen:request-cache:x"}\n',
    "_cacache/content-v2/sha512/aa/bb/ccdd": b"tarball-one" * 100,
    "_cacache/content-v2/sha512/ee/ff/0011": b"tarball-two" * 50,
}
IMAGE_BYTES = sum(len(v) for v in IMAGE_FILES.values())


def _logger():
    stream = io.StringIO()
    log = build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="tenant-a", generation=1,
        runner_profile="claude-code", stream=stream,
    )
    return log, stream


def _records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o644)


@pytest.fixture
def image_cache(tmp_path):
    """A read-only image cache, as the Dockerfile leaves `/opt/npm-cache`."""
    root = tmp_path / "opt-npm-cache"
    for rel, data in IMAGE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "_cacache" / "tmp").mkdir()
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o444)
        os.chmod(dirpath, 0o555)
    yield root
    _make_writable(root)


def _npm(ws) -> Path:
    return ws.cache / CACHE_SUBDIRS["npm_config_cache"]


def _writable(path: Path) -> bool:
    # Mode bits, not os.access: the container's tests can run as root, which
    # writes through a read-only mode and would hide the defect.
    return bool(path.stat().st_mode & stat.S_IWUSR)


# ---------------------------------------------------------------------------
# 1. the entries arrive, in the same layout, writable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["gke", "cloud-run"])
def test_the_attempt_cache_holds_the_image_entries_and_stays_writable(
    tmp_path, image_cache, backend
):
    cache_root = tmp_path / "cache-volume" if backend == "gke" else None
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)
    log, stream = _logger()

    copied = seed_npm_cache(ws.cache, log, image_cache=image_cache)

    npm = _npm(ws)
    for rel, data in IMAGE_FILES.items():
        # MUTATION: make seed_npm_cache return before copying -> the entry is missing.
        assert (npm / rel).read_bytes() == data, rel
    # MUTATION: skip directories with no files in the walk -> `_cacache/tmp` is missing.
    assert (npm / "_cacache" / "tmp").is_dir()
    # MUTATION: count each file's bytes twice (or not at all) -> the total differs.
    assert copied == IMAGE_BYTES

    for dirpath, dirnames, filenames in os.walk(npm):
        # MUTATION: copy with shutil.copytree (copystat keeps 0o555) -> a directory is read-only.
        assert _writable(Path(dirpath)), dirpath
        for name in filenames:
            # MUTATION: use shutil.copy2 (keeps 0o444) -> a copied file is read-only.
            assert _writable(Path(dirpath) / name), name
    (npm / "_cacache" / "tmp" / "npm-write-probe").write_bytes(b"ok")

    # The image copy is left exactly as it was: read-only.
    # MUTATION: chmod the source instead of the copy -> the image cache becomes writable.
    assert not _writable(image_cache / "_cacache")

    seeded = [r for r in _records(stream) if r["message"] == "npm cache seeded from the image"]
    # MUTATION: drop the log line, or its bytes/seconds fields -> no matching record.
    assert len(seeded) == 1
    assert seeded[0]["bytes"] == IMAGE_BYTES
    assert isinstance(seeded[0]["seconds"], (int, float))


# ---------------------------------------------------------------------------
# 2. npm_config_cache still names the attempt's own cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["gke", "cloud-run"])
def test_npm_config_cache_still_points_at_the_attempt_cache(tmp_path, image_cache, backend):
    cache_root = tmp_path / "cache-volume" if backend == "gke" else None
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)
    log, _ = _logger()
    seed_npm_cache(ws.cache, log, image_cache=image_cache)

    env = ws.child_env()
    # MUTATION: point npm_config_cache at cachedirs.IMAGE_NPM_CACHE -> it names the image.
    assert env["npm_config_cache"] == str(_npm(ws))
    assert env["npm_config_cache"] != str(cachedirs.IMAGE_NPM_CACHE)
    # MUTATION: change IMAGE_NPM_CACHE away from the path the Dockerfile writes -> this fails.
    assert cachedirs.IMAGE_NPM_CACHE == Path("/opt/npm-cache")


# ---------------------------------------------------------------------------
# 3. an absent or empty image cache changes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["absent", "empty"])
def test_an_absent_or_empty_image_cache_changes_nothing(tmp_path, kind):
    image = tmp_path / "opt-npm-cache"
    if kind == "empty":
        image.mkdir()
    ws = workspace_mod.create(tmp_path / "workspace", "att_1")
    log, stream = _logger()

    # MUTATION: raise when the image cache is missing -> the call raises.
    copied = seed_npm_cache(ws.cache, log, image_cache=image)

    # MUTATION: return a nonzero count for a no-op -> this fails.
    assert copied == 0
    # MUTATION: create anything in the attempt cache on the no-op path -> it is not empty.
    assert list(_npm(ws).iterdir()) == []
    assert _writable(_npm(ws))
    records = _records(stream)
    # MUTATION: drop the no-op log line (or log it twice) -> the count is not 1.
    assert [r["message"] for r in records] == ["npm image cache absent or empty; not seeded"]


# ---------------------------------------------------------------------------
# 4. a second seed copies nothing
# ---------------------------------------------------------------------------


def test_a_second_seed_does_not_copy_again(tmp_path, image_cache, monkeypatch):
    ws = workspace_mod.create(tmp_path / "workspace", "att_1")
    log, stream = _logger()
    assert seed_npm_cache(ws.cache, log, image_cache=image_cache) == IMAGE_BYTES

    # npm wrote into its cache since: a second seed must not overwrite it.
    blob = _npm(ws) / "_cacache/content-v2/sha512/aa/bb/ccdd"
    blob.write_bytes(b"npm-rewrote-this")
    calls: list[str] = []
    real = shutil.copyfile
    monkeypatch.setattr(
        cachedirs.shutil, "copyfile", lambda s, d, **k: (calls.append(str(s)), real(s, d, **k))[1]
    )

    # MUTATION: drop the "already holds content" check -> it copies again and returns IMAGE_BYTES.
    assert seed_npm_cache(ws.cache, log, image_cache=image_cache) == 0
    assert calls == []
    assert blob.read_bytes() == b"npm-rewrote-this"
    messages = [r["message"] for r in _records(stream)]
    assert messages.count("npm cache seeded from the image") == 1
    assert "npm cache already holds content; not seeded" in messages


# ---------------------------------------------------------------------------
# 5. a copy error leaves an empty, writable cache and the attempt running
# ---------------------------------------------------------------------------


def test_a_copy_error_leaves_an_empty_writable_cache(tmp_path, image_cache, monkeypatch):
    ws = workspace_mod.create(tmp_path / "workspace", "att_1")
    log, stream = _logger()
    real = shutil.copyfile
    seen: list[str] = []

    def flaky(src, dst, **kwargs):
        # The first file copies, the second fails: a half-copied cache.
        if seen:
            raise OSError(28, "No space left on device")
        seen.append(str(src))
        return real(src, dst, **kwargs)

    monkeypatch.setattr(cachedirs.shutil, "copyfile", flaky)

    # MUTATION: let the OSError propagate -> the call raises and the attempt would fail.
    copied = seed_npm_cache(ws.cache, log, image_cache=image_cache)

    assert copied == 0
    npm = _npm(ws)
    # MUTATION: keep the partial copy -> the first file is still there.
    assert npm.is_dir() and list(npm.iterdir()) == []
    # MUTATION: recreate the directory read-only (or not at all) -> npm cannot write.
    assert _writable(npm)
    assert npm == Path(ws.child_env()["npm_config_cache"])
    failed = [r for r in _records(stream) if r["message"] == "npm cache seed failed; continuing with an empty cache"]
    # MUTATION: swallow the error without a log line -> no WARNING record.
    assert len(failed) == 1 and failed[0]["severity"] == "WARNING"
    assert failed[0]["error_type"] == "OSError"


# ---------------------------------------------------------------------------
# 6. the worker's own prepare step seeds it, before the agent starts
# ---------------------------------------------------------------------------

#: A stand-in for `claude --print`: reports what it finds in its npm cache,
#: and whether it can write there, before doing anything else.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, stat, sys
sys.stdin.read()
npm = pathlib.Path(os.environ["npm_config_cache"])
files = {}
for dirpath, dirnames, filenames in os.walk(npm):
    for name in filenames:
        path = pathlib.Path(dirpath) / name
        files[str(path.relative_to(npm))] = path.read_bytes().decode()
writable = all(
    os.stat(dirpath).st_mode & stat.S_IWUSR for dirpath, _d, _f in os.walk(npm)
)
artifacts = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"])
(artifacts / "npm-seen.json").write_text(json.dumps(
    {"npm_config_cache": str(npm), "files": files, "writable": bool(writable)}
))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
"""


@pytest.mark.parametrize("backend", ["gke", "cloud-run"])
def test_the_worker_seeds_the_npm_cache_before_the_agent_starts(
    db, store, worker_factory, tmp_path, image_cache, monkeypatch, backend
):
    """End to end: the real lifecycle, not a direct call. The agent finds the
    image's entries in its own, writable npm cache the moment it starts."""
    from agent_worker.errors import ExitCode
    from worker_seeds import TENANT, seed_attempt

    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    monkeypatch.setattr(cachedirs, "IMAGE_NPM_CACHE", image_cache)
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "go"})
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    kwargs = {}
    if backend == "gke":
        kwargs["cache_root"] = tmp_path / "cache-volume"
        kwargs["cache_root"].mkdir()

    worker, _config, _exporter = worker_factory(runner_profile="claude-code", **kwargs)
    assert worker.run() == ExitCode.OK

    seen = json.loads(store.download_bytes(
        f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/npm-seen.json"
    ))
    # MUTATION: remove the seed call from lifecycle STEP 3 -> the agent's npm cache is empty.
    assert seen["files"] == {rel: data.decode() for rel, data in IMAGE_FILES.items()}
    # MUTATION: point npm_config_cache at cachedirs.IMAGE_NPM_CACHE -> the agent sees the image path.
    assert seen["npm_config_cache"] != str(image_cache)
    # MUTATION: copy with shutil.copytree (keeps 0o555) -> the agent's cache is read-only.
    assert seen["writable"] is True
    # The image copy is left read-only.
    assert not _writable(image_cache / "_cacache")
