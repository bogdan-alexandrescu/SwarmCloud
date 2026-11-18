"""An agent's tool caches and temp files live outside the workspace (CACHE-SCRATCH).

Measured 2026-10-15: three claude-code attempts (task_41ae250f and
task_c6a07695, CI-fix rounds on PR 1338; task_44c61c77, BROWSER-CLASS
implement) were evicted twice each with `Usage of EmptyDir volume "workspace"
exceeds the limit "5Gi"` and not retried. HOME and TMPDIR were both inside the
workspace, so uv's, npm's, pip's and terraform's caches and every pytest temp
directory filled the same volume as the clone.

What holds now, one test per property:

1. on GKE (SWARM_CACHE_ROOT set) the attempt's cache directory is on that
   volume, not under the workspace root, and every cache variable and TMPDIR
   points into it, set by the worker before the runner starts;
2. on Cloud Run (no SWARM_CACHE_ROOT) it is inside the attempt's tmpfs tree,
   counted, and uv's and pip's caches are switched off;
3. the CLI and generic runners carry those variables on to the agent;
4. the checkpoint and the artifact upload never see it, and it is emptied at
   the start of an attempt and removed at its end.

MUTATIONS: drop a name from `cachedirs.CACHE_SUBDIRS` or from
`AGENT_CACHE_ENV` (tests 1 and 3 name it); put the cache under `work/` (the
checkpoint and upload tests see its files); stop `create` clearing a leftover
cache, or `destroy` removing it (test 4); drop `NO_CACHE_ENV` on the Cloud Run
path (test 2).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker import workspace as workspace_mod
from agent_worker.cachedirs import AGENT_CACHE_ENV, CACHE_ROOT_ENV
from agent_worker.checkpoint import CheckpointManager
from agent_worker.config import WorkerConfig
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger
from agent_worker.runners import cliagent, generic

from test_runners import make_ctx
from worker_seeds import PROJECT, TENANT, seed_attempt

#: The variables the owner named, each of which must point into the cache.
CACHE_VARIABLES = (
    "UV_CACHE_DIR",
    "npm_config_cache",
    "TF_PLUGIN_CACHE_DIR",
    "PIP_CACHE_DIR",
    "XDG_CACHE_HOME",
    "TMPDIR",
)


def _inside(path: str | Path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


# ---------------------------------------------------------------------------
# 1. GKE: its own volume
# ---------------------------------------------------------------------------


def test_on_gke_the_cache_is_on_its_own_volume_and_every_variable_points_there(tmp_path):
    cache_root = tmp_path / "cache-volume"
    cache_root.mkdir()
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)

    assert ws.cache is not None
    assert _inside(ws.cache, cache_root), ws.cache
    assert not _inside(ws.cache, tmp_path / "workspace"), "the cache is on the workspace volume"

    env = ws.child_env()
    for name in CACHE_VARIABLES:
        assert name in env, f"{name} is not set for the runner"
        assert _inside(env[name], ws.cache), f"{name}={env[name]} is not in {ws.cache}"
        assert Path(env[name]).is_dir(), f"{name}={env[name]} does not exist"
    # A hard link cannot cross volumes; say so instead of letting uv warn.
    assert env["UV_LINK_MODE"] == "copy"
    # The caches work on GKE: nothing switches them off.
    assert "UV_NO_CACHE" not in env and "PIP_NO_CACHE_DIR" not in env
    # HOME is unchanged: the CLI's own state is the agent's and is checkpointed.
    assert env["HOME"] == str(ws.work)


def test_the_worker_reads_the_cache_root_from_its_environment(monkeypatch):
    for name, value in {
        "TASK_ID": "task_1",
        "ATTEMPT_ID": "att_1",
        "LEASE_ID": "lease_1",
        "TENANT_ID": TENANT,
        "RUNNER_PROFILE": "mock",
        "PROJECT_ID": PROJECT,
        "GENERATION": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(CACHE_ROOT_ENV, raising=False)
    assert WorkerConfig.from_env().cache_root is None
    monkeypatch.setenv(CACHE_ROOT_ENV, "/cache")
    assert WorkerConfig.from_env().cache_root == Path("/cache")


# ---------------------------------------------------------------------------
# 2. Cloud Run: small, inside the tmpfs, counted
# ---------------------------------------------------------------------------


def test_on_cloud_run_the_cache_is_in_the_attempt_tree_and_uv_and_pip_cache_nothing(tmp_path):
    ws = workspace_mod.create(tmp_path / "workspace", "att_1")

    assert ws.cache is not None
    assert _inside(ws.cache, ws.root), "on Cloud Run the cache must count against the tmpfs"
    for outside in (ws.work, ws.artifacts):
        assert not _inside(ws.cache, outside), f"the cache is inside {outside}"

    env = ws.child_env()
    for name in CACHE_VARIABLES:
        assert _inside(env[name], ws.root), f"{name}={env[name]} left the workspace tmpfs"
        assert not _inside(env[name], ws.work), f"{name}={env[name]} would be checkpointed"
    assert env["UV_NO_CACHE"] == "1"
    assert env["PIP_NO_CACHE_DIR"] == "1"
    # npm has no no-cache mode: its cache is in the tmpfs, and counted there.
    (Path(env["npm_config_cache"]) / "blob").write_bytes(b"x" * 4096)
    assert ws.disk_bytes() >= 4096


# ---------------------------------------------------------------------------
# 3. the runners carry the variables on to the agent
# ---------------------------------------------------------------------------


def test_every_cache_variable_is_carried_by_the_cli_runner():
    missing = [name for name in AGENT_CACHE_ENV if name not in cliagent._PLAIN_PASSTHROUGH
               and name != "TMPDIR"]
    assert not missing, f"the CLI runner drops {missing} before the agent starts"
    assert set(CACHE_VARIABLES) <= set(AGENT_CACHE_ENV)


def test_the_generic_runner_carries_the_cache_variables(tmp_path, monkeypatch):
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=tmp_path / "c")
    for name, value in ws.child_env().items():
        if name in AGENT_CACHE_ENV:
            monkeypatch.setenv(name, value)
    env = generic._build_env(make_ctx(tmp_path, {}))
    for name in CACHE_VARIABLES:
        assert env.get(name) and _inside(env[name], ws.cache), (name, env.get(name))


#: A stand-in for `claude --print`: reports the cache variables it was started
#: with, then fills each one the way uv, npm, pip, terraform and pytest would.
FAKE_AGENT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys

sys.stdin.read()
names = %s
seen = {name: os.environ.get(name) for name in names}
artifacts = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"])
(artifacts / "cache-env.json").write_text(json.dumps(seen))
for name in names:
    if seen[name] and os.path.isabs(seen[name]):
        target = pathlib.Path(seen[name]) / "CACHE-JUNK.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 1024)
pathlib.Path("kept.txt").write_text("the agent's work\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
""" % (repr(list(AGENT_CACHE_ENV)),)


