"""The `index:run` task writes the graph (docs/repo-index.md §2.5, lane RI9b).

RI9 shipped the shard writer (`swarm-repo-graph`, images/agent-runtime-indexer/
repo-index/repo_graph_shards.py since #625) and promotion's check of the manifest it
writes, but nothing in production ran it: the indexer prompt did not name it,
named an extractor the image does not ship, and the task carried neither the
repo_id nor the destination the writer needs. These hold the wiring:

* the compiled prompt runs the image's extractor with `--graph-out`, then the
  shard writer on that graph, with `--index` on the artifact promotion reads,
  so `graph.manifest_digest` is set by the writer and never by the agent;
* the task's input carries the repo_id and the destination, inside the one
  input key every profile takes (`prompt`): `indexer` (claude-code on the
  indexer image, contract request 48) declares no other key that could hold them, and a key it does not declare is refused at
  submission (invariant 10, contract request 25);
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


def test_the_prompt_runs_the_extractor_the_image_ships_with_a_graph_out():
    prompt = _prompt()
    # The image installs `/usr/local/bin/swarm-repo-index` (Dockerfile, lane RI3).
    assert repoindex.EXTRACTOR_COMMAND == "swarm-repo-index"
    assert f"{repoindex.EXTRACTOR_COMMAND} --repo . --out " in prompt
    assert f"--graph-out {repoindex.GRAPH_FILE}" in prompt


def test_the_compiled_indexer_prompt_names_the_shard_writer():
    prompt = _prompt()
    write = f"{repoindex.GRAPH_WRITER_COMMAND} write --graph {repoindex.GRAPH_FILE}"
    assert repoindex.GRAPH_WRITER_COMMAND == "swarm-repo-graph"
    assert write in prompt
    assert f"--index $SWARM_ARTIFACTS_DIR/{repoindex.INDEX_FILE}" in prompt
    # The extractor first, the index composed, the writer last.
    assert prompt.index("--graph-out") < prompt.index(write)
    assert prompt.index(f"Write exactly one file, $SWARM_ARTIFACTS_DIR/") < prompt.index(write)


def test_the_agent_never_sets_the_manifest_digest_itself():
    prompt = _prompt()
    assert "never write graph.manifest_digest yourself" in prompt


def test_an_index_run_tasks_input_carries_the_repo_id_and_the_destination():
    task = repoindex.indexer_task(_record(), SHA)
    prompt = task.input["prompt"]
    assert f"--repo-id {REPO_ID}" in prompt
    assert f"--destination tenants/{TENANT}/repos/{REPO_ID}/graph" in prompt
    assert task.metadata["repo_index"] == REPO_ID


def test_the_destination_is_where_promotion_reads_the_manifest():
    prompt = _prompt()
    destination = repoindex.graph_destination(TENANT, REPO_ID)
    assert repograph.manifest_key(TENANT, REPO_ID, SHA) == f"{destination}/{SHA}/manifest.json"
    assert f"--destination {destination} " in prompt


def test_the_destination_is_the_tasks_own_tenants():
    prompt = _prompt(tenant="research")
    assert f"--destination tenants/research/repos/{REPO_ID}/graph" in prompt
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
