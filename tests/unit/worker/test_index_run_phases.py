"""An index run's deterministic passes are the worker's steps (owner decision 2026-10-06, lane IX1).

Measured on task_209ba9e0c9c948e284e9 (2026-10-06 15:08-15:32): the agent
ran the extractor and the shard writer through its Bash tool, and Claude
Code's 10-minute command limit killed the writer after 150 blobs. Both
passes are commands with fixed arguments, so the worker runs them around the
agent, with their own timeouts (agent_worker/indexrun.py):

    extract -> agent -> graph_write

What is pinned here:

* the order, by fake tools on PATH that each check what exists when they
  run: the extractor runs before the agent wrote the index, the writer after
  it, on the extractor's graph and with `--index` on the artifact;
* each phase's duration is recorded in the step's `result_summary` and in
  `work/repo-index.phases.json`;
* the writer's target is derived from the signed spec, by the registration's
  own recipe, and a phase that fails or is missing never fails the run;
* the agent's time is shortened by the write's reserve, so an agent that
  uses all its time still leaves the write its own;
* the worker's paths are the ones the prompt names.
"""

from __future__ import annotations

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

from conftest import TENANT, seed_attempt

OWNER, REPO = "Saga-XYZ", "Widgets"
URL = f"https://github.com/{OWNER}/{REPO}"


# --------------------------------------------------------------------------
# the pure half
# --------------------------------------------------------------------------

def test_the_repo_id_is_the_registrations_own_recipe():
    for tenant, owner, repo in ((TENANT, OWNER, REPO), ("eng", "saga", "x.y-z")):
        assert indexrun.repo_id_for(tenant, owner, repo) == repositories.repo_id_for(
            tenant, owner, repo
        )


def test_the_target_comes_from_the_signed_url_and_tenant():
    where = indexrun.target(TENANT, URL)
    assert isinstance(where, indexrun.Target)
    assert where.repo_id == repositories.repo_id_for(TENANT, OWNER, REPO)
    assert where.destination == repoindex.graph_destination(TENANT, where.repo_id)
    assert indexrun.target(TENANT, URL + ".git") == where


def test_a_repository_that_is_not_on_github_has_no_target():
    assert isinstance(indexrun.target(TENANT, "file:///tmp/x"), str)
    assert isinstance(indexrun.target(TENANT, None), str)


def test_the_budgets_fit_the_thirty_minute_run():
    full = indexrun.budgets(repoindex.FULL_TIMEOUT_SECONDS)
    assert full.extract == 720 and full.graph_write == 270
    assert full.lsp_total < full.extract
    # The agent keeps the larger part of the run.
    assert repoindex.FULL_TIMEOUT_SECONDS - full.extract - full.graph_write >= 800
    short = indexrun.budgets(repoindex.INCREMENTAL_TIMEOUT_SECONDS)
    assert short.extract + short.graph_write < repoindex.INCREMENTAL_TIMEOUT_SECONDS


def test_the_worker_reads_and_writes_the_paths_the_prompt_names():
    assert repoindex.EXTRACT_FILE == f"$SWARM_WORK_DIR/{indexrun.EXTRACT_FILE}"
    assert repoindex.GRAPH_FILE == f"$SWARM_WORK_DIR/{indexrun.GRAPH_FILE}"
    assert repoindex.PHASES_FILE == f"$SWARM_WORK_DIR/{indexrun.PHASES_FILE}"
    assert repoindex.INDEX_FILE == indexrun.INDEX_FILE
    assert repoindex.INDEXER_PROFILE == indexrun.INDEXER_PROFILE
    assert repoindex.EXTRACTOR_COMMAND == indexrun.EXTRACTOR_COMMAND
    assert repoindex.GRAPH_WRITER_COMMAND == indexrun.GRAPH_WRITER_COMMAND


# --------------------------------------------------------------------------
# the lifecycle: extract -> agent -> graph_write
# --------------------------------------------------------------------------