@pytest.fixture
def agent_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def _archives(store: Any) -> list[str]:
    names: list[str] = []
    for key in store.list_keys(f"tenants/{TENANT}/tasks/task_1/attempts/"):
        if "/checkpoints/" in key and key.endswith(".tar.gz"):
            with tarfile.open(fileobj=io.BytesIO(store.download_bytes(key)), mode="r:gz") as tar:
                names += tar.getnames()
    return names


def test_a_claude_code_agent_fills_the_cache_and_none_of_it_is_kept(
    db, store, worker_factory, agent_cli, tmp_path
):
    """End to end: the worker sets the variables, the CLI runner passes them,
    the agent writes into every one, and neither a checkpoint nor the upload
    carries a byte of it; the cache is gone when the attempt ends."""
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "go"})
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    cache_root = tmp_path / "cache-volume"
    cache_root.mkdir()

    worker, _config, _exporter = worker_factory(runner_profile="claude-code", cache_root=cache_root)
    assert worker.run() == ExitCode.OK

    artifacts = {e["name"] for e in db.doc("tasks/task_1")["result_summary"]["artifacts"]}
    seen = json.loads(store.download_bytes(
        f"tenants/{TENANT}/tasks/task_1/attempts/att_1/artifacts/cache-env.json"
    ))
    for name in CACHE_VARIABLES:
        assert seen.get(name), f"the agent was started without {name}"
        assert _inside(seen[name], cache_root / "att_1"), f"{name}={seen[name]}"
    assert not [a for a in artifacts if "CACHE-JUNK" in a], artifacts
    names = _archives(store)
    assert names, "no checkpoint was written; this test would check nothing"
    assert not [n for n in names if "CACHE-JUNK" in n], names
    assert not os.path.lexists(cache_root / "att_1"), "the attempt's cache outlived it"


# ---------------------------------------------------------------------------
# 4. never checkpointed, never uploaded, cleared per attempt
# ---------------------------------------------------------------------------


def _manager(store, attempt_id="att_1", generation=1):
    logger = build_logger(
        task_id="task_1", attempt_id=attempt_id, tenant_id=TENANT, generation=generation,
        runner_profile="claude-code", stream=io.StringIO(),
    )
    return CheckpointManager(
        store=store, tenant_id=TENANT, task_id="task_1", attempt_id=attempt_id,
        generation=generation, logger=logger,
    )


@pytest.mark.parametrize("backend", ["gke", "cloud-run"])
def test_the_checkpoint_carries_the_work_and_none_of_the_cache(store, tmp_path, backend):
    cache_root = tmp_path / "cache-volume" if backend == "gke" else None
    ws = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)
    env = ws.child_env()
    for name in CACHE_VARIABLES:
        (Path(env[name]) / "CACHE-JUNK.bin").write_bytes(b"x")
    (ws.work / "notes.md").write_text("half done\n")

    record = _manager(store).create(ws)
    with tarfile.open(fileobj=io.BytesIO(store.download_bytes(record.archive_key)),
                      mode="r:gz") as archive:
        names = archive.getnames()

    assert "notes.md" in names, names
    assert not [n for n in names if "CACHE-JUNK" in n], names


@pytest.mark.parametrize("backend", ["gke", "cloud-run"])
def test_the_cache_is_emptied_at_the_start_of_an_attempt_and_removed_at_its_end(
    tmp_path, backend
):
    cache_root = tmp_path / "cache-volume" if backend == "gke" else None
    first = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)
    assert first.cache is not None
    (first.cache / "left-over.bin").write_bytes(b"x")

    again = workspace_mod.create(tmp_path / "workspace", "att_1", cache_root=cache_root)
    assert again.cache is not None
    assert not (again.cache / "left-over.bin").exists(), "a cache survived into a new attempt"

    workspace_mod.destroy(again)
    assert not os.path.lexists(again.cache), "destroy left the attempt's cache behind"
