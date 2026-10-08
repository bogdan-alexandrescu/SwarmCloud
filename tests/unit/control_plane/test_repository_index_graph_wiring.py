"""The `index:run` task writes the graph (docs/repo-index.md §2.5, lane RI9b).

RI9 shipped the shard writer (`swarm-repo-graph`, images/agent-runtime-indexer/
repo-index/repo_graph_shards.py since #625) and promotion's check of the manifest it
writes, but nothing in production ran it: the indexer prompt did not name it,
named an extractor the image does not ship, and the task carried neither the
repo_id nor the destination the writer needs. These hold the wiring:

* since lane IX1 (2026-10-06) the WORKER runs the image's extractor before
  the agent and the shard writer after it (agent_worker/indexrun.py), so the
  prompt starts from the extractor's output and names neither command line:
  the agent's own run of the writer was killed by its shell's 10-minute
  limit (task_209ba9e0c9c948e284e9). `graph.manifest_digest` is still set by
  the writer and never by the agent;
* the task's input is the prompt alone: `indexer` (claude-code on the
  indexer image, contract request 48) declares no other key, and a key it
  does not declare is refused at submission (invariant 10, contract request
  25). The repo_id travels in `metadata.repo_index` for promotion; the worker
  never reads it, and derives its own from the signed spec;
* the destination is exactly the prefix promotion reads the manifest from
  (`repograph.manifest_key`), under the task's own tenant (invariant 9);
* the bucket is never in the task: the writer resolves it from the step's own
  configuration.
"""

from __future__ import annotations

from swarm_common.profiles import RUNNER_PROFILES, check_inputs

from swarm_api import repograph, repoindex

SHA = "a" * 40
TENANT = "eng"
REPO_ID = "gh-saga-xyz-widgets"


def _record(tenant: str = TENANT, repo_id: str = REPO_ID) -> dict:
    return {
        "tenant_id": tenant,
        "repo_id": repo_id,
        "owner": "saga-xyz",
        "repo": "widgets",
        "default_branch": "main",
        "repository_url": "https://github.com/saga-xyz/widgets",
    }


def _prompt(**kwargs: str) -> str:
    return repoindex.indexer_task(_record(**kwargs), SHA).input["prompt"]


def test_the_prompt_starts_from_the_extractor_the_worker_already_ran():
    """Lane IX1 (2026-10-06): the worker runs the extractor before the agent."""
    prompt = _prompt()
    # The image installs `/usr/local/bin/swarm-repo-index` (Dockerfile, lane RI3).
    assert repoindex.EXTRACTOR_COMMAND == "swarm-repo-index"
    assert repoindex.EXTRACT_FILE in prompt and repoindex.GRAPH_FILE in prompt
    assert repoindex.PHASES_FILE in prompt
    assert "has already run" in prompt
    # The agent is no longer told to run it: the command line is the worker's.
    assert f"{repoindex.EXTRACTOR_COMMAND} --repo " not in prompt
    assert "--graph-out" not in prompt


def test_the_agent_prompt_no_longer_contains_the_graph_write_command():
    """Measured on task_209ba9e0c9c948e284e9: the agent's write was killed by its
    shell's 10-minute limit. The worker writes the graph after the agent."""
    prompt = _prompt()
    assert repoindex.GRAPH_WRITER_COMMAND == "swarm-repo-graph"
    assert repoindex.GRAPH_WRITER_COMMAND not in prompt
    assert " write --graph" not in prompt
    assert "--repo-id" not in prompt and "--destination" not in prompt
    assert "--index" not in prompt


def test_the_agent_never_sets_the_manifest_digest_itself():
    prompt = _prompt()
    assert "never write graph.manifest_digest yourself" in prompt


def test_an_index_run_tasks_metadata_carries_the_repo_id():
    task = repoindex.indexer_task(_record(), SHA)
    assert task.metadata["repo_index"] == REPO_ID


def test_the_destination_is_where_promotion_reads_the_manifest():
    prompt = _prompt()
    destination = repoindex.graph_destination(TENANT, REPO_ID)
    assert repograph.manifest_key(TENANT, REPO_ID, SHA) == f"{destination}/{SHA}/manifest.json"
    # Named for the agent, which must never write under it.
    assert f"{destination}/" in prompt


def test_the_destination_is_the_tasks_own_tenants():
    prompt = _prompt(tenant="research")
    assert f"tenants/research/repos/{REPO_ID}/graph/" in prompt
    assert f"tenants/{TENANT}/" not in prompt


def test_the_input_holds_only_what_the_indexer_profile_declares():
    task = repoindex.indexer_task(_record(), SHA)
    profile = RUNNER_PROFILES[repoindex.INDEXER_PROFILE]
    rest = {key: value for key, value in task.input.items() if key != "prompt"}
    check_inputs(profile, rest)
    assert set(task.input) == {"prompt"}


def test_the_bucket_is_never_in_the_task():
    task = repoindex.indexer_task(_record(), SHA)
    prompt = task.input["prompt"]
    assert "gs://" not in prompt and "--store" not in prompt and "--tenant" not in prompt
    assert "gs://" not in repr(task.metadata)
