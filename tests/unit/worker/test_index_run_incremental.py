"""An incremental index run is given its base by reference (docs/repo-index.md §3.4, lane IX2).

swarm-api names the base in the task's prompt, on a line of its own
(`swarm-index-base: <sha>`), because the prompt is inside the signed `input`
and `metadata.base_sha` is not; the worker stages it before the extractor,
as the phase `stage_base`:

* the promoted version is read under the registration the SIGNED spec
  names, and only when it is this tenant's;
* the base's repo-index.json comes through the staged-input path every
  workflow input takes (the successful attempt's manifest, a key inside this
  tenant's prefix) and must match the digest promotion recorded;
* the base graph is read back from the shards by `swarm-repo-graph read`,
  checked against the manifest digest promotion recorded;
* the extractor is then given `--base-sha`/`--base-index`/`--base-graph`, and
  the graph write `--base-commit`.

Anything that cannot be staged makes the run full and says why in the phase
record; it never fails the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import indexrun, lifecycle
from agent_worker.lifecycle import Worker
from swarm_api import repoindex, repositories

from worker_seeds import TENANT, seed_attempt

OWNER, REPO = "Saga-XYZ", "Widgets"
URL = f"https://github.com/{OWNER}/{REPO}"
REPO_ID = repositories.repo_id_for(TENANT, OWNER, REPO)
BASE = hashlib.sha1(b"base").hexdigest()
HEAD = hashlib.sha1(b"head").hexdigest()
GRAPH_DIGEST = "sha256:" + hashlib.sha256(b"the base manifest").hexdigest()
BASE_INDEX = json.dumps({"schema": "swarm.repo-index/v1", "commit_sha": BASE,
                         "kind": "full", "modules": []}).encode()

#: The fake extractor: records its argv and whether the base was on disk.
FAKE_EXTRACTOR = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
out, graph = args[args.index("--out") + 1], args[args.index("--graph-out") + 1]
seen = {}
for flag in ("--base-index", "--base-graph"):
    if flag in args:
        path = args[args.index(flag) + 1]
        seen[flag] = open(path).read() if os.path.exists(path) else None
with open(ORDER, "a") as fh:
    fh.write("extract " + json.dumps({"argv": args, "seen": seen}) + "\n")
open(out, "w").write("{}")
open(graph, "w").write('{"schema": "swarm.repo-graph/v1"}')
'''

#: The fake shard tool: `read` writes the base graph, `write` sets the digest.
FAKE_GRAPH_TOOL = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
if args[0] == "read":
    with open(ORDER, "a") as fh:
        fh.write("read " + json.dumps({"argv": args}) + "\n")
    if os.environ.get("FAIL_READ"):
        print("swarm-repo-graph: the manifest does not match the digest", file=sys.stderr)
        sys.exit(1)
    open(args[args.index("--out") + 1], "w").write('{"schema": "swarm.repo-graph/v1"}')
    sys.exit(0)
index = args[args.index("--index") + 1]
with open(ORDER, "a") as fh:
    fh.write("graph_write " + json.dumps({"argv": args}) + "\n")
doc = json.load(open(index))
doc["graph"] = {"manifest_digest": "sha256:" + "0" * 64}
open(index, "w").write(json.dumps(doc))
'''

FAKE_AGENT = r'''
import json, os, runpy
phases = os.path.join(os.environ["SWARM_WORK_DIR"], "repo-index.phases.json")
seen = json.load(open(phases)) if os.path.exists(phases) else None
with open(ORDER, "a") as fh:
    fh.write("agent " + json.dumps({"phases": seen}) + "\n")
runpy.run_module("agent_worker.runners.mock", run_name="__main__")
'''


def _tool(path: Path, text: str, order: Path) -> None:
    path.write_text(text.replace("PYTHON", sys.executable, 1).replace("ORDER", repr(str(order))))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def index_tools(tmp_path, monkeypatch):
    order = tmp_path / "order.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _tool(bin_dir / indexrun.EXTRACTOR_COMMAND, FAKE_EXTRACTOR, order)
    _tool(bin_dir / indexrun.GRAPH_WRITER_COMMAND, FAKE_GRAPH_TOOL, order)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        lifecycle, "_runner_argv",
        lambda cfg: [sys.executable, "-c", FAKE_AGENT.replace("ORDER", repr(str(order)))],
    )
    return order


@pytest.fixture
def checkout(monkeypatch):
    def fake_clone(self: Worker, task: dict[str, Any]) -> dict[str, Any]:
        (self.ws.work / lifecycle.REPO_DIR_NAME).mkdir(parents=True, exist_ok=True)
        return {"url": URL}

    def harvest(self: Worker, **_kwargs: Any) -> dict[str, Any]:
        return {"published": False, "publish_reason": "an index run publishes nothing"}

    monkeypatch.setattr(Worker, "_maybe_clone", fake_clone)
    monkeypatch.setattr(Worker, "_harvest_git", harvest)


def _seed(db: Any, store: Any, *, kind: str = "incremental", base_tenant: str = TENANT,
          digest: str | None = None, version: bool = True, prompt: str | None = None) -> None:
    if prompt is None:
        prompt = "index it\n" + (f"{indexrun.BASE_LINE}{BASE}\n" if kind == "incremental" else "")
    seed_attempt(
        db,
        runner_profile="indexer",
        task_input={"prompt": prompt, "steps": 1, "sleep_seconds": 0.05,
                    "artifact_name": indexrun.INDEX_FILE,
                    "artifact_text": json.dumps({"schema": "swarm.repo-index/v1"})},
    )
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    task = db.doc("tasks/task_1")
    task["repository_url"] = URL
    task["metadata"] = {"repo_index": REPO_ID, "commit_sha": HEAD, "index_kind": kind,
                        **({"base_sha": BASE} if kind == "incremental" else {})}
    # The registration and its promoted base version, as swarm-api wrote them.
    db.seed(f"repositories/{REPO_ID}", {"tenant_id": TENANT, "repo_id": REPO_ID})
    if version:
        db.seed(f"repositories/{REPO_ID}/index_versions/{BASE}", {
            "commit_sha": BASE, "task_id": "task_base",
            "digest": digest or repoindex.content_digest(BASE_INDEX.decode()),
            "graph_digest": GRAPH_DIGEST,
        })
    key = f"tenants/{base_tenant}/tasks/task_base/attempts/att_9/artifacts/repo-index.json"
    store.upload_bytes(key, BASE_INDEX)
    db.seed("tasks/task_base", {
        "id": "task_base", "tenant_id": base_tenant, "state": "SUCCEEDED",
        "result_summary": {"artifacts": [
            {"name": "repo-index.json", "bytes": len(BASE_INDEX), "uri": store.uri(key)},
        ]},
    })


def _order(order: Path) -> list[tuple[str, dict[str, Any]]]:
    rows = []
    for line in order.read_text().splitlines():
        name, _, rest = line.partition(" ")
        rows.append((name, json.loads(rest)))
    return rows


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _phases(db: Any) -> dict[str, dict[str, Any]]:
    return {p["phase"]: p for p in db.doc("tasks/task_1")["result_summary"]["repo_index_phases"]}


def test_an_incremental_run_stages_its_base_before_the_extractor(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store)
    worker, config, _ = worker_factory(runner_profile="indexer", timeout_seconds=900,
                                       repository_url=URL)

    worker.run()

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["read", "extract", "agent", "graph_write"], rows
    read, extract, write = rows[0][1]["argv"], rows[1][1], rows[3][1]["argv"]
    assert _flag(read, "--commit") == BASE
    assert _flag(read, "--manifest-digest") == GRAPH_DIGEST
    assert _flag(read, "--repo-id") == REPO_ID
    assert _flag(read, "--destination") == f"tenants/{TENANT}/repos/{REPO_ID}/graph"
    assert _flag(read, "--store") == f"gs://{config.artifact_bucket}"
    assert _flag(extract["argv"], "--base-sha") == BASE
    # Both base files were on disk when the extractor ran; the index is the
    # promoted artifact, byte for byte.
    assert extract["seen"]["--base-index"] == BASE_INDEX.decode()
    assert extract["seen"]["--base-graph"] is not None
    assert _flag(write, "--base-commit") == BASE
    phases = _phases(db)
    assert phases["stage_base"]["status"] == "ok"
    assert BASE[:12] in phases["stage_base"]["reason"]
    # The agent can read that the base was staged.
    assert [p["phase"] for p in rows[2][1]["phases"]] == ["stage_base", "extract"]


@pytest.mark.parametrize("case,why", [
    ("rewritten", "no longer matches the digest"),
    ("pruned", "no promoted index"),
    ("other_tenant", "belongs to tenant"),
])
def test_a_base_that_cannot_be_staged_makes_the_run_full_and_says_why(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed, case, why
):
    _seed(db, store,
          digest="sha256:" + "0" * 64 if case == "rewritten" else None,
          version=case != "pruned",
          base_tenant="other" if case == "other_tenant" else TENANT)
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=900,
                                  repository_url=URL)

    worker.run()

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["extract", "agent", "graph_write"], rows
    assert "--base-sha" not in rows[0][1]["argv"]
    assert "--base-commit" not in rows[2][1]["argv"]
    stage = _phases(db)["stage_base"]
    assert stage["status"] == "skipped" and why in stage["reason"], stage
    assert stage["reason"].startswith("a full run: ")
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_a_base_graph_that_cannot_be_read_makes_the_run_full(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed, monkeypatch
):
    monkeypatch.setenv("FAIL_READ", "1")
    monkeypatch.setattr(indexrun, "PASS_THROUGH_ENV", indexrun.PASS_THROUGH_ENV + ("FAIL_READ",))
    _seed(db, store)
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=900,
                                  repository_url=URL)

    worker.run()

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["read", "extract", "agent", "graph_write"], rows
    assert "--base-sha" not in rows[1][1]["argv"]
    stage = _phases(db)["stage_base"]
    assert stage["status"] == "skipped" and "base graph was not read" in stage["reason"]


def test_a_full_run_stages_nothing(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed(db, store, kind="full")
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=1800,
                                  repository_url=URL)

    worker.run()

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["extract", "agent", "graph_write"], rows
    assert "--base-sha" not in rows[0][1]["argv"]
    assert "stage_base" not in _phases(db)


def test_unsigned_metadata_alone_stages_no_base(
    db, store, worker_factory, index_tools, checkout, recheck_bypassed
):
    """`metadata.index_kind` and `base_sha` are outside the spec signature: never read."""
    _seed(db, store, prompt="index it")
    assert db.doc("tasks/task_1")["metadata"]["base_sha"] == BASE
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=900,
                                  repository_url=URL)

    worker.run()

    rows = _order(index_tools)
    assert [name for name, _ in rows] == ["extract", "agent", "graph_write"], rows
    assert "--base-sha" not in rows[0][1]["argv"]
    assert "stage_base" not in _phases(db)


def test_the_requested_base_is_read_from_the_signed_prompt_only():
    line = f"{indexrun.BASE_LINE}{BASE}"
    assert indexrun.requested_base({"prompt": f"index\n{line}\nmore"}) == BASE
    assert indexrun.requested_base({"prompt": f"index {line}"}) is None, "a line of its own"
    assert indexrun.requested_base({"prompt": f"{indexrun.BASE_LINE}../x"}) is None
    other = f"{indexrun.BASE_LINE}{HEAD}"
    assert indexrun.requested_base({"prompt": f"{line}\n{other}"}) is None
    assert indexrun.requested_base({"prompt": f"{line}\n{line}"}) == BASE
    assert indexrun.requested_base({"base_sha": BASE}) is None
    assert indexrun.requested_base(None) is None


def test_the_worker_and_the_api_name_the_same_base_files_and_keys():
    assert repoindex.BASE_INDEX_FILE == f"$SWARM_WORK_DIR/{indexrun.BASE_INDEX_FILE}"
    assert repoindex.VERSIONS_COLLECTION == indexrun.VERSIONS_COLLECTION
    assert repositories.COLLECTION == indexrun.REPOSITORIES_COLLECTION
    task = repoindex.indexer_task(
        {"owner": OWNER, "repo": REPO, "default_branch": "main", "tenant_id": TENANT,
         "repo_id": REPO_ID, "repository_url": URL},
        HEAD, "incremental", base_sha=BASE,
    )
    assert repoindex.BASE_LINE == indexrun.BASE_LINE
    assert indexrun.requested_base(task.input) == BASE
    full = repoindex.indexer_task(
        {"owner": OWNER, "repo": REPO, "default_branch": "main", "tenant_id": TENANT,
         "repo_id": REPO_ID, "repository_url": URL},
        HEAD, "full",
    )
    assert indexrun.requested_base(full.input) is None
