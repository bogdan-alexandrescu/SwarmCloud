"""Each index run's measurements, on the run record the index-runs read serves.

The owner's agent quality program (BGD, 2026-11-03) proves the landed
incremental index carry before relying on it, and its canary
(scripts/index_canary_report.py) reads these. What is held here:

* the phase durations -- stage_base, extraction, language servers, judgment,
  check, upload, sweep, promotion -- are read from the worker's phase records
  and the graph manifest, and a phase that did not run, or cannot be known,
  is None, never zero;
* whether the run was submitted and built full or incremental, and why a
  full rebuild happened: swarm-api's own reason when it submitted full, the
  extractor's when it was asked to carry and did not;
* the files the extractor reused and recomputed, from its manifest;
* a failed, cancelled or refused run is measured too (its cost is real);
* `run_to_api` serves the record, so GET .../index/runs carries it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api import repoindex

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE, TWO = sha("one"), sha("two")
T0 = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)

PHASES = {"repo_index_phases": [
    {"phase": "stage_base", "status": "ok", "seconds": 40.25},
    {"phase": "extract", "status": "ok", "seconds": 610.0},
    {"phase": "agent", "status": "ok", "seconds": 300.5},
    {"phase": "index_check", "status": "ok", "seconds": 1.5},
    {"phase": "graph_write", "status": "ok", "seconds": 88.0},
    {"phase": "index_sweep", "status": "skipped", "seconds": 0.0, "reason": "no keep set"},
]}


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


def _runs(client, repo_id):
    return client.get(f"/v1/repositories/{repo_id}/index/runs",
                      headers=auth_header("alice")).json()["runs"]


def _index_now(client, repo_id):
    started = client.post(f"/v1/repositories/{repo_id}/index", json={"kind": "full"},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    return started.json()["run"]["task_id"]


# --------------------------------------------------------------------------
# the phases
# --------------------------------------------------------------------------

def test_the_phases_are_read_under_the_canarys_names():
    got = repoindex.phase_seconds(PHASES)
    # MUTATION: reading a skipped phase's 0.0 as a measurement.
    assert got == {"stage_base": 40.25, "extraction": 610.0, "judgment": 300.5,
                   "check": 1.5, "upload": 88.0, "sweep": None}


def test_a_phase_the_run_never_reached_is_unknown_not_zero():
    got = repoindex.phase_seconds({"repo_index_phases": [
        {"phase": "extract", "status": "timed_out", "seconds": 720.0},
    ]})
    assert got["extraction"] == 720.0
    assert got["judgment"] is None and got["upload"] is None
    assert repoindex.phase_seconds(None) == {name: None for name in got}
    # A resumed attempt records a phase again: the last record is the run's.
    again = repoindex.phase_seconds({"repo_index_phases": [
        {"phase": "agent", "status": "failed", "seconds": 12.0},
        {"phase": "agent", "status": "ok", "seconds": 30.0},
    ]})
    assert again["judgment"] == 30.0


def _manifest(*rows, lsp=True, work=None, incremental=None):
    extractor = {"lsp": {"servers": {}} if lsp else None}
    if work is not None:
        extractor["work"] = work
    if incremental is not None:
        extractor["incremental"] = incremental
    return {"kind": "full", "languages": list(rows), "extractor": extractor}


def _row(language, server, status, **counts):
    return {"language": language, "server": server, "status": status, "lsp": counts}


def test_the_language_servers_time_is_each_servers_once_and_summed():
    manifest = _manifest(
        _row("python", "pyright", "ok", grant_seconds=900.0, cost_seconds=410),
        # tsserver serves two languages: counted once.
        _row("typescript", "tsserver", "partial", grant_seconds=120.0, cost_seconds=300),
        _row("javascript", "tsserver", "partial", grant_seconds=120.0, cost_seconds=300),
        # Carried answers, never started: no time.
        _row("go", "gopls", "ok", grant_seconds=0.0, cost_seconds=60),
    )
    # MUTATION: a cut server's scaled `cost_seconds` (its need, 300) for the grant it used.
    assert repoindex.language_server_seconds(manifest) == 410 + 120


def test_a_server_that_measured_nothing_makes_the_pass_unknown():
    manifest = _manifest(
        _row("python", "pyright", "ok", grant_seconds=900.0, cost_seconds=410),
        _row("hcl", "terraform-ls", "failing", grant_seconds=60.0),
    )
    assert repoindex.language_server_seconds(manifest) is None
    assert repoindex.language_server_seconds(_manifest(lsp=False)) is None
    assert repoindex.language_server_seconds(None) is None


def test_the_files_reused_and_recomputed_are_the_manifests():
    work = {"parsed": 2000, "recomputed": 14, "reused": 1986, "lsp_asked": 14,
            "lsp_carried": 1900}
    assert repoindex.files_work(_manifest(work=work)) == work
    assert repoindex.files_work(_manifest(work={"parsed": True, "recomputed": -1})) == {
        "parsed": None, "recomputed": None, "reused": None, "lsp_asked": None,
        "lsp_carried": None}
    # A graph extracted before the extractor said is unknown, not zero files.
    assert repoindex.files_work(_manifest()) is None


# --------------------------------------------------------------------------
# full or incremental, and why
# --------------------------------------------------------------------------

def test_a_full_submission_says_swarm_apis_reason():
    run = {"kind": "full", "kind_reason": "the weekly full run is due"}
    assert repoindex.rebuild_reason(run, "full", None) == "the weekly full run is due"
    assert repoindex.rebuild_reason({"kind": "full"}, "full", None) == (
        "submitted full; no reason was recorded")


def test_an_incremental_submission_built_full_says_the_extractors_reason():
    run = {"kind": "incremental", "kind_reason": None}
    manifest = _manifest(incremental={"ran": False,
                                      "reason": "configuration changed: pyproject.toml"})
    assert repoindex.rebuild_reason(run, "full", manifest) == (
        "the extractor did not carry the base: configuration changed: pyproject.toml")
    # MUTATION: reporting swarm-api's None as "no rebuild".
    assert repoindex.rebuild_reason(run, "full", None) == (
        "submitted incremental and built full; the extractor recorded no reason")
    assert repoindex.rebuild_reason(run, "incremental", manifest) is None
    assert repoindex.rebuild_reason(run, None, manifest) is None


def test_the_metrics_name_promotion_and_lag():
    run = {"kind": "incremental", "queued_at": T0}
    got = repoindex.run_metrics(
        PHASES, run, built_kind="incremental", manifest=_manifest(
            _row("python", "pyright", "ok", grant_seconds=200.0, cost_seconds=150),
            work={"parsed": 10, "recomputed": 2, "reused": 8, "lsp_asked": 2,
                  "lsp_carried": 8}),
        ended_at=T0 + timedelta(minutes=20), promoted_at=T0 + timedelta(minutes=21),
        at_head=True,
    )
    assert got["phases"] == {
        "stage_base": 40.25, "extraction": 610.0, "language_servers": 150.0,
        "judgment": 300.5, "check": 1.5, "upload": 88.0, "sweep": None, "promotion": 60.0,
    }
    assert got["requested_kind"] == "incremental" and got["built_kind"] == "incremental"
    assert got["rebuild_reason"] is None
    assert got["files"]["reused"] == 8 and got["files"]["recomputed"] == 2
    assert got["lag_seconds"] == 21 * 60 and got["at_head"] is True


def test_a_run_that_built_nothing_has_unknowns_not_zeros():
    got = repoindex.run_metrics({"repo_index_phases": [
        {"phase": "extract", "status": "failed", "seconds": 5.0}]}, {"kind": "full"})
    assert got["built_kind"] is None and got["rebuild_reason"] is None
    assert got["phases"]["extraction"] == 5.0
    assert got["phases"]["promotion"] is None and got["phases"]["language_servers"] is None
    assert got["files"] is None and got["lag_seconds"] is None and got["at_head"] is None


# --------------------------------------------------------------------------
# on the run, through the index-runs read
# --------------------------------------------------------------------------

def test_a_promoted_run_serves_its_metrics(client, db, objects, repo_id):
    task_id = _index_now(client, repo_id)
    finish_index_task(db, objects, task_id, fixture_index(ONE))
    db.docs[f"tasks/{task_id}"]["result_summary"].update(PHASES)

    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))

    run = _runs(client, repo_id)[0]
    assert run["promotion"]["outcome"] == "promoted"
    metrics = run["metrics"]
    # MUTATION: `run_to_api` without `metrics`, or the promotion not writing it.
    assert metrics["requested_kind"] == "full" and metrics["built_kind"] == "full"
    assert metrics["rebuild_reason"]
    assert metrics["phases"]["extraction"] == 610.0
    assert metrics["phases"]["judgment"] == 300.5
    assert metrics["phases"]["upload"] == 88.0
    # No graph in this fixture: the servers' time and the files are unknown.
    assert metrics["phases"]["language_servers"] is None and metrics["files"] is None
    assert metrics["lag_seconds"] is not None and metrics["lag_seconds"] >= 0
    assert metrics["at_head"] is True


def test_a_failed_run_is_measured_too(client, db, objects, repo_id):
    task_id = _index_now(client, repo_id)
    finish_index_task(db, objects, task_id, None, state="FAILED")
    db.docs[f"tasks/{task_id}"]["result_summary"] = {"repo_index_phases": [
        {"phase": "extract", "status": "timed_out", "seconds": 720.0}]}

    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))

    run = _runs(client, repo_id)[0]
    assert run["state"] == "FAILED"
    # MUTATION: measuring only the runs that promoted, so a canary undercounts cost.
    assert run["metrics"]["phases"]["extraction"] == 720.0
    assert run["metrics"]["built_kind"] is None


def test_a_refused_run_is_measured_too(client, db, objects, repo_id):
    task_id = _index_now(client, repo_id)
    finish_index_task(db, objects, task_id, fixture_index(TWO))
    db.docs[f"tasks/{task_id}"]["result_summary"].update(PHASES)

    client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header("alice"))

    run = _runs(client, repo_id)[0]
    assert run["promotion"]["outcome"] == "refused"
    assert run["metrics"]["phases"]["judgment"] == 300.5
    assert run["metrics"]["built_kind"] is None


def test_a_run_recorded_before_the_metrics_serves_none():
    assert repoindex.run_to_api({"task_id": "task_1"})["metrics"] is None
