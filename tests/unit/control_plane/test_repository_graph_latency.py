"""The indexer's opt-in answer latency measurement (#1840,
docs/design/graph-answer-cache.md §1.2), in the shipped
images/agent-runtime-indexer/repo-index/repo_graph_shards.py.

WHAT IS HELD HERE
-----------------
* OPT-IN: without `measure_latency_hours` nothing is measured and no
  latency.json is written; the report has no `latency`.
* BESIDE THE MANIFEST, NOT IN IT: with it, `graph/<sha>/latency.json` holds
  the battery's runs (p50, p95, max, the reference, the five slowest) in the
  schema swarm-api reads, and the manifest's bytes and digest are the same as
  an unmeasured write's.
* AT MOST ONCE PER N HOURS per registration: a second write within N hours
  measures nothing and says when it last did.
* NEVER FAILS THE WRITE: a measurement that raises is the report's reason.
* THE SAME SHAPE swarm-api reads: `graphreports.measurement_of` takes the
  written file and gives the median run's ratio.
* THE CLI flag reaches `write_graph`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_api import graphreports

WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")
COMMIT = "a" * 40


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


def graph_doc(symbols: int = 30) -> dict:
    def sym(path: str, name: str) -> dict:
        return {"id": f"{path}#{name}", "kind": "function", "path": path, "start_line": 1,
                "end_line": 3, "language": "python", "exported": True}

    syms = [sym(f"src/m{i % 5}.py", f"load_user_{i}") for i in range(symbols)]
    edges = [{"from": syms[i]["id"], "to": syms[(i * 7) % 5]["id"], "kind": "call",
              "evidence": "ast", "confidence": 0.9, "also_evidence": [],
              "path": syms[i]["path"], "line": 2, "sites": 1} for i in range(symbols)]
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": COMMIT,
        "branch": "main", "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "1"},
        "symbols": syms, "call_edges": edges, "symbol_test_map": [],
        "files": [{"path": f"src/m{i}.py"} for i in range(5)],
        "terms": [{"term": "user", "postings": {"src/m0.py": [["load_user_0", 1, 3]]}}],
        "term_stats": {"documents": symbols, "average_length": 3.0},
    }


def write(writer, store, **kwargs: Any) -> dict:
    return writer.write_graph(graph_doc(), store, tenant_id="eng", repo_id="repo_1", **kwargs)


def latency_path(root: Path) -> Path:
    return root / "tenants/eng/repos/repo_1/graph" / COMMIT / "latency.json"


def test_without_the_option_nothing_is_measured(writer, tmp_path) -> None:
    report = write(writer, writer.LocalStore(tmp_path))
    assert "latency" not in report and not latency_path(tmp_path).exists()


def test_the_measurement_is_written_beside_the_manifest_and_not_in_it(writer, tmp_path) -> None:
    plain = write(writer, writer.LocalStore(tmp_path / "plain"))
    root = tmp_path / "measured"
    report = write(writer, writer.LocalStore(root), measure_latency_hours=24)

    assert report["latency"]["measured"] is True, report["latency"]
    assert report["manifest_digest"] == plain["manifest_digest"]
    manifest = (root / "tenants/eng/repos/repo_1/graph" / COMMIT / "manifest.json").read_text()
    assert "latency" not in manifest
    document = json.loads(latency_path(root).read_text())
    assert document["schema"] == graphreports.LATENCY_SCHEMA == writer.LATENCY_SCHEMA
    assert document["commit_sha"] == COMMIT and document["symbols"] == 30
    # The busiest four and an even sample of twelve, three tools each.
    assert document["questions"] == 16 * 3 and document["tools"] == ["context", "callers", "search"]
    assert len(document["runs"]) == writer.LATENCY_RUNS
    for run in document["runs"]:
        assert run["p95_ms"] >= run["p50_ms"] >= 0 and run["max_ms"] >= run["p95_ms"]
        assert run["reference_ms"] > 0 and len(run["slowest"]) == writer.LATENCY_SLOWEST
    measured = graphreports.measurement_of(COMMIT, None, document, source="latency.json")
    assert measured is not None and measured.ratio > 0


def test_the_battery_is_timed_and_the_median_run_is_what_the_report_reads(writer) -> None:
    ticks = iter(range(0, 10_000_000, 1))

    def clock() -> float:
        # Each call advances a millisecond: every answer and reference takes 1 ms.
        return next(ticks) / 1000.0

    built = writer.build(graph_doc(), tenant_id="eng", repo_id="repo_1")
    manifest = json.loads(built[1])
    document = writer.measure_latency(manifest, built[2], graph_doc(), runs=3, clock=clock,
                                      reference=lambda: None, cpu_limit=2.0)
    assert [run["p95_ms"] for run in document["runs"]] == [1.0, 1.0, 1.0]
    assert [run["reference_ms"] for run in document["runs"]] == [1.0, 1.0, 1.0]
    assert document["cpu_limit"] == 2.0
    # Every edge goes into one of the first five symbols: those are the busiest.
    targets = {s["id"] for s in graph_doc()["symbols"][:5]}
    questions = writer.latency_questions(graph_doc())
    assert set(questions[:4]) <= targets and len(set(questions)) == 16


def test_at_most_once_per_n_hours_per_registration(writer, tmp_path) -> None:
    store = writer.LocalStore(tmp_path)
    first = write(writer, store, measure_latency_hours=24)
    assert first["latency"]["measured"] is True
    second = write(writer, store, measure_latency_hours=24)
    assert second["latency"]["measured"] is False
    assert "within 24 h" in second["latency"]["reason"]
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    due, why = writer.latency_due(store, tenant_id="eng", repo_id="repo_1", hours=24, now=later)
    assert due is True and why.startswith("last measured")


def test_a_failed_measurement_is_reported_and_never_fails_the_write(writer, tmp_path) -> None:
    store = writer.LocalStore(tmp_path)

    def broken(*_args: Any, **_kwargs: Any) -> dict:
        raise KeyError("blob")

    built = writer.build(graph_doc(), tenant_id="eng", repo_id="repo_1")
    out = writer.write_latency(store, built[1], built[2], graph_doc(), tenant_id="eng",
                               repo_id="repo_1", hours=24, measure=broken)
    assert out == {"measured": False, "reason": "the measurement failed (KeyError)"}
    assert not latency_path(tmp_path).exists()


def test_the_cli_flag_reaches_the_write(writer, tmp_path, monkeypatch, capsys) -> None:
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(graph_doc()))
    (tmp_path / "repo").mkdir()
    root = tmp_path / "store"
    monkeypatch.setenv(writer.TENANT_ENV, "eng")
    code = writer.main(["write", "--no-sweep", "--graph", str(graph), "--repo-id", "repo_1",
                        "--tenant", "eng", "--store", str(root),
                        "--destination", "tenants/eng/repos/repo_1/graph",
                        "--measure-latency-hours", "24"])
    assert code == 0, capsys.readouterr().err
    assert json.loads(capsys.readouterr().out)["latency"]["measured"] is True
    assert latency_path(root).exists()