#: The fake extractor: refuses to run once the agent has written the index.
FAKE_EXTRACTOR = r'''#!PYTHON
import json, os, sys, time
args = sys.argv[1:]
out, graph = args[args.index("--out") + 1], args[args.index("--graph-out") + 1]
artifacts = os.path.join(os.path.dirname(out), "..", "artifacts", "repo-index.json")
with open(ORDER, "a") as fh:
    fh.write("extract " + json.dumps({"index_exists": os.path.exists(artifacts),
                                     "argv": args}) + "\n")
time.sleep(0.2)
open(out, "w").write("{}")
open(graph, "w").write('{"schema": "swarm.repo-graph/v1"}')
'''

#: The fake shard writer: records what it was given and what existed.
FAKE_WRITER = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
index = args[args.index("--index") + 1]
graph = args[args.index("--graph") + 1]
with open(ORDER, "a") as fh:
    fh.write("graph_write " + json.dumps({"index_exists": os.path.exists(index),
                                         "graph_exists": os.path.exists(graph),
                                         "argv": args,
                                         "tenant_env": os.environ.get("TENANT_ID")}) + "\n")
doc = json.load(open(index))
doc["graph"] = {"manifest_digest": "sha256:" + "0" * 64}
open(index, "w").write(json.dumps(doc))
print(json.dumps({"blobs_written": 3, "blobs_reused": 0}))
'''

#: The agent: records itself and the phases file it can read, then hands over
#: to the mock runner.
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
    """The image's two tools as fakes on PATH, and the order they and the agent ran in."""
    order = tmp_path / "order.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _tool(bin_dir / indexrun.EXTRACTOR_COMMAND, FAKE_EXTRACTOR, order)
    _tool(bin_dir / indexrun.GRAPH_WRITER_COMMAND, FAKE_WRITER, order)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        lifecycle, "_runner_argv",
        lambda cfg: [sys.executable, "-c", FAKE_AGENT.replace("ORDER", repr(str(order)))],
    )
    return order, bin_dir


@pytest.fixture
def checkout(monkeypatch):
    """A checkout without a forge: the clone is not what this file is about."""

    def fake_clone(self: Worker, task: dict[str, Any]) -> dict[str, Any]:
        (self.ws.work / lifecycle.REPO_DIR_NAME).mkdir(parents=True, exist_ok=True)
        return {"url": URL}

    def harvest(self: Worker, **_kwargs: Any) -> dict[str, Any]:
        return {"published": False, "publish_reason": "an index run publishes nothing"}

    monkeypatch.setattr(Worker, "_maybe_clone", fake_clone)
    monkeypatch.setattr(Worker, "_harvest_git", harvest)


def _seed_index_run(db: Any, *, repo_index: str | None = None) -> None:
    seed_attempt(
        db,
        runner_profile="indexer",
        task_input={"prompt": "index it", "steps": 1, "sleep_seconds": 0.05,
                    "artifact_name": indexrun.INDEX_FILE,
                    "artifact_text": json.dumps({"schema": "swarm.repo-index/v1"})},
    )
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    task = db.doc("tasks/task_1")
    task["repository_url"] = URL
    task["metadata"] = {
        "repo_index": repo_index or repositories.repo_id_for(TENANT, OWNER, REPO),
        "commit_sha": "a" * 40, "index_kind": "full",
    }


def _order(order: Path) -> list[tuple[str, dict[str, Any]]]:
    rows = []
    for line in order.read_text().splitlines():
        name, _, rest = line.partition(" ")
        rows.append((name, json.loads(rest)))
    return rows


def test_the_worker_runs_extract_then_agent_then_graph_write(
    db, worker_factory, index_tools, checkout, recheck_bypassed
):
    order, _bin = index_tools
    _seed_index_run(db)
    worker, config, _ = worker_factory(runner_profile="indexer", timeout_seconds=600,
                                       repository_url=URL)

    worker.run()

    rows = _order(order)
    assert [name for name, _ in rows] == ["extract", "agent", "graph_write"], rows
    extract, write = rows[0][1], rows[2][1]
    assert extract["index_exists"] is False, "the extractor runs before the agent's index"
    assert "--lsp-total-budget-seconds" in extract["argv"]
    assert write["index_exists"] and write["graph_exists"]
    repo_id = repositories.repo_id_for(TENANT, OWNER, REPO)
    argv = write["argv"]
    assert argv[argv.index("--repo-id") + 1] == repo_id
    assert argv[argv.index("--destination") + 1] == f"tenants/{TENANT}/repos/{repo_id}/graph"
    assert argv[argv.index("--store") + 1] == f"gs://{config.artifact_bucket}"
    assert write["tenant_env"] == TENANT

    phases = db.doc("tasks/task_1")["result_summary"]["repo_index_phases"]
    assert [p["phase"] for p in phases] == ["extract", "agent", "graph_write"]
    assert all(p["status"] == "ok" for p in phases), phases
    assert all(isinstance(p["seconds"], float) and p["seconds"] > 0 for p in phases), phases
    assert phases[0]["seconds"] >= 0.2
    # What the agent could read when it started: the extractor's record.
    assert [p["phase"] for p in rows[1][1]["phases"]] == ["extract"]
    assert rows[1][1]["phases"][0]["status"] == "ok"


def test_a_missing_extractor_is_recorded_and_the_agent_still_runs(
    db, worker_factory, index_tools, checkout, recheck_bypassed
):
    order, bin_dir = index_tools
    (bin_dir / indexrun.EXTRACTOR_COMMAND).unlink()
    _seed_index_run(db)
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=600,
                                  repository_url=URL)

    worker.run()

    assert [name for name, _ in _order(order)] == ["agent"]
    phases = db.doc("tasks/task_1")["result_summary"]["repo_index_phases"]
    by_name = {p["phase"]: p for p in phases}
    assert by_name["extract"]["status"] == "skipped"
    assert "not installed" in by_name["extract"]["reason"]
    # No graph without the extractor's graph file.
    assert by_name["graph_write"]["status"] == "skipped"
    # The agent can read why, and falls back to computing the fields itself.
    seen = _order(order)[0][1]["phases"]
    assert seen[0]["phase"] == "extract" and "not installed" in seen[0]["reason"]


def test_unsigned_metadata_cannot_point_the_write_at_another_registration(
    db, worker_factory, index_tools, checkout, recheck_bypassed
):
    """`metadata.repo_index` is outside the spec signature: the worker never reads it."""
    order, _bin = index_tools
    _seed_index_run(db, repo_index="repo_0000000000000000")
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=600,
                                  repository_url=URL)

    worker.run()

    rows = _order(order)
    assert [name for name, _ in rows] == ["extract", "agent", "graph_write"]
    argv = rows[-1][1]["argv"]
    assert argv[argv.index("--repo-id") + 1] == repositories.repo_id_for(TENANT, OWNER, REPO)
    assert "repo_0000000000000000" not in " ".join(argv)


def test_the_agent_is_given_the_task_time_less_the_writes_reserve(
    db, worker_factory, index_tools, checkout, recheck_bypassed
):
    _seed_index_run(db)
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=600,
                                  repository_url=URL)
    worker.run()
    reserve = indexrun.budgets(600).graph_write
    assert worker._deadline - worker._runner_deadline == pytest.approx(reserve, abs=0.01)


def test_an_ordinary_task_runs_no_index_phase(db, worker_factory, index_tools):
    order, _bin = index_tools
    seed_attempt(db, task_input={"prompt": "x", "steps": 1, "sleep_seconds": 0.05})
    worker, _, _ = worker_factory()
    worker.run()
    assert [name for name, _ in _order(order)] == ["agent"]
    assert "repo_index_phases" not in (db.doc("tasks/task_1").get("result_summary") or {})
