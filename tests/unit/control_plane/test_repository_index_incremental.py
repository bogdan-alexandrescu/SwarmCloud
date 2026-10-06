"""Incremental index runs: the choice, the submission and promotion (repo-index.md §3.4, lane IX2).

Owner decision 2026-10-06: until now `check_run_kind` refused `incremental`,
so every trigger was a full ~20-minute rebuild (2026-10-06: 1,685 files
re-indexed two commits after the previous index). What is held here:

  * `choose_kind` grants incremental only when every §3.4 condition holds --
    a promoted index with a graph, of an ANCESTOR of the head, fewer than 300
    changed files, no build or test configuration changed, the last full run
    younger than `full_every_days` -- and says why when it does not;
  * an incremental run is submitted by profile name like any other
    (invariant 10), with the incremental timeout, its base in the metadata
    and in the signed prompt, and records kind, base_sha and the reason;
  * a configuration change or a non-ancestor base submits a full run;
  * promotion records kind and base_sha, refuses an index that claims a base
    the run was not given, and only a full index moves `last_full_at`;
  * the API serves kind and base_sha on runs and versions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import repoindex

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE, TWO, THREE = sha("one"), sha("two"), sha("three")
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
GRAPH = "sha256:" + "ab" * 32
#: A promoted graph the extractor can carry, as `graph_extractor_record` records it.
CARRIED = {"version": repoindex.INDEXER_EXTRACTOR_VERSION, "blob_ids": True,
           "files_not_listed": 0, "truncated": []}


@pytest.fixture
def github():
    return IndexGitHub(heads={"main": ONE})


@pytest.fixture
def client(db, tokens, group_map, objects, github):
    return make_client(
        db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=github
    )


@pytest.fixture
def repo_id(client) -> str:
    created = client.post(
        "/v1/repositories", json={"repository": REPOSITORY}, headers=auth_header("alice")
    )
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


def _index_now(client, repo_id, **body):
    return client.post(f"/v1/repositories/{repo_id}/index", json=body,
                       headers=auth_header("alice"))


def _tasks(db) -> list[dict]:
    return [doc for path, doc in db.docs.items()
            if path.startswith("tasks/") and path.count("/") == 1]


def _promote_one_with_a_graph(client, db, objects, repo_id) -> None:
    """ONE indexed in full and promoted, its version carrying a graph."""
    started = _index_now(client, repo_id, kind="full")
    assert started.status_code == 202, started.text
    finish_index_task(db, objects, started.json()["run"]["task_id"], fixture_index(ONE))
    assert client.get(f"/v1/repositories/{repo_id}/index",
                      headers=auth_header("alice")).status_code == 200
    # The version as promotion records it when the index named a graph
    # (`graph_digest`); the graph's own checks are test_repository_index_graph_wiring.py's.
    db.docs[f"repositories/{repo_id}/index_versions/{ONE}"]["graph_digest"] = GRAPH
    db.docs[f"repositories/{repo_id}/index_versions/{ONE}"]["graph_extractor"] = dict(CARRIED)


# --------------------------------------------------------------------------
# choose_kind: §3.4, condition by condition
# --------------------------------------------------------------------------

def _state(**overrides):
    index = {"current_sha": ONE, "last_kind": "full",
             "last_indexed_at": NOW - timedelta(days=1), "full_every_days": 7}
    index.update(overrides)
    return index


def _choose(index=None, *, requested="incremental", version=None, changes=None):
    return repoindex.choose_kind(
        _state() if index is None else index, TWO, requested=requested,
        version=({"commit_sha": ONE, "graph_digest": GRAPH, "graph_extractor": dict(CARRIED)}
                 if version is None else version),
        changes=({"status": "ahead", "files": ["src/a.py", "src/b.py"]}
                 if changes is None else changes),
        now=NOW,
    )


def test_every_condition_holding_is_incremental_from_the_promoted_index():
    choice = _choose()
    assert choice == repoindex.KindChoice("incremental", base_sha=ONE, reason=None)


@pytest.mark.parametrize("changed", [
    "Makefile", "pyproject.toml", "web/package.json", "web/package-lock.json", "uv.lock",
    ".github/workflows/ci.yml", "tests/conftest.py", "pytest.ini", "web/vitest.config.ts",
    "web/tsconfig.json", "go.mod", "terraform/.terraform.lock.hcl", "pyrightconfig.json",
])
def test_a_build_or_test_configuration_change_forces_full(changed):
    choice = _choose(changes={"status": "ahead", "files": ["src/a.py", changed]})
    assert choice.kind == "full" and choice.base_sha is None
    assert "configuration changed" in choice.reason and changed in choice.reason


@pytest.mark.parametrize("status", ["diverged", "behind", "identical"])
def test_a_base_that_is_not_an_ancestor_of_the_head_forces_full(status):
    choice = _choose(changes={"status": status, "files": ["src/a.py"]})
    assert choice.kind == "full" and "not an ancestor" in choice.reason


def test_three_hundred_changed_files_force_full():
    files = [f"src/f{n}.py" for n in range(repoindex.MAX_INCREMENTAL_CHANGES)]
    choice = _choose(changes={"status": "ahead", "files": files})
    assert choice.kind == "full" and "300" in choice.reason
    files.pop()
    assert _choose(changes={"status": "ahead", "files": files}).kind == "incremental"


def test_the_weekly_full_run_stays():
    old = _state(last_indexed_at=NOW - timedelta(days=7))
    choice = _choose(old)
    assert choice.kind == "full" and "due every 7" in choice.reason
    # full_every_days is the registration's own.
    assert _choose(_state(last_indexed_at=NOW - timedelta(days=7),
                          full_every_days=14)).kind == "incremental"
    # Incremental runs do not reset it: last_full_at does.
    after = _state(last_kind="incremental", last_indexed_at=NOW,
                   last_full_at=NOW - timedelta(days=8))
    assert _choose(after).kind == "full"
    recent = _state(last_kind="incremental", last_indexed_at=NOW,
                    last_full_at=NOW - timedelta(days=2))
    assert _choose(recent).kind == "incremental"
    unknown = _state(last_kind="incremental", last_indexed_at=NOW)
    assert "no full run" in _choose(unknown).reason


@pytest.mark.parametrize("index,version,changes,why", [
    (_state(current_sha=None), None, None, "no promoted index"),
    (_state(current_sha=TWO), None, None, "the head is the promoted index"),
    (_state(), {"commit_sha": ONE, "graph_digest": None}, None, "no graph"),
])
def test_without_a_base_to_build_on_the_run_is_full(index, version, changes, why):
    choice = repoindex.choose_kind(index, TWO, requested="incremental",
                                   version=version, changes=changes, now=NOW)
    assert choice.kind == "full" and why in choice.reason


@pytest.mark.parametrize("record,why", [
    (None, "predates incremental runs"),
    (dict(CARRIED, version="0"), "version '0'"),
    (dict(CARRIED, blob_ids=False), "blob id"),
    (dict(CARRIED, files_not_listed=3), "truncated (files)"),
    (dict(CARRIED, truncated=["call_edges:symbol"]), "truncated (call_edges:symbol)"),
])
def test_a_graph_the_extractor_would_not_carry_forces_full(record, why):
    """Lane IX2 review: the extractor-side fallbacks are decided before submission.

    Otherwise the run is submitted incremental, with the 900 s timeout, and
    the extractor then reads the whole repository inside it.
    """
    version = {"commit_sha": ONE, "graph_digest": GRAPH, "graph_extractor": record}
    choice = _choose(version=version)
    assert choice.kind == "full" and choice.base_sha is None
    assert why in choice.reason


def test_the_graph_extractor_record_is_read_from_the_manifest():
    manifest = {"extractor": {"version": "1", "blob_ids": True, "files_not_listed": 0},
                "truncated": ["call_edges:below_0.4"]}
    record = repoindex.graph_extractor_record(manifest)
    assert record == {"version": "1", "blob_ids": True, "files_not_listed": 0,
                      "truncated": ["call_edges:below_0.4"]}
    assert "truncated" in repoindex.graph_carry_refusal(record)
    assert repoindex.graph_carry_refusal(dict(record, truncated=[])) is None
    # A manifest with no extractor block is a graph nobody can carry.
    assert repoindex.graph_extractor_record({})["blob_ids"] is False
    assert repoindex.graph_extractor_record(None) is None


def test_an_unknown_relation_or_a_full_request_is_full():
    unread = repoindex.choose_kind(_state(), TWO, requested="incremental",
                                   version={"graph_digest": GRAPH,
                                            "graph_extractor": dict(CARRIED)},
                                   changes=None, now=NOW)
    assert unread.kind == "full" and "GitHub could not say" in unread.reason
    assert _choose(requested="full").reason == "a full run was asked for"


# --------------------------------------------------------------------------
# the submission
# --------------------------------------------------------------------------

def test_an_incremental_request_submits_an_incremental_run_with_its_base(
    client, db, objects, repo_id, github
):
    _promote_one_with_a_graph(client, db, objects, repo_id)
    github.heads["main"] = TWO
    github.compares[(ONE, TWO)] = {"status": "ahead", "ahead_by": 2,
                                   "files": ["src/api/users.py", "src/worker/queue.py"]}

    response = _index_now(client, repo_id, kind="incremental")

    assert response.status_code == 202, response.text
    run = response.json()["run"]
    assert run["kind"] == "incremental" and run["base_sha"] == ONE
    assert run["kind_reason"] is None
    task = db.docs[f"tasks/{run['task_id']}"]
    # By profile name, the API's own prompt, nothing a caller chose (invariant 10).
    assert task["runner_profile"] == "indexer" and set(task["input"]) == {"prompt"}
    assert task["timeout_seconds"] == repoindex.INCREMENTAL_TIMEOUT_SECONDS
    assert task["metadata"]["index_kind"] == "incremental"
    assert task["metadata"]["base_sha"] == ONE
    prompt = task["input"]["prompt"]
    assert ONE in prompt and repoindex.BASE_INDEX_FILE in prompt
    # The worker reads the base from this line of the SIGNED prompt, never
    # from the unsigned metadata.
    assert f"\n{repoindex.BASE_LINE}{ONE}\n" in prompt
    stored = db.docs[f"repo_index_runs/{run['task_id']}"]
    assert stored["requested_kind"] == "incremental" and stored["base_sha"] == ONE


def test_a_configuration_change_submits_a_full_run_saying_why(
    client, db, objects, repo_id, github
):
    _promote_one_with_a_graph(client, db, objects, repo_id)
    github.heads["main"] = TWO
    github.compares[(ONE, TWO)] = {"status": "ahead", "ahead_by": 1,
                                   "files": ["src/api/users.py", "Makefile"]}

    run = _index_now(client, repo_id, kind="incremental").json()["run"]

    assert run["kind"] == "full" and run["base_sha"] is None
    assert "Makefile" in run["kind_reason"]
    task = db.docs[f"tasks/{run['task_id']}"]
    assert task["timeout_seconds"] == repoindex.FULL_TIMEOUT_SECONDS
    assert task["metadata"]["index_kind"] == "full" and "base_sha" not in task["metadata"]
    assert repoindex.BASE_INDEX_FILE not in task["input"]["prompt"]
    assert repoindex.BASE_LINE not in task["input"]["prompt"]


def test_a_non_ancestor_base_submits_a_full_run(client, db, objects, repo_id, github):
    _promote_one_with_a_graph(client, db, objects, repo_id)
    github.heads["main"] = TWO
    github.compares[(ONE, TWO)] = {"status": "diverged", "files": ["src/api/users.py"]}

    run = _index_now(client, repo_id, kind="incremental").json()["run"]

    assert run["kind"] == "full" and "not an ancestor" in run["kind_reason"]


@pytest.mark.parametrize("record", [None, dict(CARRIED, truncated=["files"])])
def test_a_graph_promoted_before_ix2_or_truncated_submits_a_full_run_with_the_full_timeout(
    client, db, objects, repo_id, github, record
):
    _promote_one_with_a_graph(client, db, objects, repo_id)
    db.docs[f"repositories/{repo_id}/index_versions/{ONE}"]["graph_extractor"] = record
    github.heads["main"] = TWO
    github.compares[(ONE, TWO)] = {"status": "ahead", "ahead_by": 1,
                                   "files": ["src/api/users.py"]}

    run = _index_now(client, repo_id, kind="incremental").json()["run"]

    assert run["kind"] == "full" and run["base_sha"] is None
    assert "promoted graph" in run["kind_reason"]
    task = db.docs[f"tasks/{run['task_id']}"]
    assert task["timeout_seconds"] == repoindex.FULL_TIMEOUT_SECONDS == 1800
    assert repoindex.BASE_LINE not in task["input"]["prompt"]


def test_index_now_without_a_kind_is_still_full(client, db, objects, repo_id, github):
    _promote_one_with_a_graph(client, db, objects, repo_id)
    github.heads["main"] = TWO
    github.compares[(ONE, TWO)] = {"status": "ahead", "files": ["src/api/users.py"]}
    run = _index_now(client, repo_id).json()["run"]
    assert run["kind"] == "full" and run["kind_reason"] == "a full run was asked for"


def test_an_unknown_kind_is_refused(client, db, repo_id):
    response = _index_now(client, repo_id, kind="partial")
    assert response.status_code == 422
    assert "'full' or 'incremental'" in response.json()["message"]
    assert _tasks(db) == []


# --------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------

def _incremental_run(client, db, objects, repo_id, github) -> str:
    _promote_one_with_a_graph(client, db, objects, repo_id)
    github.heads["main"] = TWO
    github.compares[(ONE, TWO)] = {"status": "ahead", "ahead_by": 1,
                                   "files": ["src/api/users.py"]}
    run = _index_now(client, repo_id, kind="incremental").json()["run"]
    assert run["kind"] == "incremental"
    return run["task_id"]


def test_an_incremental_index_is_promoted_with_its_kind_and_base(
    client, db, objects, repo_id, github
):
    task_id = _incremental_run(client, db, objects, repo_id, github)
    full_at = db.docs[f"repositories/{repo_id}"]["index"]["last_full_at"]
    document = fixture_index(TWO, kind="incremental", base_sha=ONE)
    document["modules"][1]["commit_sha"] = ONE
    finish_index_task(db, objects, task_id, document)

    body = client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice")).json()

    assert body["index"]["commit_sha"] == TWO
    assert body["index"]["kind"] == "incremental" and body["index"]["base_sha"] == ONE
    assert f"from `{ONE}`" in body["summary"]
    version = db.docs[f"repositories/{repo_id}/index_versions/{TWO}"]
    assert version["kind"] == "incremental" and version["base_sha"] == ONE
    index = db.docs[f"repositories/{repo_id}"]["index"]
    assert index["current_sha"] == TWO and index["last_kind"] == "incremental"
    # Only a full index moves what full_every_days counts from.
    assert index["last_full_at"] == full_at
    promotion = db.docs[f"repo_index_runs/{task_id}"]["promotion"]
    assert promotion["outcome"] == "promoted"
    assert promotion["kind"] == "incremental" and promotion["base_sha"] == ONE


def test_a_run_that_fell_back_to_full_is_promoted_as_full(
    client, db, objects, repo_id, github
):
    """The worker or the extractor may find the base unusable: the index says full."""
    task_id = _incremental_run(client, db, objects, repo_id, github)
    finish_index_task(db, objects, task_id, fixture_index(TWO))
    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    index = db.docs[f"repositories/{repo_id}"]["index"]
    assert index["current_sha"] == TWO and index["last_kind"] == "full"
    assert index["last_full_at"] == index["last_indexed_at"]


def test_an_index_claiming_a_base_it_was_not_given_is_refused(
    client, db, objects, repo_id, github
):
    task_id = _incremental_run(client, db, objects, repo_id, github)
    finish_index_task(db, objects, task_id, fixture_index(TWO, kind="incremental",
                                                          base_sha=THREE))
    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    promotion = db.docs[f"repo_index_runs/{task_id}"]["promotion"]
    assert promotion["outcome"] == "refused"
    assert THREE in promotion["reason"] and ONE in promotion["reason"]
    assert db.docs[f"repositories/{repo_id}"]["index"]["current_sha"] == ONE


def test_a_full_run_whose_index_claims_to_be_incremental_is_refused(
    client, db, objects, repo_id
):
    started = _index_now(client, repo_id, kind="full").json()["run"]["task_id"]
    finish_index_task(db, objects, started, fixture_index(ONE, kind="incremental",
                                                          base_sha=THREE))
    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))
    promotion = db.docs[f"repo_index_runs/{started}"]["promotion"]
    assert promotion["outcome"] == "refused" and "given no base" in promotion["reason"]


def test_runs_serve_their_kind_and_base():
    served = repoindex.run_to_api({"task_id": "t", "kind": "incremental", "base_sha": ONE,
                                   "kind_reason": None})
    assert served["kind"] == "incremental" and served["base_sha"] == ONE
    assert "kind_reason" in served
